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


def test_fetch_limit_pulls_more_than_display_limit():
    """T08：默认展示 5 条也要向上游拉更多候选，正确答案在第 6 位也能进前五。"""
    results = [
        SearchResult(f"filler number {index}", f"https://example.com/{index}", "nothing")
        for index in range(5)
    ]
    results.append(
        SearchResult("Tibo explains the roadmap", "https://example.com/tibo", "full announcement")
    )
    provider = EngineProvider({"bing": results})
    router = SearchRouter({"fake": provider}, engines=["bing"], max_results=5)
    outcome = router.search(SearchQuery("tibo"))
    assert provider.calls[0].limit == 15  # min(30, max(10, 5*3))
    assert outcome.results[0].url == "https://example.com/tibo"


def test_weak_first_engine_result_does_not_stop_fallback():
    """T02：首引擎只有摘要级弱命中时不早停，继续候补找强命中。"""
    provider = EngineProvider(
        {
            "bing": [
                SearchResult(
                    "Weekly digest", "https://example.com/weak", "a quiet mention of tibo inside"
                )
            ],
            "baidu": [
                SearchResult(
                    "Tibo announces the roadmap",
                    "https://example.com/tibo",
                    "Tibo shared the announcement",
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
    outcome = router.search(SearchQuery("tibo"))
    assert any(query.engines == ("baidu",) for query in provider.calls)
    assert outcome.results[0].url == "https://example.com/tibo"
    assert outcome.hint == ""


def test_two_strong_hits_stop_after_first_request():
    """T03：首批已有两条强命中即早停，不消耗后续引擎预算。"""
    provider = EngineProvider(
        {
            "bing": [
                SearchResult("Tibo roadmap for codex", "https://example.com/1", "details"),
                SearchResult("codex notes from Tibo", "https://example.com/2", "more"),
            ]
        }
    )
    router = SearchRouter(
        {"fake": provider}, engines=["bing"], fallback_engines=(), max_engines=1
    )
    outcome = router.search(SearchQuery("tibo codex"))
    assert len(provider.calls) == 1
    assert outcome.hint == ""


def test_single_weak_pool_keeps_low_confidence_hint():
    """T16：候补池里没有任何核心词命中时，必须用提示标明低相关。"""
    provider = EngineProvider(
        {"bing": [SearchResult("Unrelated candidate", "https://example.com/c", "")]}
    )
    router = SearchRouter(
        {"fake": provider}, engines=["bing"], fallback_engines=(), max_engines=1
    )
    outcome = router.search(SearchQuery("tibo"))
    assert outcome.results
    assert "未找到" in outcome.hint


def test_multi_engine_request_preserves_real_engine():
    """T07：混合引擎请求里每条结果保留真实 engine，空的才补槽位引擎。"""
    results = [
        SearchResult("result one", "https://example.com/1", ""),
        SearchResult("result two", "https://example.com/2", ""),
    ]
    results[1].engine = "baidu"

    class MixedProvider:
        name = "fake"
        capabilities = {"search", "engines"}

        def __init__(self):
            self.calls: list[SearchQuery] = []

        def search(self, query):
            self.calls.append(query)
            assert query.engines == ("bing", "baidu")
            return [result.model_copy() if hasattr(result, "model_copy") else result for result in results]

    provider = MixedProvider()
    router = SearchRouter({"fake": provider})
    outcome = router.search(SearchQuery("q", engines=("bing", "baidu")))
    assert [item.engine for item in outcome.results] == ["bing", "baidu"]


def test_mapped_result_keeps_daemon_engine():
    """T07：daemon 逐条返回的 engine/source 必须映射进结果，不能全标成槽位引擎。"""
    from quiet_memo.infra.search.open_websearch import _map_result

    mapped = _map_result(
        {"title": "t", "url": "https://example.com/a", "engine": "baidu"}
    )
    assert mapped is not None and mapped.engine == "baidu"
    mapped_source = _map_result(
        {"title": "t", "url": "https://example.com/b", "source": "sogou"}
    )
    assert mapped_source is not None and mapped_source.engine == "sogou"


def test_request_timeout_bounded_by_remaining_budget(monkeypatch):
    """T14：单次请求超时上限取剩余预算，不能拿默认 10s 越过总预算。"""
    from quiet_memo.infra.search import router as router_module

    clock = [0.0]
    seen_timeouts: list[float | None] = []

    class TimedProvider:
        name = "fake"
        capabilities = {"search", "engines"}

        def search(self, query):
            seen_timeouts.append(query.timeout)
            clock[0] += 4.0
            return [SearchResult("filler page", f"https://example.com/{len(seen_timeouts)}", "")]

    monkeypatch.setattr(router_module.time, "perf_counter", lambda: clock[0])
    router = SearchRouter(
        {"fake": TimedProvider()},
        engines=["bing"],
        fallback_engines=("baidu", "duckduckgo"),
        max_engines=3,
        timeout_budget=10.0,
    )
    outcome = router.search(SearchQuery("tibo"))
    assert seen_timeouts == [10.0, 6.0, 2.0]
    assert all(timeout is not None and timeout <= 10.0 for timeout in seen_timeouts)
    assert not outcome.cached or outcome.results


def test_partial_failures_surfaced_with_results():
    """partialFailures 不因有部分结果被静默吞掉：转换成结构化失败随结果返回。"""
    from quiet_memo.infra.search.base import SearchFailure

    class PartialProvider:
        name = "fake"
        capabilities = {"search", "engines"}

        def __init__(self):
            self.last_partial_failures: list[SearchFailure] = [
                SearchFailure("bing", "rate_limit", "429 too many requests")
            ]

        def search(self, query):
            return [SearchResult("Tibo update", "https://example.com/t", "")]

    router = SearchRouter(
        {"fake": PartialProvider()}, engines=["bing"], fallback_engines=()
    )
    outcome = router.search(SearchQuery("tibo"))
    assert outcome.results
    assert any(failure.kind == "rate_limit" for failure in outcome.failed)


def test_engine_rank_evidence_breaks_relevance_ties():
    """同等相关时，引擎内名次靠前的候选排前（上游排序信号）。"""
    from quiet_memo.infra.search.router import CandidatePool

    analysis = analyze_query("tibo")
    second = SearchResult("Tibo shares update", "https://example.com/second", "")
    first = SearchResult("Tibo posts update", "https://example.com/first", "")
    pool = CandidatePool()
    pool.add([SearchResult("noise", "https://example.com/n", ""), second])
    pool.add([first])
    ranked = pool.ranked_entries(analysis)
    assert ranked[0].result.url == "https://example.com/first"


def test_cross_engine_corroboration_boosts_confidence():
    """多引擎独立返回同一 URL：交叉命中证据提升排序，排名证据保留。"""
    from quiet_memo.infra.search.router import CandidatePool

    analysis = analyze_query("tibo")
    corroborated = SearchResult("Tibo update", "https://example.com/a", "")
    corroborated.engine = "bing"
    single = SearchResult("Tibo update again", "https://example.com/c", "")
    single.engine = "bing"
    duplicate = SearchResult("Tibo update", "https://example.com/a", "")
    duplicate.engine = "baidu"
    pool = CandidatePool()
    pool.add([corroborated])
    pool.add([single])
    pool.add([duplicate])
    entry = next(item for item in pool.ranked_entries(analysis) if item.result.url.endswith("/a"))
    assert entry.score.corroboration > 0.0
    assert ("bing", 1) in entry.ranks and ("baidu", 1) in entry.ranks
    assert pool.ranked(analysis)[0].url == "https://example.com/a"


def test_published_boost_only_for_freshness_queries():
    """发布时间只在时效查询里加权；无 published 保持未知，不伪造日期。"""
    from quiet_memo.infra.search.quality import score_entry

    result = SearchResult("Tibo announcement", "https://example.com/a", "", published="2026-01-05")
    fresh = analyze_query("Tibo 最新消息")
    plain = analyze_query("Tibo announcement")
    assert score_entry(result, fresh).published > 0.0
    assert score_entry(result, plain).published == 0.0
    no_date = score_entry(
        SearchResult("Tibo announcement", "https://example.com/b", ""), fresh
    )
    assert no_date.published == 0.0


def test_score_breakdown_sums_to_total():
    """逐特征贡献可解释：各分量之和等于总分。"""
    from quiet_memo.infra.search.quality import score_entry

    analysis = analyze_query("tibo codex announcement", context_terms=("openai",))
    result = SearchResult(
        "Tibo codex announcement", "https://example.com/a", "openai notes"
    )
    breakdown = score_entry(result, analysis, ranks=[("bing", 1), ("baidu", 3)])
    parts = [
        breakdown.title_core,
        breakdown.snippet_core,
        breakdown.core_coverage,
        breakdown.intent,
        breakdown.context,
        breakdown.engine_rank,
        breakdown.corroboration,
        breakdown.published,
    ]
    assert abs(breakdown.total - sum(parts)) < 1e-9
    assert breakdown.title_core > 0.0 and breakdown.corroboration > 0.0


def test_www_host_variants_dedupe():
    """www. 前缀差异视为同一页面；其余路径参数谨慎不动。"""
    from quiet_memo.infra.search.router import CandidatePool, normalize_url

    assert normalize_url("https://WWW.Example.com/a/") == "https://example.com/a"
    pool = CandidatePool()
    pool.add([SearchResult("Tibo", "https://www.example.com/a", "")])
    pool.add([SearchResult("Tibo", "https://example.com/a", "")])
    assert pool.size == 1


def test_freshness_prefers_dated_result_without_fabricating_dates():
    """T10：时效查询里已解析日期轻度优先；无日期保持未知，不被填充。"""
    from quiet_memo.infra.search.router import CandidatePool

    dated = SearchResult("Tibo announcement", "https://example.com/a", "")
    dated.published = "2026-01-05"
    dated.engine = "bing"
    undated = SearchResult("Tibo announcement again", "https://example.com/b", "")
    undated.engine = "bing"
    pool = CandidatePool()
    pool.add([undated])
    pool.add([dated])
    fresh = analyze_query("Tibo 最新消息")
    assert pool.ranked(fresh)[0].url == "https://example.com/a"
    plain = analyze_query("Tibo announcement")
    # 非时效查询不按时间加权：保持入池顺序。
    assert pool.ranked(plain)[0].url == "https://example.com/b"
    # published 原样保留，未猜测/未改写。
    assert pool.ranked(fresh)[1].published is None


def test_baidu_redirect_link_kept_without_auto_follow():
    """T09：跳转链接原样保留，不主动追踪解析；解析只能走 fetch_web。"""
    from quiet_memo.infra.search.router import normalize_url

    link = "https://baidu.com/link?url=zzz"
    # 链接不被解析追踪：URL 原样保留（www. 前缀归一是仅有的宿主差异合并）。
    assert normalize_url(link) == link
    provider = EngineProvider(
        {"baidu": [SearchResult("Tibo announces the roadmap", link, "announcement")]}
    )
    router = SearchRouter(
        {"fake": provider}, engines=["bing"], fallback_engines=("baidu",), max_engines=2
    )
    outcome = router.search(SearchQuery("tibo"))
    assert outcome.results[0].url == link
    assert outcome.results[0].engine == "baidu"


def test_gate_stop_with_mixed_pool_reports_weak_share():
    """早停输出中多数条目与核心词无关时附提示，不让整批冒充相关结果。"""
    provider = EngineProvider(
        {
            "bing": [
                # 1 条强命中 + 2 条仅命中单字的噪音（模拟 bing 单字切词）。
                SearchResult("智谱清言", "https://example.com/zq", "智谱官方应用"),
                SearchResult("智（汉语汉字）_百度百科", "https://example.com/dict1", "智的意思"),
                SearchResult("智 zhì - 汉典", "https://example.com/dict2", "智的解释"),
            ]
        }
    )
    router = SearchRouter(
        {"fake": provider}, engines=["bing"], fallback_engines=(), max_engines=1
    )
    outcome = router.search(SearchQuery("智谱 最新消息"))
    assert outcome.results[0].title == "智谱清言"
    assert "无关" in outcome.hint


def test_single_strong_hit_stays_hint_free():
    """单条或少数强命中 + 个别弱候选是常态，不应误报提示。"""
    provider = EngineProvider(
        {
            "bing": [
                SearchResult("Tibo announces the roadmap", "https://example.com/a", "details"),
                SearchResult("Weekly digest", "https://example.com/weak", "a mention of tibo"),
            ]
        }
    )
    router = SearchRouter(
        {"fake": provider}, engines=["bing"], fallback_engines=(), max_engines=1
    )
    outcome = router.search(SearchQuery("tibo"))
    assert outcome.hint == ""


class NamedProvider:
    """可命名 provider：按 name 注册，用于跨 Provider 调度测试。"""

    capabilities = {"search", "engines"}

    def __init__(self, name: str, results: list[SearchResult]):
        self.name = name
        self.results = results
        self.calls: list[SearchQuery] = []

    def search(self, query: SearchQuery) -> list[SearchResult]:
        self.calls.append(query)
        return list(self.results)


def test_weak_first_provider_does_not_block_second_provider():
    """Phase A：第一家只有弱候选时不得收尾，第二家的强候选要能进前五。"""
    weak = NamedProvider(
        "weak_one",
        [SearchResult("Unrelated candidate", f"https://example.com/w{i}", "") for i in range(3)],
    )
    strong = NamedProvider(
        "strong_two",
        [
            SearchResult("Tibo announces the roadmap", "https://example.com/s1", "details"),
            SearchResult("Tibo release notes", "https://example.com/s2", "more"),
        ],
    )
    router = SearchRouter(
        {"weak_one": weak, "strong_two": strong},
        order=["weak_one", "strong_two"],
        engines=["bing"],
    )
    outcome = router.search(SearchQuery("tibo"))
    assert weak.calls and strong.calls
    assert outcome.provider == "strong_two"
    urls = [item.url for item in outcome.results[:5]]
    assert "https://example.com/s1" in urls and "https://example.com/s2" in urls
    # 强候选在前，弱候选如实提示（3/5 过半触发 weak_share_hint）。
    assert "无关" in outcome.hint


def test_all_providers_exhausted_finalizes_with_collected_pool():
    """全部 provider 遍历完才收尾：低相关候选如实提示，不丢第一家的候选。"""
    weak = NamedProvider(
        "weak_one",
        [SearchResult("Unrelated candidate", "https://example.com/w", "")],
    )
    empty = NamedProvider("empty_two", [])
    router = SearchRouter(
        {"weak_one": weak, "empty_two": empty},
        order=["weak_one", "empty_two"],
        engines=["bing"],
    )
    outcome = router.search(SearchQuery("tibo"))
    assert weak.calls and empty.calls
    assert [item.url for item in outcome.results] == ["https://example.com/w"]
    assert "未找到" in outcome.hint


def test_provider_budget_shared_across_providers(monkeypatch):
    """全局超时预算跨 provider 共享：第一家耗时耗尽后不再打第二家。"""
    from quiet_memo.infra.search import router as router_module

    clock = [0.0]

    class SlowProvider(NamedProvider):
        def search(self, query):
            self.calls.append(query)
            clock[0] += 30.0
            return list(self.results)

    weak = SlowProvider("weak_one", [SearchResult("Unrelated", "https://example.com/w", "")])
    second = NamedProvider("second", [SearchResult("Tibo roadmap", "https://example.com/s", "")])
    monkeypatch.setattr(router_module.time, "perf_counter", lambda: clock[0])
    router = SearchRouter(
        {"weak_one": weak, "second": second},
        order=["weak_one", "second"],
        engines=["bing"],
        timeout_budget=25.0,
    )
    outcome = router.search(SearchQuery("tibo"))
    assert weak.calls and not second.calls
    assert any(item.kind == "timeout" for item in outcome.failed)
