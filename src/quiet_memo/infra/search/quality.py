"""查询分析与确定性相关性评分（搜索质量优化 Phase 1）。

把“整句词元子串匹配”升级为分层打分：
- core_terms 只来自原始查询（拉丁专有名词按词边界、中文按轻量分词短语），
  是判定结果是否相关的唯一依据；
- context_terms 只做低权重消歧，不能单独令结果通过质量门控；
- 时间词/事件词归入 freshness/intent，不要求结果完整命中。
全部为确定性规则，不访问 LLM。
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .base import SearchResult

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
# 拉丁词元：字母数字开头结尾，允许内部 @ . _ -（账号、域名片段、版本号）。
_LATIN_RUN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_@.\-]*[A-Za-z0-9])?")

# 受控停用词：只收虚词/疑问词，不收可能成为实体的内容词。
_CJK_STOPWORDS = frozenset(
    "的 了 吗 呢 吧 啊 呀 是 在 有 和 与 对 就 还 都 也 很 我 你 他 她 它 这 那 "
    "什么 怎么 怎样 如何 为什么 哪 哪些 哪个 关于 请问 一下 介绍 详细 说说 看看 "
    "新 个 种 等".split()
)
# 时间词：命中即视为时效性查询，不进入核心词，也不猜测事实日期。
_FRESHNESS_WORDS = (
    "今天",
    "最新",
    "最近",
    "刚刚",
    "昨日",
    "昨天",
    "本周",
    "上周",
    "latest",
    "news",
    "today",
    "now",
    "breaking",
)
# 事件词：弱特征（发布/故障/价格等），命中只加分，不作为相关性门槛。
_INTENT_WORDS = (
    "发布",
    "官宣",
    "公告",
    "更新",
    "上线",
    "新闻",
    "消息",
    "价格",
    "故障",
    "版本",
    "release",
    "launch",
    "announce",
    "announcement",
    "update",
    "changelog",
    "outage",
    "incident",
)
_LATIN_STOPWORDS = frozenset(
    "the a an of in on for and or is are was were what how who when where why "
    "to do does did about with vs at by from".split()
)
# 从原始查询中剥离出 core_terms 的词（停用词 + 时间词 + 事件词）。
_REMOVED_WORDS = _CJK_STOPWORDS | frozenset(_FRESHNESS_WORDS) | frozenset(_INTENT_WORDS)

_FRESHNESS_PATTERN = re.compile(
    "|".join(re.escape(word) for word in _FRESHNESS_WORDS), re.IGNORECASE
)


# 拉丁词边界匹配：前后不能紧邻字母数字，避免 tibo 命中 tibonacci。
def _latin_pattern(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![0-9a-z]){re.escape(word)}(?![0-9a-z])")


@dataclass(frozen=True)
class QueryTerm:
    """参与匹配的词元；latin 用词边界，cjk 用子串。"""

    text: str  # 匹配用的小写形式
    kind: str  # "latin" | "cjk_phrase" | "cjk_gram"
    original: str = ""


@dataclass(frozen=True)
class QueryAnalysis:
    raw_query: str
    core_terms: tuple[QueryTerm, ...]  # 来自原始查询，决定相关性
    context_terms: tuple[QueryTerm, ...]  # 仅辅助消歧，不可独立令结果通过
    intent_terms: tuple[str, ...]  # 事件主题词（发布/故障/价格...）
    freshness_requested: bool
    ambiguous: bool  # 无高置信实体时提高回退保守度


def _split_cjk(run: str) -> list[str]:
    """按受控词表把连续中文段切成片段，保留原始词序。

    这是刻意从简的切分：不引入分词模型，只剥离虚词/时间词/事件词，
    剩下的片段（≥2 字）作为短语词元；更长片段补充二元词元兜底。
    """
    removed = sorted(_REMOVED_WORDS, key=len, reverse=True)
    segments: list[str] = []
    buffer: list[str] = []
    index = 0
    while index < len(run):
        for word in removed:
            if run.startswith(word, index):
                if buffer:
                    segments.append("".join(buffer))
                    buffer.clear()
                index += len(word)
                break
        else:
            buffer.append(run[index])
            index += 1
    if buffer:
        segments.append("".join(buffer))
    return [segment for segment in segments if len(segment) >= 2]


def _extract_terms(source: str) -> list[QueryTerm]:
    terms: list[QueryTerm] = []
    for raw in _LATIN_RUN.findall(source):
        token = raw.strip("@._-").lower()
        if len(token) >= 2 and token not in _LATIN_STOPWORDS:
            terms.append(QueryTerm(token, "latin", raw))
    for run in _CJK_RUN.findall(source):
        for segment in _split_cjk(run):
            # ≥2 字中文片段统一按短语权重匹配；更长片段再补二元词元兜底。
            terms.append(QueryTerm(segment, "cjk_phrase", segment))
            if len(segment) >= 4:
                seen: set[str] = set()
                for start in range(len(segment) - 1):
                    gram = segment[start : start + 2]
                    if gram not in seen:
                        seen.add(gram)
                        terms.append(QueryTerm(gram, "cjk_gram", gram))
    # 去重保序：同文本的词元只保留首个（短语优先于二元）。
    deduped: list[QueryTerm] = []
    seen: set[tuple[str, str]] = set()
    for term in terms:
        key = (term.text, term.kind)
        if key not in seen:
            seen.add(key)
            deduped.append(term)
    return deduped


def _match(term: QueryTerm, text_lower: str) -> bool:
    if term.kind == "latin":
        return _latin_pattern(term.text).search(text_lower) is not None
    return term.text in text_lower


def _intent_hit(word: str, text_lower: str) -> bool:
    if word.isascii():
        return _latin_pattern(word).search(text_lower) is not None
    return word in text_lower


def analyze_query(text: str, context_terms: Sequence[str] = ()) -> QueryAnalysis:
    raw = text.strip()
    core = _extract_terms(raw)
    core_texts = {term.text for term in core}
    context: list[QueryTerm] = []
    for term in context_terms:
        for extracted in _extract_terms(str(term)):
            if extracted.text not in core_texts:
                context.append(extracted)
    intent = [word for word in _INTENT_WORDS if _intent_hit(word, raw.lower())]
    # 高置信实体：拉丁专有名词或 ≥3 字中文短语；只有二元片段时视为模糊。
    confident = any(term.kind in ("latin", "cjk_phrase") for term in core)
    return QueryAnalysis(
        raw_query=raw,
        core_terms=tuple(core),
        context_terms=tuple(context),
        intent_terms=tuple(intent),
        freshness_requested=bool(_FRESHNESS_PATTERN.search(raw)),
        ambiguous=not confident,
    )


def has_core_hit(result: SearchResult, analysis: QueryAnalysis) -> bool:
    """结果是否命中任一核心词；核心词为空时不做门控。"""
    if not analysis.core_terms:
        return True
    title = result.title.lower()
    snippet = result.snippet.lower()
    return any(_match(term, title) or _match(term, snippet) for term in analysis.core_terms)


def result_relevance(result: SearchResult, analysis: QueryAnalysis) -> float:
    """确定性评分（无引擎证据时的总量）：标题核心命中 > 摘要 > 二元片段。"""
    return score_entry(result, analysis).total


@dataclass(frozen=True)
class ScoreBreakdown:
    """逐特征贡献；权重为内置常量，离线基准调参，排序稳定可复现。"""

    title_core: float = 0.0  # 标题核心词命中（最强证据）
    snippet_core: float = 0.0  # 摘要核心词命中
    core_coverage: float = 0.0  # 全部核心词覆盖奖励
    intent: float = 0.0  # 事件/主题词覆盖
    context: float = 0.0  # 上下文词命中（低权重，不单独通过门控）
    engine_rank: float = 0.0  # 上游引擎内名次信号
    corroboration: float = 0.0  # 多引擎独立交叉命中
    published: float = 0.0  # 发布时间（仅时效查询，有日期才计）

    @property
    def total(self) -> float:
        return (
            self.title_core
            + self.snippet_core
            + self.core_coverage
            + self.intent
            + self.context
            + self.engine_rank
            + self.corroboration
            + self.published
        )


def score_entry(
    result: SearchResult,
    analysis: QueryAnalysis,
    ranks: Sequence[tuple[str, int]] = (),
) -> ScoreBreakdown:
    """确定性多特征评分；每个特征单独记录贡献，便于解释与调参。"""
    title = result.title.lower()
    snippet = result.snippet.lower()
    title_core = snippet_core = 0.0
    for term in analysis.core_terms:
        if _match(term, title):
            title_core += 3.0 if term.kind != "cjk_gram" else 1.0
        elif _match(term, snippet):
            snippet_core += 1.0 if term.kind != "cjk_gram" else 0.5
    covered = sum(
        1 for term in analysis.core_terms if _match(term, title) or _match(term, snippet)
    )
    core_coverage = (
        2.0 if analysis.core_terms and covered == len(analysis.core_terms) else 0.0
    )
    intent = sum(
        0.5
        for word in analysis.intent_terms
        if _intent_hit(word, title) or _intent_hit(word, snippet)
    )
    context = 0.0
    for term in analysis.context_terms:
        if _match(term, title):
            context += 0.5
        elif _match(term, snippet):
            context += 0.25
    engine_rank = 0.0
    if ranks:
        best = min(rank for _, rank in ranks)
        engine_rank = {1: 1.2, 2: 0.6, 3: 0.3}.get(best, 0.0)
    distinct = len({engine for engine, _ in ranks if engine})
    corroboration = 0.0 if distinct < 2 else (1.0 if distinct == 2 else 1.5)
    # 时间优先级只对明确时效查询生效；没有 published 就保持未知，不猜测日期。
    published = 0.3 if analysis.freshness_requested and result.published else 0.0
    return ScoreBreakdown(
        title_core=title_core,
        snippet_core=snippet_core,
        core_coverage=core_coverage,
        intent=intent,
        context=context,
        engine_rank=engine_rank,
        corroboration=corroboration,
        published=published,
    )


def is_relevant(results: list[SearchResult], analysis: QueryAnalysis) -> bool:
    """保守判定：任一结果命中核心词即算相关；无核心词时不判定。"""
    if not analysis.core_terms or not results:
        return False
    return any(has_core_hit(result, analysis) for result in results)


@dataclass(frozen=True)
class GateDecision:
    should_stop: bool
    strong_count: int
    reason: str  # multiple_strong / navigational / no_core_terms / insufficient


def is_strong_hit(result: SearchResult, analysis: QueryAnalysis) -> bool:
    """高置信相关：高置信实体在标题命中，且核心词覆盖达到多数。

    摘要级弱命中永远不算强命中，防止“任意一处提到”就早停。
    """
    if not analysis.core_terms:
        return True
    title = result.title.lower()
    snippet = result.snippet.lower()
    title_hits = sum(1 for term in analysis.core_terms if _match(term, title))
    if not title_hits:
        return False
    covered = sum(
        1 for term in analysis.core_terms if _match(term, title) or _match(term, snippet)
    )
    # 覆盖一半以上核心词；只有 1 个核心词时标题命中即可（导航型查询）。
    return covered * 2 >= len(analysis.core_terms)


def quality_gate(ranked: list[SearchResult], analysis: QueryAnalysis) -> GateDecision:
    """渐进式搜索的早停判定：候选池足够好才停，不再“任词命中即返回”。"""
    if not analysis.core_terms:
        # 宽泛/无实体查询没有可强制的实体，有结果即停，避免误删全部结果。
        return GateDecision(bool(ranked), 0, "no_core_terms")
    strong = sum(1 for result in ranked if is_strong_hit(result, analysis))
    # 标题完整覆盖全部核心词的结果本身就是决定性命中，单条即可早停。
    full_title = any(
        all(_match(term, result.title.lower()) for term in analysis.core_terms)
        for result in ranked
    )
    if strong >= 2 or full_title:
        return GateDecision(True, strong, "multiple_strong" if strong >= 2 else "full_title")
    if strong >= 1 and len(analysis.core_terms) == 1:
        # 单实体导航型查询：一条标题级强命中已足够。
        return GateDecision(True, strong, "navigational")
    return GateDecision(False, strong, "insufficient")
