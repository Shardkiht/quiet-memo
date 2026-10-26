from __future__ import annotations

from functools import partial

from textual.widgets import Input

from ..infra.tokens import format_tokens
from . import actions
from .chat_widgets import PickScreen, SLASH_COMMANDS, SessionPickerScreen, TextInputScreen
from .session_ui import visible_sessions


class ChatCommandsMixin:
    """斜杠命令、待确认、主题、会话与设置选择器；依赖 ChatApp 的界面方法。"""

    # ---------- 斜杠命令 ----------

    def _command(self, line: str) -> None:
        command, _, argument = line.partition(" ")
        command = command.lower()
        argument = argument.strip()

        if command in ("/exit", "/quit"):
            self.exit()
        elif command == "/help":
            self.add_card("\n".join(f"{usage:<24}{description}" for _, usage, description in SLASH_COMMANDS), "命令")
        elif command == "/model":
            self._open_models(argument.lower() in ("refresh", "--refresh", "-r"))
        elif command == "/think":
            self._pick_reasoning()
        elif command == "/context":
            self._pick_context()
        elif command == "/setup":
            self.add_card("完整配置需在终端运行 qm setup，当前界面内无法启动配置向导。")
        elif command == "/new":
            if not self.busy:
                self._new_session()
        elif command in ("/sessions", "/resume"):
            if argument and command == "/resume":
                self._resume_by_query(argument)
            else:
                self.open_session_picker()
        elif command == "/compact":
            self._run_compact()
        elif command == "/summary":
            if not self.runtime.has_session:
                self.add_card("新会话还没有摘要。")
            else:
                summary = self.runtime.summary_text()
                self.add_markdown_card(summary, "会话摘要") if summary else self.add_card("当前会话还没有摘要。")
        elif command == "/status":
            self._show_status()
        elif command == "/rename":
            if not argument:
                self.add_card("用法：/rename <title>")
            elif not self.runtime.has_session:
                self.add_card("新会话尚未创建，发送第一条消息后再重命名。")
            else:
                self.runtime.rename(argument)
                self.refresh_sessions()
                self.add_card(f"已重命名为：{argument}")
        elif command == "/recall":
            if not argument:
                self.add_card("用法：/recall <query>")
            else:
                hits = self.service.recall(argument, 10)
                self.add_card(
                    "\n".join(f"[{hit.topic}] {hit.score:.2f} · {hit.text}" for hit in hits)
                    if hits
                    else "未找到相关记忆。"
                )
        elif command == "/remember":
            if not argument:
                self.add_card("用法：/remember <text>")
            else:
                self._run_remember(argument)
        elif command == "/pending":
            self._open_pending()
        elif command == "/topics":
            self._show_topics(argument.strip() or None)
        else:
            self.add_card(f"未知命令：{command}，输入 /help 查看帮助。")

    # ---------- 待确认交互处理（issue #1） ----------

    @staticmethod
    def _truncate(text: str, width: int) -> str:
        return text if len(text) <= width else text[:width] + "…"

    def _open_pending(self) -> None:
        items = self.service.pending()
        if not items:
            self.add_card("没有待确认项。")
            return
        options = [
            (f"{item.id}  [{item.topic}] {self._truncate(item.text, 36)}", item.id)
            for item in items
        ]
        self.push_screen(PickScreen(f"待确认 {len(items)} 条", options, self._pending_picked))

    def _pending_picked(self, item_id) -> None:
        if item_id is None:
            self.query_one("#input", Input).focus()
            return
        item = next((i for i in self.service.pending() if i.id == item_id), None)
        if item is None:
            self.notify("该待确认项已不存在。")
            return
        options = [
            ("确认写入", ("approve", item_id, None)),
            ("改写后写入", ("edit", item_id, None)),
            ("拒绝", ("reject", item_id, None)),
        ]
        self.push_screen(PickScreen(f"[{item.topic}] {self._truncate(item.text, 44)}", options, self._pending_action))

    def _pending_action(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        action, item_id, _ = value
        if action == "edit":
            item = next((i for i in self.service.pending() if i.id == item_id), None)
            default = item.text if item else ""
            self.push_screen(
                TextInputScreen("改写后的完整表述", default=default, on_result=lambda v: self._pending_edited(item_id, v))
            )
            return
        self._pending_apply(action, item_id, None)

    def _pending_edited(self, item_id, text) -> None:
        if not text:
            self.query_one("#input", Input).focus()
            return
        self._pending_apply("approve", item_id, text.strip())

    def _pending_apply(self, action: str, item_id: str, override: str | None) -> None:
        try:
            if action == "approve":
                entry = self.service.approve(item_id)
                if override and override != entry.text:
                    entry = self.service.correct(entry.id, override, reason="人工改写后写入")
                message = f"已写入 [{entry.topic}] {entry.text}"
            else:
                item = self.service.reject(item_id)
                message = f"已拒绝 {item.id} {self._truncate(item.text, 40)}"
        except (KeyError, ValueError) as exc:
            self.add_card(str(exc))
            return
        self.add_card(message)
        self.refresh_pending()
        self.update_status()
        if self.service.pending():
            # 队列里还有：自动进入下一条，ESC 随时退出循环。
            self.call_after_refresh(self._open_pending)
        else:
            self.add_card("待确认队列已清空。")
            self.query_one("#input", Input).focus()

    def _show_topics(self, name: str | None = None) -> None:
        topics = self.service.topics()
        if not topics:
            self.add_card("记忆库为空。")
            return
        if name:
            needle = name.lower()
            matched = next(
                (t for t in topics if needle in t["topic"].lower() or needle in t["title"].lower()),
                None,
            )
            if matched is None:
                self.add_card(f"没有匹配的主题：{name}")
                return
            entries = self.service.store.entries(topic=matched["topic"])
            lines = [f"{matched['title']}（{matched['topic']}）· {len(entries)} 条", ""]
            for entry in entries:
                tag = "" if entry.status == "active" else f"（{entry.status}）"
                lines.append(f"{entry.id[:26]}  {self._truncate(entry.text, 56)}{tag}")
            self.add_card("\n".join(lines), "主题条目")
            return
        self.add_card(
            "\n".join(f"{t['topic']:<24} {t['title']} · {t['entries']} 条 · 更新 {t['updated']}" for t in topics),
            "记忆主题",
        )

    def open_session_picker(self) -> None:
        rows = visible_sessions(self.sessions.list(100, with_tokens=False))
        if not rows:
            self.add_card("没有可选择的会话。")
            return
        current_id = self.runtime.current.id if self.runtime.has_session else None
        self.push_screen(
            SessionPickerScreen(rows, current_id, self._picker_selected)
        )

    def _picker_selected(self, session_id) -> None:
        if session_id is None:
            self.query_one("#input", Input).focus()
            return
        if self.runtime.has_session and session_id == self.runtime.current.id:
            self.notify("已经是当前会话。")
            self.query_one("#input", Input).focus()
            return
        if self.busy:
            self._pending_switch = session_id
            return
        self._switch_session(session_id)

    def _resume_by_query(self, query: str) -> None:
        if self.busy:
            return
        rows = visible_sessions(self.sessions.list(100))
        needle = query.lower()
        matched = next(
            (row for row in rows if row["id"] == query or needle in row["title"].lower()),
            None,
        )
        if matched is None:
            self.notify("没有匹配的会话。")
            return
        if self.runtime.has_session and matched["id"] == self.runtime.current.id:
            self.notify("已经是当前会话。")
            return
        self._switch_session(matched["id"])

    def _show_status(self) -> None:
        if not self.runtime.has_session:
            self.add_card("新会话尚未创建，发送第一条消息后生成。")
            return
        info = self.runtime.status()
        percent = info["estimated_tokens"] / max(info["context_window"], 1)
        self.add_card(
            f"会话：{info['session_id']} · {info['title']}\n"
            f"模型：{info['model']}\n"
            f"检索：{info['retrieval']}\n"
            f"上下文：{format_tokens(info['estimated_tokens'])}/{format_tokens(info['context_window'])}（{percent:.0%}）\n"
            f"摘要：{info['summaries']} 段 / {info['summary_chars']} 字 · 已覆盖 {info['covered_records']} 条记录\n"
            f"待确认：{info['pending']} 条",
            "状态",
        )

    def _run_compact(self) -> None:
        self._set_busy(True)
        self.run_worker(
            self._compact_work, group="ask", thread=True, exclusive=True, exit_on_error=False
        )

    def _compact_work(self) -> None:
        try:
            events = self.runtime.force_compact()
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"压缩失败：{exc}")
            return
        self.call_from_thread(self._after_compact, events)

    def _after_compact(self, events: list[str]) -> None:
        for event in events:
            self.add_event(event)
        self.update_status()
        self._set_busy(False)

    def _run_remember(self, text: str) -> None:
        self._set_busy(True)
        self.run_worker(
            partial(self._remember_work, text),
            group="ask",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _remember_work(self, text: str) -> None:
        source = f"session:{self.runtime.current.id}:manual" if self.runtime.has_session else "manual"
        try:
            result = self.service.memorize(text, source=source)
            message = (
                f"写入 {len(result.admitted)} 条，待确认 {len(result.queued)} 条，"
                f"重复 {len(result.duplicates)} 条，拦截 {len(result.blocked)} 条"
            )
        except Exception as exc:
            message = f"写入失败：{exc}"
        self.call_from_thread(self._after_remember, message)

    def _after_remember(self, message: str) -> None:
        self.add_card(message)
        self.refresh_pending()
        self.update_status()
        self._set_busy(False)

    # ---------- 设置类选择器（逻辑在 actions.py） ----------

    def _open_models(self, refresh: bool) -> None:
        self._set_busy(True)
        self.run_worker(
            partial(self._models_work, refresh),
            group="ask",
            thread=True,
            exclusive=True,
            exit_on_error=False,
        )

    def _models_work(self, refresh: bool) -> None:
        try:
            options = actions.model_options(self.service, refresh=refresh)
        except Exception as exc:
            self.call_from_thread(self._finish_error, f"获取模型列表失败：{exc}")
            return
        entries = [(option.label, (option.provider, option.model), option.current) for option in options]
        entries.append(("↻ 刷新模型列表", ("__refresh__", ""), False))
        entries.append(("自定义模型…", ("__custom__", ""), False))
        self.call_from_thread(self._show_model_picker, entries)

    def _show_model_picker(self, entries: list[tuple]) -> None:
        self._set_busy(False)
        self.push_screen(
            PickScreen(f"当前模型：{self.runtime.model_label}", entries, self._model_picked)
        )

    def _model_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        provider, model = value
        if provider == "__refresh__":
            self._open_models(True)
            return
        if provider == "__custom__":
            current = self.service.config.llm
            self.push_screen(
                TextInputScreen("模型名称", default=current.model, on_result=self._model_custom)
            )
            return
        message = actions.apply_model(self.service, self.runtime, provider, model)
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _model_custom(self, name) -> None:
        if not name:
            self.query_one("#input", Input).focus()
            return
        current = self.service.config.llm
        message = actions.apply_model(self.service, self.runtime, current.provider, name)
        self.add_card(message)
        self.update_status()
        self.query_one("#input", Input).focus()

    def _pick_reasoning(self) -> None:
        options = actions.reasoning_options(self.service)
        if not options:
            self.add_card(f"{self.runtime.model_label} 不支持思考强度参数，可用 /models 换支持 reasoning 的模型。")
            return
        current = self.service.config.llm.reasoning_effort or ""
        entries = [(label, value, value == current) for value, label in options]
        self.push_screen(PickScreen(f"当前思考强度：{current or '默认'}", entries, self._reasoning_picked))

    def _reasoning_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        self.add_card(actions.apply_reasoning(self.service, value))
        self.query_one("#input", Input).focus()

    def _pick_context(self) -> None:
        current = self.service.config.chat.context_window
        entries = [(format_tokens(size), size, size == current) for size in actions.context_options(self.service)]
        entries.append(("自定义…", 0, False))
        self.push_screen(PickScreen(f"当前上下文：{format_tokens(current)}", entries, self._context_picked))

    def _context_picked(self, value) -> None:
        if value is None:
            self.query_one("#input", Input).focus()
            return
        if value == 0:
            current = self.service.config.chat.context_window
            self.push_screen(
                TextInputScreen("上下文长度（token）", default=str(current), on_result=self._context_custom)
            )
            return
        self.add_card(actions.apply_context(self.service, value))
        self.update_status()
        self.query_one("#input", Input).focus()

    def _context_custom(self, value) -> None:
        try:
            # isdigit 会放行 "①" 这类 Unicode 数字而 int() 抛错，改用 int 校验。
            size = int(value) if value else 0
        except ValueError:
            size = 0
        if size <= 0:
            self.add_card("上下文需要是正整数（token）。")
            self.query_one("#input", Input).focus()
            return
        self.add_card(actions.apply_context(self.service, size))
        self.update_status()
        self.query_one("#input", Input).focus()
