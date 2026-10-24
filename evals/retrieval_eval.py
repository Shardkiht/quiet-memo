"""检索评测：合成语料 + 固定用例，对比 keyword / vector / hybrid 三路。

语料按个人记忆库的特征建模（中文短条目、小节前缀、含"的"虚词噪声、
领域干扰项、短身份行、跨语言查询），不包含任何真实个人信息，任何
机器装好依赖后可复现同样数字。

用法：
    uv run python evals/retrieval_eval.py            # 全部三路（需 local-embed extra）
    uv run python evals/retrieval_eval.py --keyword  # 只跑 BM25，离线可用
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from quiet_memo.infra.config import Config, LLMConfig, RetrievalConfig
from quiet_memo.memory.service import MemoryService
from quiet_memo.retrieval import KeywordRetriever
from quiet_memo.retrieval.vector import LocalEmbedder, VectorRetriever

# ---------------------------------------------------------------------------
# 合成语料：目标是条目 + 各领域"的"字噪声干扰项
# ---------------------------------------------------------------------------

# (topic, 条目文本) —— 用例的命中目标，内容全部虚构
TARGETS = [
    # 咖啡与饮食
    ("food", "咖啡器具包括聪明杯、手磨 C2 和滤纸。"),
    ("food", "喜欢清爽、层次丰富、像茶一样的咖啡风味，中烘可以接受，不喜欢深烘焦苦。"),
    ("food", "用浅烘豆的记录：C2 20 格、18g 粉、180ml 水、90—92℃、3 分钟，放液后补 90ml。"),
    ("food", "连锁咖啡主要喝拿铁，不喝果茶；认为连锁纯咖啡口感较差。"),
    ("food", "基本不喝奶茶，提及价格与健康顾虑；不喜欢太甜，默认少糖或无糖。"),
    # 居住生活
    ("housing", "短租在地铁 1 号线起点，计划过渡后换长租。"),
    ("housing", "租房预算在 3000 元以内，要求独立卫浴。"),
    ("housing", "卧室装了感应夜灯，起夜不用开主灯。"),
    # 职业与求职
    ("career", "技术方向偏 Java 后端，正在准备秋招。"),
    ("career", "7 月曾在招聘平台海投约 1000 家，大多数已读不回。"),
    ("career", "两场小公司面试失利，暴露出数据库索引和并发的知识盲区。"),
    # 学业
    ("learning", "毕业设计做检索相关方向，导师 9 月才确定。"),
    ("learning", "10 月核对了毕业学分，还缺约 3.5 学分，预审已通过。"),
    ("learning", "就读于某地方院校，2023 年入学、预计 2027 年毕业。"),
    # 身份（短行 + 小节前缀，复现前缀稀释问题）
    ("identity", "01 基本身份与联系方式｜姓名：张三"),
    ("identity", "01 基本身份与联系方式｜年龄：22 岁，无确切出生日期"),
    ("identity", "01 基本身份与联系方式｜家乡：某省某市某镇"),
    # 健康与作息
    ("health", "身高 173cm、体重 67kg，自测有误差。"),
    ("health", "体测免测申请中，社会实践已完成。"),
    ("schedule", "作息较新：约早 6 点自然醒，晚 23 点多睡，午休可躺。"),
    # 设备
    ("devices", "键盘用 75 配列的客制化套件，茶轴。"),
    ("devices", "耳机是封闭式监听耳塞，通勤用入耳式动铁。"),
]

# 领域干扰项模板：含"的"与领域词，但与任何用例无关——模拟真实库的虚词噪声
_DISTRACTORS = {
    "food": [
        "当时的{place}的咖啡店的会员卡已经找不到了。",
        "同事推荐的{place}的甜品据说不错，还没有去过。",
        "公司楼下的便利店的{thing}经常缺货。",
    ],
    "housing": [
        "搬家的时候的{thing}的处理方式还没有想好。",
        "中介发来的房子的照片看起来不太真实。",
        "小区的物业的通知经常看漏。",
    ],
    "career": [
        "面试官提到的项目的细节当时没有记全。",
        "招聘软件上的职位的描述和实际的聊的经常对不上。",
        "简历里的经历的措辞改了很多遍。",
    ],
    "learning": [
        "课件里的公式的推导当时跳过了，后来也没有补。",
        "课程群的讨论的大部分内容与考试无关。",
        "图书馆的座位的预约规则改过一次。",
    ],
    "identity": [
        "证件照的重拍的截止时间在月底。",
        "表格里的信息的核对花了一个下午。",
    ],
    "health": [
        "体检的报告的纸质版还没有去取。",
        "药房的营业的时间与下班的路程顺路。",
    ],
    "devices": [
        "桌面的理线的方案改了两次还是不满意。",
        "充电器的大部分的接口都是旧的规格。",
    ],
}
_PLACE = ["公司旁边", "家附近", "地铁站里"]
_THING = ["储物柜", "快递柜", "置物架"]


def build_corpus() -> list[tuple[str, str]]:
    """确定性生成约 480 条语料：22 条目标 + 每领域约 60 条干扰。"""
    entries = list(TARGETS)
    for domain, templates in _DISTRACTORS.items():
        count = 0
        round_index = 0
        while count < 62:
            for template in templates:
                text = template.format(
                    place=_PLACE[(count + round_index) % len(_PLACE)],
                    thing=_THING[(count + round_index) % len(_THING)],
                ) + f"（记录 {count}）"
                entries.append((domain, text))
                count += 1
                if count >= 62:
                    break
            round_index += 1
    return entries


# ---------------------------------------------------------------------------
# 评测用例：(类别, 查询, 可接受子串列表；top-k 内命中任一即算相关)
# ---------------------------------------------------------------------------

CASES = [
    ("原词", "聪明杯", ["聪明杯"]),
    ("原词", "手磨 C2", ["C2"]),
    ("原词", "夜灯", ["夜灯"]),
    ("原词", "招聘平台 海投", ["1000 家"]),
    ("原词", "毕业设计", ["毕业设计"]),
    ("原词", "茶轴 键盘", ["茶轴"]),
    ("原词", "并发的知识盲区", ["并发"]),
    ("原词", "体测免测", ["体测"]),
    ("改写", "我喝咖啡喜欢什么口味", ["清爽", "像茶"]),
    ("改写", "我住的地方怎么安排", ["短租", "长租"]),
    ("改写", "我早上一般几点起床", ["早 6 点", "作息"]),
    ("改写", "我想找什么方向的工作", ["Java 后端"]),
    ("改写", "我的毕业设计进展", ["毕业设计", "导师"]),
    ("改写", "我用什么键盘", ["键盘"]),
    ("改写", "我身体有什么数据记录", ["身高", "体重"]),
    ("改写", "我在哪些平台投过简历", ["1000 家", "招聘平台"]),
    ("改写", "我不喜欢喝什么", ["奶茶", "深烘"]),
    ("身份", "我是谁", ["姓名：张三"]),
    ("身份", "我叫什么名字", ["姓名"]),
    ("身份", "我多大了", ["22 岁"]),
    ("身份", "我的家乡在哪里", ["家乡"]),
    ("数字", "我身高多少", ["173"]),
    ("数字", "我还缺多少学分", ["3.5 学分"]),
    ("数字", "投了多少份简历", ["1000 家"]),
    ("数字", "手冲咖啡的粉水参数", ["18g", "180ml"]),
    ("跨语言", "coffee flavor preference", ["清爽", "咖啡"]),
    ("跨语言", "my hometown", ["家乡"]),
]


def evaluate(store, retrievers: dict) -> dict:
    results: dict[str, dict] = {}
    for name, retriever in retrievers.items():
        p3 = p5 = 0
        mrr = 0.0
        per_category: dict[str, list[bool]] = {}
        failures = []
        for category, query, subs in CASES:
            hits = retriever.search(query, 10)
            rank = next(
                (i + 1 for i, hit in enumerate(hits) if any(s in hit.text for s in subs)),
                None,
            )
            ok3 = rank is not None and rank <= 3
            ok5 = rank is not None and rank <= 5
            p3 += ok3
            p5 += ok5
            mrr += 1.0 / rank if rank else 0.0
            per_category.setdefault(category, []).append(ok3)
            if not ok3:
                top = hits[0].text[:36].replace("\n", " ") if hits else "无结果"
                failures.append(f"  [{category}] {query} → {top}")
        results[name] = {
            "p3": p3 / len(CASES),
            "p5": p5 / len(CASES),
            "mrr": mrr / len(CASES),
            "per_category": per_category,
            "failures": failures,
        }
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="检索评测（合成语料，脱敏）")
    parser.add_argument("--keyword", action="store_true", help="只跑 BM25（离线）")
    parser.add_argument("--failures", action="store_true", help="打印未过 @3 的用例")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="qm-eval-") as tmp:
        vault = Path(tmp) / "vault"
        config = Config(vault=vault, llm=LLMConfig(), retrieval=RetrievalConfig(method="keyword"))
        service = MemoryService(config)
        service.init_vault()
        for topic, text in build_corpus():
            service.store.append(topic, text, source="eval", confidence=1.0)
        print(f"语料 {len(service.store.entries())} 条 · 用例 {len(CASES)} 例\n")

        retrievers: dict = {"keyword": KeywordRetriever(service.store)}
        if not args.keyword:
            if not LocalEmbedder.available():
                raise SystemExit("未安装 local-embed extra，先用 --keyword 或 uv sync --extra local-embed")
            embedder = LocalEmbedder("BAAI/bge-small-zh-v1.5")
            vector = VectorRetriever(service.store, embedder)
            retrievers["vector"] = vector
            retrievers["hybrid"] = _hybrid(service, vector)

        results = evaluate(service.store, retrievers)
        print(f"{'':<10}{'Recall@3':>10}{'Recall@5':>10}{'MRR@10':>9}")
        for name, r in results.items():
            print(f"{name:<10}{r['p3']:>9.0%}{r['p5']:>10.0%}{r['mrr']:>9.3f}")
        print("\n分类 Recall@3：")
        print(f"{'':<8}" + "".join(f"{name:>10}" for name in results))
        categories = [c for c, _, _ in CASES]
        for category in dict.fromkeys(categories):
            line = f"{category:<8}"
            for name, r in results.items():
                rows = r["per_category"][category]
                line += f"{sum(rows) / len(rows):>10.0%}"
            print(line)
        if args.failures:
            for name, r in results.items():
                if r["failures"]:
                    print(f"\n{name} 未过 @3：")
                    print("\n".join(r["failures"]))


def _hybrid(service: MemoryService, vector: VectorRetriever):
    from quiet_memo.retrieval import HybridRetriever

    return HybridRetriever(KeywordRetriever(service.store), vector)


if __name__ == "__main__":
    main()
