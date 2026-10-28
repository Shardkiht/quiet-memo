# 检索评测

## 记忆解析与裁决回归

```bash
uv run python evals/memory_eval.py
```

`memory_eval.py` 使用固定合成模型输出和独立临时 vault，覆盖普通抽取结果、低置信与敏感候选、更新/冲突、重复、日期精度与非法日期、生效区间、未知相对时间、事件/居住地点、显式/推断情绪及凭据拦截。对应案例也在 `tests/test_memory_eval.py` 中随 pytest 和 CI 执行。

指标为各类固定案例的通过情况；它验证程序的解析、裁决与存储契约，**不衡量真实模型理解自然语言的准确性**，不代表长期聊天效果。推断去重、隔离和来源边界另由 `tests/test_inference.py` 覆盖。没有真实 vault、Key、付费模型或额外服务依赖。

## 检索基准

可复现的检索质量基准：合成语料 + 固定用例，对比 keyword（BM25）/ vector（本地向量）/ hybrid（RRF 融合）三路。

## 运行

```bash
uv sync --extra local-embed
uv run python evals/retrieval_eval.py               # 全部三路
uv run python evals/retrieval_eval.py --keyword     # 只跑 BM25（离线）
uv run python evals/retrieval_eval.py --failures    # 打印未过 @3 的用例
```

## 方法

- **语料**：456 条合成条目（脚本内确定性生成）。按个人记忆库特征建模——中文短条目、小节标题前缀、含"的"虚词噪声、领域干扰项、短身份行——不含任何真实个人信息。
- **用例**：27 例，分五类（原词 / 口语改写 / 身份 / 数字 / 跨语言）；top-3 内命中任一可接受子串记过。
- **指标**：Recall@3、Recall@5、MRR@10。

## 结果（2026-10-25，BAAI/bge-small-zh-v1.5 本地推理）

| 检索方式 | Recall@3 | Recall@5 | MRR@10 |
| --- | --- | --- | --- |
| keyword | 67% | 67% | 0.667 |
| vector | 89% | 89% | 0.815 |
| hybrid | **89%** | **89%** | 0.815 |

分类 Recall@3：hybrid 在原词 / 身份上与向量单路持平且由两路融合保障；数字类从 50% 升到 75%（hybrid 增加 keyword 命中的字面覆盖闸：查询的 CJK bigram 在命中条目覆盖率 < 0.3 视为虚匹配，不参与 RRF——此前干扰项的 BM25 高名次会把向量路的正确结果挤下去）。跨语言 50% 是 bge-small-zh 的模型局限，agent 查询改写已要求非中文问题补充中文说法，根治需换 multilingual embedding 模型。

## 真实库验证（脱敏，仅汇总指标）

在一个 797 条的真实个人记忆库上（用例集含真实姓名 / 地名，未随仓库发布）：

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| hybrid Recall@3 | 64% | 75% |
| hybrid MRR@10 | 0.569 | **0.745（三路最佳）** |
| hybrid Recall@5 | 71% | 79%（追平最佳） |

修复内容：批量导入在同一秒内生成条目 ID 发生生日碰撞（随机后缀仅 16 位），重复 ID 导致向量缓存矩阵与条目错位、结果串条。修复为随机后缀加宽到 32 位 + 追加时查重 + 向量同步按 ID 去重。

## 已知弱点

- 身份类超短条目（如"姓名：X"）受小节前缀稀释和同义词（姓名↔名字）影响，三路 Recall@3 均低。
- 数字类小样本（4 例）下 hybrid 会被共享前缀的同小节条目挤占。
