"""搜索质量优化的聚焦回归：查询分析与强实体约束（Phase 1 起步）。

全部使用假 provider 与合成标题/摘要，不发真实网络请求。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.search

from quiet_memo.infra.search.base import SearchQuery, SearchResult
from quiet_memo.infra.search.quality import analyze_query, result_relevance
from quiet_memo.infra.search.router import SearchRouter


class EngineProvider:
    """支持多引擎槽位的假 provider：按请求的引擎返回预置结果。"""

    name = "fake"
    capabilities = {"search", "engines"}

    def __init__(self, by_engine: dict[str, list[SearchResult]]):
        self.by_engine = by_engine
        self.calls: list[SearchQuery] = []

    def search(self, query: SearchQuery) -> list[SearchResult]:
        self.calls.append(query)
        engine = query.engines[0] if query.engines else ""
        return list(self.by_engine.get(engine, []))


def test_context_hit_alone_does_not_stop_engine_fallback():
    """T01：只含 context 词的结果不能让首选引擎早停，必须继续候补。"""
    provider = EngineProvider(
        {
            "bing": [
                SearchResult(
                    "OpenAI Codex changelog highlights",
                    "https://example.com/openai",
                    "OpenAI Codex weekly update roundup",
                )
            ],
            "baidu": [
                SearchResult(
                    "Tibo announces the roadmap",
                    "https://example.com/tibo",
                    "Tibo shared the announcement earlier today",
                )
            ],
        }
    )
    router = SearchRouter(
        {"fake": provider},
        engines=["bing"],
        fallback_engines=("baidu",),
        max_engines=2,
    )
    outcome = router.search(
        SearchQuery("Tibo 最近的公告", context_terms=("OpenAI", "Codex"))
    )
    assert any(query.engines == ("baidu",) for query in provider.calls)
    assert outcome.results[0].title.startswith("Tibo")
    assert outcome.results[0].engine == "baidu"
    assert outcome.hint == ""


def test_latin_term_requires_word_boundary():
    """T05：拉丁短实体按词边界匹配，不能命中偶然包含同字符的长单词。"""
    analysis = analyze_query("tibo")
    hit = SearchResult("Tibo announced the roadmap", "https://example.com/a", "")
    miss = SearchResult("The Tibonacci sequence explained", "https://example.com/b", "")
    assert result_relevance(hit, analysis) >= 3.0
    assert result_relevance(miss, analysis) == 0.0


def test_cjk_full_name_scores_above_partial_char():
    """T04：完整名称命中高于仅命中其中单字。"""
    analysis = analyze_query("智谱 GLM 更新")
    full = SearchResult("智谱发布 GLM 新版本", "https://example.com/a", "")
    partial = SearchResult("智者见智的启示", "https://example.com/b", "")
    assert result_relevance(partial, analysis) == 0.0
    assert result_relevance(full, analysis) > result_relevance(partial, analysis)


def test_freshness_and_intent_words_are_not_core_terms():
    """时间词与事件词单独归入 intent/freshness，不作为必须完整命中的核心词。"""
    analysis = analyze_query("Tibo 最近有什么新消息", context_terms=("OpenAI",))
    assert analysis.freshness_requested
    assert "tibo" in [term.text for term in analysis.core_terms]
    assert not any(
        term.text in ("最近", "消息", "什么", "新") for term in analysis.core_terms
    )
    assert "消息" in analysis.intent_terms
    assert [term.text for term in analysis.context_terms] == ["openai"]


def test_entity_free_query_is_ambiguous_and_not_gated():
    """整句停用词/时间词查询没有核心词：标记 ambiguous，不做实体门控。"""
    analysis = analyze_query("最近有什么消息")
    assert analysis.ambiguous
    assert not analysis.core_terms
    assert analysis.freshness_requested
    provider = EngineProvider(
        {"bing": [SearchResult("随便一条结果", "https://example.com/x", "")]}
    )
    router = SearchRouter({"fake": provider}, engines=["bing"])
    outcome = router.search(SearchQuery("最近有什么消息"))
    assert outcome.hint == ""
    assert len(outcome.results) == 1


def test_wide_query_results_pass_without_proper_entity():
    """T15：宽泛查询（Python 教程）不因专有名词门控丢弃全部有效结果。"""
    provider = EngineProvider(
        {
            "bing": [
                SearchResult(
                    "Python 入门教程",
                    "https://example.com/py1",
                    "面向初学者的 Python 教程",
                ),
                SearchResult(
                    "Python 进阶教程",
                    "https://example.com/py2",
                    "进阶 Python 教程合集",
                ),
            ]
        }
    )
    router = SearchRouter({"fake": provider}, engines=["bing"])
    outcome = router.search(SearchQuery("Python 教程"))
    assert outcome.hint == ""
    assert len(outcome.results) == 2


def test_context_terms_score_lower_than_core_terms():
    """context 只做低权重消歧，不能反超核心词命中。"""
    analysis = analyze_query("tibo announcement", context_terms=("openai",))
    core_only = SearchResult("Tibo profile", "https://example.com/a", "about Tibo")
    ctx_only = SearchResult("OpenAI profile", "https://example.com/b", "about OpenAI")
    assert result_relevance(ctx_only, analysis) > 0.0
    assert result_relevance(core_only, analysis) > result_relevance(ctx_only, analysis)


def test_ranking_is_deterministic_for_identical_candidates():
    """T20：相同候选重复排序结果一致（平分保持原始顺序）。"""
    analysis = analyze_query("tibo")
    candidates = [
        SearchResult("unrelated one", "https://example.com/1", ""),
        SearchResult("Tibo update", "https://example.com/2", ""),
        SearchResult("another unrelated", "https://example.com/3", ""),
        SearchResult("Tibo again", "https://example.com/4", ""),
    ]

    def ranked() -> list[str]:
        scored = [
            (result_relevance(item, analysis), index, item)
            for index, item in enumerate(candidates)
        ]
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [entry[2].url for entry in scored]

    assert ranked() == ranked()
    assert ranked()[0] == "https://example.com/2"
