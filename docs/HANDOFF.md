# 开发交接

更新日期：2026-10-28。

## 本轮范围

本轮实现 [#10](https://github.com/Shardkiht/quiet-memo/issues/10)、[#11](https://github.com/Shardkiht/quiet-memo/issues/11) 的基础部分，并建立 [#14](https://github.com/Shardkiht/quiet-memo/issues/14) 的首批合成解析与裁决回归。未提高包版本，也未发布到 PyPI。

## 已实现

- 独立 `.venv` 与锁定依赖可安装，支持离线缓存；仓库提供 VS Code pytest 发现和调试配置。
- `MemoryContext` 包含可选事件日期、生效起止日期、时间原话、地点类型和显式情绪。日期支持年、月、日精度，检查非法日期和倒置区间。
- 普通抽取与导入使用同一上下文契约。缺少可靠时间锚点时只保留相对时间原话；语气推断不写入情绪事实。
- 上下文写在 Markdown 行尾 `context` JSON 标签中，沿用标签转义，旧格式无需迁移。候选排队、普通确认和 resolved 记录保留上下文；改写后确认及手动更正清除旧上下文。
- 正文和新增上下文共同经过凭据拦截。条目 ID、来源、软删除与替代关系保持现有语义。
- `MemoryService.propose_inference()` 只把假设追加到 `.quiet-memo/inferences.jsonl`，新记录为 `hypothesis`，包含类型、来源 ID 和带时区的生成/更新时间。
- 来源去重且必须是有效事实；同类型、同规范化文本、同证据集合重复登记返回原记录。推断不能支持另一推断，不进入 Markdown 事实库或普通召回。
- `evals/memory_eval.py` 提供固定合成输出的解析、冲突和上下文回归，并通过 `tests/test_memory_eval.py` 接入 pytest/CI；推断边界由独立领域测试覆盖。
- 离线验证发现并修复 token 截断在极小预算下超限的问题；字符估算分支按最终预算截断，tokenizer 分支避免截断产生无效 Unicode。
- 正式增加讯飞 Spark MaaS provider：配置向导、密钥环境变量、模型预设与 OpenAI 兼容调用；适配其采样默认值、输出上限以及 JSON 提示词约束。
- 修复 AI Markdown 回复无法选择复制：保留单控件绘制，通过带文本坐标的 Content 渲染选择高亮；覆盖流式、历史回复与窗口缩放。恢复会话成功不再弹出通知。

## 主要入口

| 文件 | 职责 |
| --- | --- |
| `memory/models.py` | 上下文和推断领域模型、字段校验 |
| `memory/extractor.py` | 普通抽取/导入的上下文契约 |
| `memory/store.py` | `context` 标签的读写与转义 |
| `memory/service.py` | 裁决、确认与推断证据检查 |
| `memory/inference.py` | 独立推断日志的追加和读取 |
| `evals/memory_eval.py` | 离线合成解析与裁决回归 |

以上源码路径相对 `src/quiet_memo/`，评测路径相对仓库根目录。

## 复现与调试

```bash
uv sync --frozen
uv run pytest
uv run pytest -m memory
uv run python evals/memory_eval.py
```

缓存齐全时可使用 `uv sync --frozen --offline`。网络受限环境可以下载与 `uv.lock` 对应的官方 wheel 并校验哈希，再用 `uv pip` 安装到项目 `.venv`；不要用全局包路径替代最终测试环境。共享缓存不可写时，项目默认的 `.cache/uv` 可独立使用。

VS Code 选择 `.venv` 后可从测试面板下断点，或使用 `.vscode/launch.json` 的当前文件/全量配置。不要运行真实 `qm` 会话验证测试：夹具会隔离配置、vault 和凭据，调试测试时也应保留这些护栏。

当前受限 Windows 执行环境禁止回环 TCP，默认 asyncio 在 `socketpair` 初始化时阻塞。测试专用开关 `QUIET_MEMO_TEST_PIPE_LOOP=1` 使用本地管道唤醒 Proactor，保持正常异步调度与跨线程回调；它不改变应用运行时或网络权限。普通终端与 CI 不必启用。当前环境同时用 `UV_FROZEN=true` 避免全局镜像配置触发重新解析依赖，用 `UV_OFFLINE=true` 复用已准备的锁定依赖。

## 本机手动测试

测试入口位于仓库外 `D:/workspace/qm-test/start-qm.cmd`，拥有独立虚拟环境与配置。包以 editable 方式直接指向本项目 `src`；修改保存后重启 qm 即可测试。此前复制的 `app` 目录不再使用。

测试 vault 是用户指定记忆库的独立副本，拥有自己的 Git 历史，无远程仓库；只复制长期记忆与人设，没有复制旧会话。新会话仅追加到副本的 `sessions/`。启动检查已验证原库文件哈希未变。

启动器设置进程环境变量 `QUIET_MEMO_CONFIG`、`QUIET_MEMO_VAULT`、`SPARK_API_KEY` 和 tokenizer 缓存。凭据仅存本地，不入库。受限执行环境无法写用户注册表；需要在普通终端直接运行 `qm` 时，用户运行测试目录中的 `configure-environment.cmd` 并重开终端。启动器本身不依赖持久环境变量。

## 验证记录

- 本地 Windows / Python 3.12.14，独立 `.venv`，126 个运行/测试依赖符合锁定版本，未借用全局工具环境。
- `uv sync --frozen --offline`：通过。
- `uv run pytest -q -x --tb=short -o faulthandler_timeout=45`：478 passed，147.57 秒；使用上述受限 Windows 测试环境开关。
- `uv run python evals/memory_eval.py`：24/24 通过。
- `git diff --check`：通过。

Spark 真实接口合成数据冒烟：流式回复、带时间/地点的记忆抽取、`read_topic` 工具结果回传均通过，共 4 次请求。当前环境不能由 Python 直接建立网络连接，因此保留项目 LiteLLM/OpenAI 的请求序列化和响应解析，通过本地文件转交已授权网络客户端发送原始请求；这验证了 API 接线和响应形状，未验证普通终端的直连网络或逐帧延迟。测试凭据和冒烟产物均在本地忽略路径；未发送个人记忆。

普通 Windows 事件循环与 Ubuntu 的最终验证以 GitHub Actions 双矩阵结果为准。

## 剩余工作与下一分支

- #10：为原始消息提供可靠时间锚点并解析相对时间；明确时区处理；推断情绪须进入独立假设流程。
- #11：用户查看、确认、拒绝与纠正；来源更正/删除后使已有推断失效；状态事件与损坏日志恢复。当前只声明后续状态枚举，不提供状态转换接口。
- #12：事实的历史/当前有效视图，时间范围与地点/情绪过滤或重排，回退及效果评估；目前上下文只参与抽取与存储。
- #14：在程序契约回归之外增加真实模型语义抽取评估、时间演化、无关注入率、过时事实与推断误用指标。任何真实模型冒烟先约定预算。
- Dreaming、画像和跨客户端主动召回继续按各自 Issue 推进。

用户要求：本轮基础功能验证通过并提交后，另开 feature 分支集中完成较难的后续功能，供用户评估后合入；不得自动合并该 feature 分支。
