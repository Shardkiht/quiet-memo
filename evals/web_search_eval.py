# -*- coding: utf-8 -*-
"""离线联网搜索质量基准（合成数据，不访问真实网络）。

两套基准：
- 固定候选池：每个查询给定各引擎的固定候选（人工标注 0/1/2 三级相关性），
  端到端跑 SearchRouter，衡量"候选池质量"与"管线输出质量"的差距。
- mock 策略场景：脚本化各引擎响应，统计请求数、早停行为与高置信误接受。

用法：
    uv run python evals/web_search_eval.py                # 打印当前指标
    uv run python evals/web_search_eval.py --save PATH    # 保存为基线
    uv run python evals/web_search_eval.py --compare PATH # 与基线对比

标注：2=能直接支持查询，1=仅背景相关，0=无关。人名/产品名均为合成或
公开技术测试词（如 Tibo/Codex），不含任何私人数据。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from quiet_memo.infra.search.base import SearchQuery, SearchResult
from quiet_memo.infra.search.router import SearchRouter


@dataclass(frozen=True)
class PoolItem:
    title: str
    url: str
    snippet: str
    grade: int  # 2 直接支持 / 1 背景相关 / 0 无关
    engine: str = "bing"
    published: str | None = None


@dataclass(frozen=True)
class Case:
    qid: str
    query: str
    category: str
    pool: tuple[PoolItem, ...]
    context: tuple[str, ...] = ()
    # 策略场景期望（Phase 2+ 用于断言；基线仅记录）
    expect_engines_requests: int | None = None  # auto 模式下期望的最大请求数上界
    expect_strong_stop: bool = False  # 首引擎有强匹配时应早停（1 次请求）


def it(title: str, url: str, snippet: str, grade: int, engine: str = "bing", published: str | None = None) -> PoolItem:
    return PoolItem(title=title, url=url, snippet=snippet, grade=grade, engine=engine, published=published)


# ---------- 固定候选池数据集（32 例） ----------

CASES: tuple[Case, ...] = (
    # ---- 短英文实体（含 context 消歧） ----
    Case("se1", "tibo", "short-entity", (
        it("OpenAI Codex overview", "https://openai.example/codex", "Codex is OpenAI's coding agent.", 0),
        it("AI industry weekly", "https://news.example/ai-weekly", "General AI news roundup.", 0),
        it("Tibo (Thibault Sottiaux) leads Codex", "https://bio.example/tibo", "Tibo is the Codex lead at OpenAI.", 2, "baidu"),
        it("Tibo 重置追踪站", "https://tracker.example/tibo", "Tibo 的 Codex 额度重置记录。", 2, "baidu"),
    ), context=("OpenAI", "Codex")),
    Case("se2", "tibo codex", "short-entity", (
        it("Codex resets tracker", "https://reset.example/codex", "Track Codex usage resets.", 1),
        it("Tibo Codex 28 天挑战", "https://reset.example/tibo-28", "Tibo 的 Codex 挑战记录。", 2),
        it("Codex 安装教程", "https://guide.example/codex-install", "How to install Codex.", 0),
    )),
    Case("se3", "thsottiaux codex", "short-entity", (
        it("街头挑战视频", "https://video.example/street-1", "娱乐视频，与查询无关。", 0),
        it("直播剪辑", "https://video.example/street-2", "娱乐直播剪辑。", 0),
        it("Thibault Sottiaux on X", "https://x.example/thsottiaux", "@thsottiaux announces Codex resets.", 2, "baidu"),
        it("Codex 负责人发声", "https://media.example/codex-lead", "Codex 团队负责人公开回应。", 1, "baidu"),
    )),
    Case("se4", "tibo 最近的公告", "short-entity", (
        it("OpenAI Codex release notes", "https://docs.example/codex-notes", "Codex changelog and release notes.", 1),
        it("Tibo 发布重置公告", "https://tracker.example/tibo-notice", "Tibo 官宣新一轮 Codex 重置。", 2, "baidu"),
    ), context=("OpenAI", "Codex")),
    Case("se5", "wjx-handler ai", "short-entity", (
        it("Handler pattern in AI apps", "https://patterns.example/handler", "Generic handler design pattern.", 0),
        it("AI handler libraries", "https://libs.example/ai-handlers", "List of handler libraries.", 0),
    )),
    Case("se6", "Tibo OpenAI", "short-entity", (
        it("OpenAI company page", "https://openai.example/about", "OpenAI official about page.", 0),
        it("Tibo 在 OpenAI 负责什么", "https://bio.example/tibo-openai", "Tibo 负责 OpenAI 核心产品与平台。", 2, "baidu"),
        it("Codex team structure", "https://docs.example/codex-team", "Codex team org chart.", 1, "baidu"),
    ), context=("OpenAI",)),

    # ---- 中文专有名词 ----
    Case("cn1", "智谱 GLM 更新", "cjk-proper", (
        it("智（汉语汉字）", "https://dict.example/zhi", "汉字智的基本解释。", 0),
        it("智的意思与拼音", "https://hanzi.example/zhi", "智字详解。", 0),
        it("智谱清言", "https://chat.example/zhipu", "智谱的对话产品主页。", 1),
        it("智谱发布 GLM 新一代模型", "https://ai.example/glm5", "智谱发布 GLM 新版本。", 2, "baidu"),
    )),
    Case("cn2", "文心一言 用户数", "cjk-proper", (
        it("文（汉字）", "https://dict.example/wen", "汉字文的基本解释。", 0),
        it("文心一言累计用户规模", "https://ai.example/ernie-users", "文心一言用户数据公布。", 2, "baidu"),
        it("百度文心一言发布", "https://news.example/ernie-launch", "文心一言发布会回顾。", 1, "baidu"),
    )),
    Case("cn3", "通义千问 开源", "cjk-proper", (
        it("义（汉字）", "https://dict.example/yi", "汉字义的解释。", 0),
        it("通义千问开源模型下载", "https://model.example/qwen-oss", "通义千问开源版本发布页。", 2, "baidu"),
    )),
    Case("cn4", "豆包 大模型", "cjk-proper", (
        it("豆包食谱", "https://food.example/douban", "豆包菜谱。", 0),
        it("豆包大模型正式发布", "https://ai.example/doubao", "豆包大模型能力介绍。", 2, "baidu"),
    )),
    Case("cn5", "Kimi 月之暗面", "cjk-proper", (
        it("Kimi 官网", "https://kimi.example", "Kimi 产品主页。", 1),
        it("月之暗面发布 Kimi 新版", "https://ai.example/kimi-moon", "月之暗面公司发布 Kimi 更新。", 2, "baidu"),
    )),

    # ---- 技术名词（应一次请求早停） ----
    Case("tt1", "LiteLLM tool calling documentation", "tech-term", (
        it("LiteLLM tool calling docs", "https://docs.example/litellm-tools", "LiteLLM tool calling reference.", 2),
        it("Getting started | LiteLLM", "https://docs.example/litellm-start", "LiteLLM getting started guide.", 1),
    ), expect_strong_stop=True),
    Case("tt2", "python asyncio tutorial", "tech-term", (
        it("Asyncio in Python docs", "https://docs.example/asyncio", "Official asyncio tutorial.", 2),
        it("Async patterns", "https://blog.example/async-patterns", "Common asyncio patterns.", 1),
    ), expect_strong_stop=True),
    Case("tt3", "rust ownership borrow checker", "tech-term", (
        it("Rust ownership chapter", "https://docs.example/rust-own", "The Rust book ownership chapter.", 2),
        it("Borrow checker explained", "https://blog.example/borrow", "Understanding the borrow checker.", 1),
    ), expect_strong_stop=True),
    Case("tt4", "docker compose healthcheck", "tech-term", (
        it("Compose healthcheck reference", "https://docs.example/compose-hc", "Docker Compose healthcheck config.", 2),
        it("Container health tips", "https://blog.example/health", "General container health tips.", 0),
    ), expect_strong_stop=True),
    Case("tt5", "textual python tui", "tech-term", (
        it("Textual TUI framework", "https://docs.example/textual", "Build TUIs with Textual.", 2),
        it("TUI design patterns", "https://blog.example/tui", "TUI design discussion.", 1),
    ), expect_strong_stop=True),

    # ---- 新闻时效 ----
    Case("nw1", "GLM 最新模型 发布", "freshness", (
        it("智（汉字）", "https://dict.example/zhi", "汉字解释。", 0),
        it("GLM-5 发布公告", "https://ai.example/glm5-release", "GLM 新模型发布公告。", 2, "baidu", "2026-10-20"),
        it("GLM 历史版本回顾", "https://ai.example/glm-history", "GLM 系列历史。", 1, "baidu", "2025-01-01"),
    )),
    Case("nw2", "codex reset latest", "freshness", (
        it("Codex resets tracker", "https://reset.example/codex", "Latest Codex resets.", 2, None, "2026-10-28"),
        it("Old Codex review", "https://blog.example/codex-old", "Early Codex impressions.", 0, None, "2025-03-01"),
    )),
    Case("nw3", "openai news today", "freshness", (
        it("OpenAI newsroom", "https://news.example/openai", "Today's OpenAI headlines.", 2, None, "2026-10-31"),
        it("OpenAI about", "https://openai.example/about", "Company background.", 1),
    )),
    Case("nw4", "智谱 最新 发布", "freshness", (
        it("智谱清言", "https://chat.example/zhipu", "产品主页。", 1),
        it("智谱最新发布会实录", "https://ai.example/zhipu-event", "智谱最新发布会内容。", 2, "baidu", "2026-10-25"),
    )),
    Case("nw5", "litellm release notes latest version", "freshness", (
        it("LiteLLM releases", "https://release.example/litellm", "Latest LiteLLM release notes.", 2, None, "2026-10-29"),
        it("LiteLLM old announcement", "https://blog.example/litellm-old", "Old intro post.", 0, None, "2024-02-01"),
    )),

    # ---- 歧义实体 ----
    Case("am1", "mercury planet facts", "ambiguous", (
        it("Mercury (planet) overview", "https://space.example/mercury", "Mercury is the smallest planet.", 2),
        it("Mercury element", "https://chem.example/hg", "Mercury is a chemical element.", 0),
    )),
    Case("am2", "julia programming", "ambiguous", (
        it("Julia language homepage", "https://lang.example/julia", "The Julia programming language.", 2),
        it("Famous people named Julia", "https://people.example/julia", "List of people named Julia.", 0),
    )),
    Case("am3", "orca whale", "ambiguous", (
        it("Orca (killer whale)", "https://ocean.example/orca", "Orcas are the largest dolphins.", 2),
        it("Orca software tool", "https://tools.example/orca", "A task runner named Orca.", 0),
    )),
    Case("am4", "swift language", "ambiguous", (
        it("Swift programming language", "https://lang.example/swift", "Apple's Swift language.", 2),
        it("Taylor Swift tour", "https://music.example/swift", "Concert information.", 0),
    )),

    # ---- 泛查询（不应被实体门控误杀） ----
    Case("gn1", "python 教程", "generic", (
        it("Python 官方教程", "https://docs.example/python", "Python 官方入门教程。", 2),
        it("编程语言排行", "https://rank.example/langs", "语言热度排行。", 0),
    )),
    Case("gn2", "编程入门", "generic", (
        it("编程入门指南", "https://learn.example/start", "零基础编程入门。", 2),
        it("键盘选购", "https://shop.example/keyboard", "机械键盘推荐。", 0),
    )),
    Case("gn3", "healthy breakfast ideas", "generic", (
        it("10 healthy breakfasts", "https://food.example/breakfast", "Healthy breakfast ideas.", 2),
        it("Restaurant reviews", "https://food.example/restaurants", "Local restaurant reviews.", 0),
    )),
    Case("gn4", "如何学习英语", "generic", (
        it("英语学习方法", "https://learn.example/english", "系统化英语学习路线。", 2),
        it("出国留学中介", "https://svc.example/study", "留学服务广告。", 0),
    )),

    # ---- 无结果 / 负例 ----
    Case("ng1", "tibo", "negative", (
        it("图书促销", "https://shop.example/books", "图书打折广告。", 0),
        it("天气查询", "https://weather.example", "天气预报。", 0),
    )),
    Case("ng2", "zzqqwxy protocol", "negative", ()),
    Case("ng3", "asdkjhg kitchen", "negative", (
        it("Kitchen design", "https://home.example/kitchen", "Kitchen remodeling ideas.", 0),
    )),
    Case("ng4", "fake-project-x release", "negative", (
        it("Project management tips", "https://work.example/pm", "Generic PM advice.", 0),
    )),
)


# ---------- mock 传输 ----------

class PoolTransport:
    """按 engines 参数返回固定候选池中对应引擎的结果；记录每次请求。"""

    def __init__(self, pool: tuple[PoolItem, ...], latency: float = 0.001):
        self.requests: list[tuple[str, tuple[str, ...], int]] = []
        self.latency = latency

    def search(self, query: str, limit: int, engines=None) -> list[dict]:
        self.requests.append((query, tuple(engines or ()), limit))
        time.sleep(self.latency)
        return []

    def fetch(self, url: str, max_chars: int) -> dict:
        raise NotImplementedError


class PoolProvider:
    """围绕固定候选池的假 provider：engines 过滤 + limit 截断，模拟真实上游。"""

    name = "open_websearch"
    capabilities = {"search", "fetch", "engines"}

    def __init__(self, pool: tuple[PoolItem, ...]):
        self.pool = pool
        self.calls = 0

    def search(self, query: SearchQuery) -> list[SearchResult]:
        self.calls += 1
        selected = query.engines or ("bing",)
        return [
            SearchResult(
                title=item.title, url=item.url, snippet=item.snippet,
                published=item.published, provider="open_websearch", engine=item.engine,
            )
            for item in self.pool
            if item.engine in selected
        ][: query.limit]

    def fetch(self, query) -> SearchResult:
        raise NotImplementedError


def run_pipeline(case: Case) -> tuple[list[SearchResult], int, bool, str]:
    """端到端跑一个 case，返回 (top_results, 请求次数, 是否高置信通过, hint)。"""
    provider = PoolProvider(case.pool)
    router = SearchRouter(
        {"open_websearch": provider}, order=["open_websearch"], engines=["bing"],
        fallback_engines=("baidu", "duckduckgo", "sogou"), max_engines=3,
    )
    outcome = router.search(SearchQuery(query=case.query, limit=5, context_terms=case.context))
    return outcome.results, provider.calls, outcome.hint == "" and bool(outcome.results), outcome.hint


# ---------- 指标 ----------

def dcg_at_5(grades: list[int]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(grades[:5]))


def ndcg_at_5(results: list[SearchResult], pool: tuple[PoolItem, ...]) -> float:
    grades_by_url = {item.url: item.grade for item in pool}
    gains = [grades_by_url.get(r.url, 0) for r in results[:5]]
    ideal = sorted((item.grade for item in pool), reverse=True)[:5]
    idcg = dcg_at_5(ideal)
    return dcg_at_5(gains) / idcg if idcg > 0 else 1.0


def grade_list(results: list[SearchResult], pool: tuple[PoolItem, ...]) -> list[int]:
    grades_by_url = {item.url: item.grade for item in pool}
    return [grades_by_url.get(r.url, 0) for r in results[:5]]


def naive_pipeline(case: Case) -> list[SearchResult]:
    """朴素基线：不做任何重排/回退，按首选引擎原始顺序截断（候选池质量参照）。"""
    primary = [i for i in case.pool if i.engine == "bing"]
    fallback = [i for i in case.pool if i.engine != "bing"]
    ordered = primary + fallback
    return [
        SearchResult(title=i.title, url=i.url, snippet=i.snippet, published=i.published, engine=i.engine)
        for i in ordered[:5]
    ]


def oracle_pipeline(case: Case) -> list[SearchResult]:
    """上界参照：全部候选按人工标注排序。"""
    ordered = sorted(case.pool, key=lambda i: -i.grade)
    return [
        SearchResult(title=i.title, url=i.url, snippet=i.snippet, published=i.published, engine=i.engine)
        for i in ordered[:5]
    ]


def evaluate(cases: tuple[Case, ...]) -> dict:
    per_case = []
    agg = {
        "pipeline": {"p5": [], "success5": [], "mrr5": [], "ndcg5": [], "false_accept": [], "requests": []},
        "naive": {"p5": [], "success5": [], "ndcg5": []},
        "oracle": {"p5": [], "success5": [], "ndcg5": []},
    }
    started = time.perf_counter()
    for case in cases:
        results, requests, confident, hint = run_pipeline(case)
        grades = grade_list(results, case.pool)
        p5 = sum(1 for g in grades if g >= 1) / 5
        s5 = 1.0 if 2 in grades else 0.0
        mrr = 0.0
        for i, g in enumerate(grades):
            if g == 2:
                mrr = 1.0 / (i + 1)
                break
        ndcg = ndcg_at_5(results, case.pool)
        false_accept = 1.0 if confident and not any(g >= 1 for g in grades) else 0.0
        agg["pipeline"]["p5"].append(p5)
        agg["pipeline"]["success5"].append(s5)
        agg["pipeline"]["mrr5"].append(mrr)
        agg["pipeline"]["ndcg5"].append(ndcg)
        agg["pipeline"]["false_accept"].append(false_accept)
        agg["pipeline"]["requests"].append(requests)

        naive = naive_pipeline(case)
        og = grade_list(naive, case.pool)
        agg["naive"]["p5"].append(sum(1 for g in og if g >= 1) / 5)
        agg["naive"]["success5"].append(1.0 if 2 in og else 0.0)
        agg["naive"]["ndcg5"].append(ndcg_at_5(naive, case.pool))

        oracle = oracle_pipeline(case)
        og = grade_list(oracle, case.pool)
        agg["oracle"]["p5"].append(sum(1 for g in og if g >= 1) / 5)
        agg["oracle"]["success5"].append(1.0 if 2 in og else 0.0)
        agg["oracle"]["ndcg5"].append(ndcg_at_5(oracle, case.pool))

        per_case.append({
            "qid": case.qid, "query": case.query, "category": case.category,
            "grades": grades, "requests": requests, "confident": confident,
            "hint": hint[:40], "ndcg5": round(ndcg, 4),
        })
    elapsed = time.perf_counter() - started

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    negative = [c for c in cases if c.category == "negative"]
    negative_qids = {c.qid for c in negative}
    false_accept_negative = [
        entry["confident"] and all(g < 1 for g in entry["grades"])
        for entry in per_case if entry["qid"] in negative_qids
    ]
    summary = {
        "cases": len(cases),
        "wall_seconds": round(elapsed, 3),
        "pipeline": {
            "p_at_5": mean(agg["pipeline"]["p5"]),
            "success_at_5": mean(agg["pipeline"]["success5"]),
            "mrr_at_5": mean(agg["pipeline"]["mrr5"]),
            "ndcg_at_5": mean(agg["pipeline"]["ndcg5"]),
            "false_accept_rate": mean(agg["pipeline"]["false_accept"]),
            "false_accept_negative_only": round(
                sum(1 for v in false_accept_negative if v) / len(false_accept_negative), 4
            ) if false_accept_negative else 0.0,
            "avg_requests_per_query": mean(agg["pipeline"]["requests"]),
        },
        "naive_pool_order": {
            "p_at_5": mean(agg["naive"]["p5"]),
            "success_at_5": mean(agg["naive"]["success5"]),
            "ndcg_at_5": mean(agg["naive"]["ndcg5"]),
        },
        "oracle_pool_order": {
            "p_at_5": mean(agg["oracle"]["p5"]),
            "success_at_5": mean(agg["oracle"]["success5"]),
            "ndcg_at_5": mean(agg["oracle"]["ndcg5"]),
        },
    }
    return {"summary": summary, "per_case": per_case}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--save", type=Path, help="把本次结果保存为基线 JSON")
    parser.add_argument("--compare", type=Path, help="与基线 JSON 对比")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()

    report = evaluate(CASES)
    if args.compare and args.compare.exists():
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        b = baseline["summary"]["pipeline"]
        c = report["summary"]["pipeline"]
        print("== pipeline 指标对比（基线 → 当前） ==")
        for key in ("p_at_5", "success_at_5", "mrr_at_5", "ndcg_at_5", "false_accept_rate", "avg_requests_per_query"):
            print(f"  {key}: {b[key]} -> {c[key]}")
        print("== 参照 ==")
        print(f"  naive_pool_order ndcg@5: {report['summary']['naive_pool_order']['ndcg_at_5']}")
        print(f"  oracle_pool_order ndcg@5: {report['summary']['oracle_pool_order']['ndcg_at_5']}")
    else:
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if args.json:
        print(json.dumps(report["per_case"], ensure_ascii=False, indent=1))
    if args.save:
        args.save.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"saved baseline to {args.save}")


if __name__ == "__main__":
    sys.exit(main())
