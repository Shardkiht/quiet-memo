from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from ..infra.config import Config, LLMConfig, RetrievalConfig, default_config_path, load_config
from ..infra.gitops import GitError, GitRepo
from ..infra.tokens import truncate_to_tokens
from ..llm import LiteLLMEmbedder
from ..llm.commit_summary import summarize_diff
from ..retrieval import (
    Embedder,
    HybridRetriever,
    KeywordRetriever,
    LocalEmbedder,
    Retriever,
    SearchHit,
    VectorRetriever,
)
from .audit import AuditLog
from .detect import contains_secret, find_conflicts
from .extractor import ImportExtractor, LLMExtractor
from .models import Candidate, Entry, PendingItem
from .policy import decide
from .queue import PendingQueue
from .store import STATUS_ACTIVE, STATUS_DELETED, STATUS_SUPERSEDED, MemoryStore


VAULT_README = """# quiet-memo vault

这个目录由 [quiet-memo](https://github.com/Shardkiht/quiet-memo) 管理。

- `memories/`：按主题拆分的 Markdown 记忆，是唯一事实来源。
- `sessions/`：会话 JSONL，保留原始对话与工具轨迹。
- `.quiet-memo/pending.jsonl`：低置信、冲突或敏感候选，等待人工确认。
- `.quiet-memo/resolved.jsonl`：已确认/已拒绝记录。
- `.quiet-memo/audit.jsonl`：更正、删除等审计记录。
- `SOUL.md`（可选）：自定义人设，替代系统提示词里的默认人设段。
- 全部变更由 Git 跟踪，可回滚、可审计。
"""

VAULT_GITIGNORE = """__pycache__/
*.pyc
.DS_Store
Thumbs.db
.quiet-memo/vectors.npz
.quiet-memo/search-cache.json
"""

# 默认人设兼作 init 写入 SOUL.md 的模板；放 memory 层是因为 agent/prompts.py 可以引 memory，
# 反向不行（分层：app → agent → memory → retrieval → infra）。
DEFAULT_PERSONA = "你是用户的个人记忆助手，运行在本地终端，拥有跨会话的长期记忆。"

SOUL_FILENAME = "SOUL.md"
SOUL_MAX_TOKENS = 2000


def load_persona(vault: Path) -> str:
    """读 vault/SOUL.md 自定义人设；缺失或为空返回空串，超长按 token 截断。

    用户可能用记事本以 ANSI/GBK 保存，UTF-8 失败时按本机编码兜底，避免炸掉整轮对话。
    """
    path = vault / SOUL_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError:
        # 记事本 ANSI（中文系统即 GBK）保存的场景；gbk 编码全平台内置，
        # 不用 Windows 专属的 mbcs——英文系统的 cp1252 解不了 GBK。
        try:
            text = path.read_text(encoding="gbk")
        except (UnicodeDecodeError, OSError):
            return ""
    text = text.strip()
    if not text:
        return ""
    return truncate_to_tokens(text, SOUL_MAX_TOKENS)


def _build_embedder(config: RetrievalConfig) -> Embedder | None:
    """按配置构造 Embedder；后端不可用返回 None，由调用方回退 keyword。"""
    embedding = config.embedding
    if embedding.backend == "api":
        llm = LLMConfig(
            provider=embedding.provider,
            base_url=embedding.base_url,
            model=embedding.model,
            api_key_env=embedding.api_key_env,
        )
        if llm.requires_api_key() and not llm.api_key():
            return None
        return LiteLLMEmbedder(llm)
    if not LocalEmbedder.available():
        return None
    return LocalEmbedder(embedding.model)


def build_retriever(config: Config, store: MemoryStore) -> tuple[Retriever, str]:
    """按 [retrieval] 装配检索器；返回检索器和模式标签，回退时标签注明。"""
    method = (config.retrieval.method or "hybrid").strip().lower()
    keyword = KeywordRetriever(store)
    if method == "keyword":
        return keyword, "keyword"
    embedder = _build_embedder(config.retrieval)
    if embedder is None:
        return keyword, f"{method}-fallback-keyword"
    vector = VectorRetriever(store, embedder)
    if method == "vector":
        return vector, "vector"
    return HybridRetriever(keyword, vector), "hybrid"


@dataclass(slots=True)
class MemorizeResult:
    admitted: list[Entry]
    queued: list[PendingItem]
    duplicates: list[str]
    blocked: list[str]

    def to_dict(self) -> dict:
        return {
            "admitted": [
                {"id": entry.id, "topic": entry.topic, "text": entry.text, "timestamp": entry.timestamp}
                for entry in self.admitted
            ],
            "queued": [item.model_dump() for item in self.queued],
            "duplicates": self.duplicates,
            "blocked": self.blocked,
        }


class MemoryService:
    def __init__(
        self,
        config: Config,
        extractor=None,
        retriever: Retriever | None = None,
        config_path: Path | None = None,
    ):
        self.config = config
        self.store = MemoryStore(config.vault)
        self.queue = PendingQueue(config.vault)
        self.repo = GitRepo(config.vault)
        self.audit = AuditLog(config.vault)
        self.extractor = extractor or LLMExtractor(config.llm)
        if retriever is not None:
            self.retriever: Retriever = retriever
            self.retrieval_mode = "custom"
        else:
            self.retriever, self.retrieval_mode = build_retriever(config, self.store)
        # 热更新（切模型/上下文）写回配置的路径：记住加载来源，避免写错文件或被读回旧值。
        self.config_path = config_path
        # 后台补沉淀与前台对话可能并发写 vault：所有变更操作在这把锁内串行。
        self.write_lock = threading.Lock()

    def init_vault(self) -> dict:
        self.store.ensure()
        self.queue.dir.mkdir(parents=True, exist_ok=True)
        (self.config.vault / "sessions").mkdir(parents=True, exist_ok=True)
        readme = self.config.vault / "README.md"
        if not readme.exists():
            readme.write_text(VAULT_README, encoding="utf-8")
        soul = self.config.vault / SOUL_FILENAME
        if not soul.exists():
            soul.write_text(DEFAULT_PERSONA + "\n", encoding="utf-8")
        gitignore = self.config.vault / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(VAULT_GITIGNORE, encoding="utf-8")
        else:
            # 老记忆库补齐派生缓存忽略规则；这些文件可重建，不进 Git 历史。
            existing = gitignore.read_text(encoding="utf-8")
            missing = [
                line
                for line in (".quiet-memo/vectors.npz", ".quiet-memo/search-cache.json")
                if line not in existing
            ]
            if missing:
                with open(gitignore, "a", encoding="utf-8") as handle:
                    # 旧文件可能没有末尾换行，直接追加会把规则拼到上一行。
                    if existing and not existing.endswith("\n"):
                        handle.write("\n")
                    handle.write("\n".join(missing) + "\n")
        created = self.repo.ensure_init()
        self.repo.stage_all()
        commit_hash = self.repo.commit("memory: 初始化记忆库")
        return {"vault": str(self.config.vault), "git_created": created, "commit": commit_hash}

    def memorize(
        self,
        text: str,
        source: str = "chat",
        session_id: str | None = None,
        message_id: str | None = None,
    ) -> MemorizeResult:
        text = text.strip()
        if not text:
            raise ValueError("对话内容为空。")
        extraction = self.extractor.extract(text, self.store.recent_context())
        return self._adjudicate(extraction.memories, source, session_id, message_id)

    def import_text(
        self,
        text: str,
        *,
        source: str = "import",
        dry_run: bool = False,
        extractor=None,
    ) -> MemorizeResult:
        """导入已有文档：AI 按契约整理成候选，程序裁决；dry_run 只出计划不落盘。"""
        text = text.strip()
        if not text:
            raise ValueError("导入内容为空。")
        if extractor is None:
            topics = [topic.topic for topic in self.store.topics()]
            extractor = ImportExtractor(self.config.llm, topics=topics)
        return self._adjudicate(extractor.extract(text).memories, source, dry_run=dry_run)

    def _adjudicate(
        self,
        candidates: list[Candidate],
        source: str,
        session_id: str | None = None,
        message_id: str | None = None,
        dry_run: bool = False,
    ) -> MemorizeResult:
        with self.write_lock:
            return self._adjudicate_locked(candidates, source, session_id, message_id, dry_run)

    def _adjudicate_locked(
        self,
        candidates: list[Candidate],
        source: str,
        session_id: str | None = None,
        message_id: str | None = None,
        dry_run: bool = False,
    ) -> MemorizeResult:
        admitted: list[Entry] = []
        queued: list[PendingItem] = []
        duplicates: list[str] = []
        blocked: list[str] = []
        for candidate in candidates:
            if contains_secret(candidate.text):
                blocked.append(candidate.text)
                continue
            if self.store.has_text(candidate.text):
                duplicates.append(candidate.text)
                continue
            conflicts = find_conflicts(candidate, self.store.entries())
            if conflicts:
                item = self._queue_candidate(
                    candidate,
                    source,
                    f"possible conflict with {', '.join(entry.id for entry in conflicts)}",
                    session_id,
                    message_id,
                    conflict_with=conflicts[0].id,
                    dry_run=dry_run,
                )
                queued.append(item)
                continue
            decision = decide(candidate, self.config.confidence_threshold)
            if decision.admit:
                if dry_run:
                    admitted.append(
                        Entry(id="(dry-run)", topic=candidate.topic, text=candidate.text, timestamp="")
                    )
                else:
                    admitted.append(self._append_candidate(candidate, source, session_id, message_id))
            else:
                queued.append(
                    self._queue_candidate(
                        candidate, source, decision.reason, session_id, message_id, dry_run=dry_run
                    )
                )
        return MemorizeResult(
            admitted=admitted, queued=queued, duplicates=duplicates, blocked=blocked
        )

    def _append_candidate(
        self,
        candidate: Candidate,
        source: str,
        session_id: str | None = None,
        message_id: str | None = None,
        supersedes: str | None = None,
    ) -> Entry:
        extra = {"session": session_id or "", "message": message_id or ""}
        return self.store.append(
            candidate.topic,
            candidate.text,
            source=source,
            confidence=candidate.confidence,
            title=candidate.title,
            extra=extra,
            supersedes=supersedes,
        )

    def _queue_candidate(
        self,
        candidate: Candidate,
        source: str,
        reason: str,
        session_id: str | None = None,
        message_id: str | None = None,
        conflict_with: str | None = None,
        dry_run: bool = False,
    ) -> PendingItem:
        item = PendingItem.from_candidate(
            candidate,
            source,
            reason,
            session_id=session_id,
            message_id=message_id,
            conflict_with=conflict_with,
        )
        if not dry_run:
            self.queue.add(item)
        return item

    def note(self, text: str, topic: str = "uncategorized", title: str | None = None) -> Entry:
        text = text.strip()
        if not text:
            raise ValueError("内容为空。")
        if contains_secret(text):
            raise ValueError("检测到密钥或敏感信息，已拦截写入。")
        with self.write_lock:
            if self.store.has_text(text):
                raise ValueError("相同内容已存在。")
            return self.store.append(topic, text, source="user-note", confidence=1.0, title=title)

    def correct(self, entry_id: str, text: str, reason: str = "") -> Entry:
        with self.write_lock:
            old = self.store.find(entry_id)
            if old is None:
                raise KeyError(f"记忆不存在：{entry_id}")
            if old.status != STATUS_ACTIVE:
                raise ValueError(f"记忆当前状态为 {old.status}，不能更正。")
            if contains_secret(text):
                raise ValueError("检测到密钥或敏感信息，已拦截写入。")
            entry = self.store.append(
                old.topic,
                text.strip(),
                source="correction",
                confidence=1.0,
                supersedes=entry_id,
            )
            self.store.set_status(entry_id, STATUS_SUPERSEDED)
            self.audit.write(
                "correct",
                old_entry=entry_id,
                new_entry=entry.id,
                reason=reason,
                old_text=old.text,
                new_text=entry.text,
            )
            return entry

    def forget(self, entry_id: str, reason: str = "") -> Entry:
        with self.write_lock:
            old = self.store.find(entry_id)
            if old is None:
                raise KeyError(f"记忆不存在：{entry_id}")
            if old.status != STATUS_ACTIVE:
                raise ValueError(f"记忆当前状态为 {old.status}，无需删除。")
            self.store.set_status(entry_id, STATUS_DELETED)
            self.audit.write("forget", entry=entry_id, reason=reason, text=old.text)
            return old.model_copy(update={"status": STATUS_DELETED})

    def recall(self, query: str, limit: int = 10) -> list[SearchHit]:
        return self.retriever.search(query, limit)

    def read_topic(self, topic: str, max_tokens: int = 2000) -> dict:
        wanted = topic.strip().casefold()
        info = next(
            (
                item
                for item in self.store.topics()
                if wanted in (item.topic.casefold(), item.title.casefold())
            ),
            None,
        )
        if info is None:
            raise KeyError(f"主题不存在：{topic.strip()}")
        entries = self.store.entries(topic=info.topic)
        content = "\n".join(f"- {entry.text}" for entry in entries)
        truncated_content = truncate_to_tokens(content, max_tokens)
        return {
            "topic": info.topic,
            "title": info.title,
            "content": truncated_content,
            "entries": len(entries),
            "truncated": truncated_content != content,
        }

    def pending(self) -> list[PendingItem]:
        return self.queue.all()

    def approve(self, item_id: str) -> Entry:
        with self.write_lock:
            return self._approve_locked(item_id)

    def _approve_locked(self, item_id: str) -> Entry:
        item = self.queue.get(item_id)
        if item is None:
            raise KeyError(f"待确认项不存在：{item_id}")
        if contains_secret(item.text):
            raise ValueError("检测到密钥或敏感信息，已拦截写入。")
        supersedes = item.conflict_with
        if supersedes:
            old = self.store.find(supersedes)
            if old is not None and old.status == STATUS_ACTIVE:
                self.store.set_status(supersedes, STATUS_SUPERSEDED)
        entry = self.store.append(
            item.topic,
            item.text,
            source=f"{item.source}+approved",
            confidence=item.confidence,
            note="人工确认",
            title=item.title,
            extra={"session": item.session_id or "", "message": item.message_id or ""},
            supersedes=supersedes,
        )
        self.queue.resolve(item_id, "approved")
        self.audit.write("approve", item=item_id, entry=entry.id, supersedes=supersedes or "")
        return entry

    def reject(self, item_id: str) -> PendingItem:
        with self.write_lock:
            return self._reject_locked(item_id)

    def _reject_locked(self, item_id: str) -> PendingItem:
        item = self.queue.resolve(item_id, "rejected")
        if item is None:
            raise KeyError(f"待确认项不存在：{item_id}")
        self.audit.write("reject", item=item_id, text=item.text)
        return item

    def topics(self) -> list[dict]:
        return [topic.model_dump() for topic in self.store.topics()]

    def history(self, limit: int = 10) -> list[dict]:
        return self.repo.history(limit)

    def status(self) -> dict:
        return {
            "vault": str(self.config.vault),
            "topics": len(self.store.topics()),
            "entries": len(self.store.entries()),
            "pending": len(self.pending()),
            "dirty": self.repo.has_changes() if self.repo.is_repo else False,
            "retrieval": self.retrieval_mode,
        }

    def commit_pending_changes(self) -> str | None:
        """启动检查用：自动提交 vault 未保存变更（不 push、不调 LLM 生成总结）。"""
        with self.write_lock:
            self.repo.ensure_init()
            self.repo.stage_all()
            if not self.repo.has_staged():
                return None
            return self.repo.commit("memory: 自动提交未保存变更（启动检查）")

    def commit(self, push: bool | None = None) -> dict:
        with self.write_lock:
            return self._commit_locked(push)

    def _commit_locked(self, push: bool | None) -> dict:
        self.repo.ensure_init()
        self.repo.stage_all()
        if not self.repo.has_staged():
            return {"committed": False, "summary": "没有需要提交的变更。", "hash": None, "pushed": False}
        stat = self.repo.staged_stat()
        summary = summarize_diff(self.repo.staged_diff(), self.config.llm, stat)
        lines = summary.splitlines()
        subject = lines[0] if lines else "memory: 更新记忆库"
        if not subject.startswith("memory:"):
            subject = f"memory: {subject}"
        message = subject[:120]
        body = "\n".join(lines[1:]).strip()
        if body:
            message += f"\n\n{body}"
        commit_hash = self.repo.commit(message)
        # 本地提交已成功；push 失败只记录原因，不掩盖已完成的提交。
        should_push = self.config.auto_push if push is None else push
        pushed = False
        push_output = ""
        push_error = ""
        if should_push and commit_hash:
            try:
                push_output = self.repo.push()
                pushed = True
            except GitError as exc:
                push_error = str(exc)
        return {
            "committed": commit_hash is not None,
            "summary": summary,
            "hash": commit_hash,
            "pushed": pushed,
            "push_output": push_output,
            "push_error": push_error,
        }


def get_service(
    config_path: Path | None = None,
    vault: Path | None = None,
    extractor=None,
    retriever: Retriever | None = None,
) -> MemoryService:
    resolved = Path(config_path).expanduser() if config_path else default_config_path()
    config = load_config(resolved)
    if vault is not None:
        config.vault = Path(vault).expanduser().resolve()
    return MemoryService(config, extractor=extractor, retriever=retriever, config_path=resolved)
