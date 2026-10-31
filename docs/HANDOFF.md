# 开发交接

更新日期：2026-10-31。代码版本 `0.3.2`，发行标记使用 `v0.3.2`；基础功能参照 `444fe0f`，文件导入与模型选择界面参照 `bef5199`。包版本、锁文件与 MCP 客户端标识保持一致；远端发布结果以 GitHub Release、Actions 与 PyPI 为准。

## 环境变量污染事件与抓取超时（2026-10-31）

- **污染事件**：`QUIET_MEMO_CONFIG`/`QUIET_MEMO_VAULT` 曾被 `setx` 持久化进 Windows 用户注册表（本应只在手动测试启动器进程内注入），导致此后所有新进程（含 TUI 会话）加载测试配置与测试 vault，期间的会话沉淀写入了测试库。已从注册表删除这两个变量。教训：多环境隔离只能用进程级环境变量或启动器包装，禁止 `setx` 等持久化写入；已在运行的长驻程序需重启才脱离旧环境。
- **数据处置**：污染窗口期写入测试库的沉淀按用户决定留在测试库，不抢救、不回滚。定位（2026-10-31 拍板）：本地一切（vault、本地 personal-memory 克隆）按测试环境对待，当前记忆标准源是远程 personal-memory 仓库，终态 vault 单源化未到。
- **抓取超时**：真实网络排查确认抓取目标站响应 24-42 秒且上游间歇 502，原默认 `search.fetch_timeout=25s` 会先于上游掐断约一半尝试；默认提升至 45s，config 数据类、加载 fallback、setup 模板、示例配置、双语 README 与测试同步（`6c24553`）。已有配置文件若显式写 25.0 需手动改为 45.0；未写该键则自动获得新默认。
- **搜索侧结论**：当次 search 失败为 Bing 间歇 TLS 失败后回退 Baidu 撞验证码，属上游瞬时问题，未改代码；daemon 日志中 request-filtering-agent 的 CIDR 告警是已捕获的良性警告（即此前记录的上游地址族比较问题）。
- fetch_timeout 相关提交未经本轮本地全量回归（由用户直接提交推送），以 CI 双矩阵结果为准；后续改动仍遵循"全量 pytest 绿了才提交"。

## 检索评测重跑（2026-10-30）

- main 已完成合成库与真实库三路检索评测：456 条合成记忆/27 个固定用例，882 条真实有效事实/24 个模型生成问题。合成题集为固定用例；真实题集由模型生成，不属于人工标注金标准。
- 基准结果：合成 keyword/vector/hybrid 的 MRR@10 为 0.722/0.913/0.913；真实三路为 0.889/0.814/0.842，真实 Hit@5 均为 95.8%。固定记忆解析与裁决用例 24/24。
- 有效期探针中 main 仍返回过期和未来事实，时间过滤尚未实现；无个人记忆意图的通用问题仍会得到非空结果，尚未验证最终回答是否误用。后续优先评估召回门控。
- 私有题集、逐题明细和原始耗时只在本地忽略目录，不写入交接文件或提交；本次未修改产品代码或个人库。方法与完整结果见 `evals/README.md`。

## 0.3.2 发布核对

- 版本覆盖 `pyproject.toml`、`__init__.py`、`uv.lock` 根包与联网搜索 MCP 客户端标识；新增功能说明已同步双语 README，Release 汇总从 `v0.3.1.1` 以来的变更。
- 本地已构建 wheel 与源码包，检查版本、`documents` extra 和新增模块均正确，未包含本地配置、vault、会话、缓存或虚拟环境。
- 启动自动提交和补沉淀的 UI 测试改为等待后台任务完成再验证，不再依赖固定刷新次数或短时轮询；功能断言保持原样。
- 发布前全量：`uv run --frozen --offline --extra documents pytest -q`，536 passed；`uv lock --check --offline` 通过。双平台 CI 和 PyPI 发布状态需通过远端记录核对。
- Release published 触发 `.github/workflows/workflow.yml` 自动测试、构建并通过 OIDC 发布 PyPI；仅推送版本提交或标签不会发布。

## 模型与供应商选择界面

- `/model` 列表仅展示模型，供应商操作不再作为模型条目混排。模型面板底部提示 `Ctrl+P` 打开独立管理菜单（添加 / 更新 Key / 移除），`Ctrl+R` 刷新，`Ctrl+N` 输入自定义模型名；管理菜单按 Esc 返回模型列表。
- `ModelPickerScreen` 复用现有选择器，单独约束模型列表高度，为搜索框和快捷键提示留空间。`ChatApp` 仅在模型选择器中重定向 Ctrl+P，其他界面仍使用 Textual 原命令面板。后台模型列表刷新仍校验原选择器身份，不能覆盖已打开的供应商管理界面。
- 本次只调整界面编排，保留配置、凭据存储与模型切换逻辑；使用隔离 UI 测试验证，不再消耗真实模型请求。
- 验证：模型选择、供应商管理、返回、快捷键、后台刷新隔离及两种窗口尺寸均通过。早期界面分类回归出现过现有启动自动 Git 提交用例超时，相关 12 项复测通过；提交前最终全量 `uv run --frozen --offline --extra documents pytest -q` 为 536 passed。若再次遇到后台 Git 时序波动，应先检查任务实际完成状态。

## 文件分析与导入

- 用户可粘贴/拖入绝对文件路径并用自然语言要求分析或导入记忆。带空格路径需引号，拖放路径转换取决于终端。仅本条用户消息中显式给出的支持格式文件可用；不沿用历史消息或文件内路径授权，不开放目录读取。
- Agent 工具 `read_file` 按字符游标分页并限制输出 token；`import_file` 每次抽取一块，默认 dry-run，用户明确要求写入长期记忆时才设 apply。返回下一块游标，步数用尽须说明未完成。预览和实际导入都会产生模型抽取请求，用户应了解费用。
- `/import <path> [--apply]` 与 CLI `qm import` 支持当前层目录；TUI 在 worker 运行，默认预览，失败释放 busy 并接续待切换意图。只读取/分析文件不会自动把全文作为个人记忆存储。
- `infra/documents.py` 读取常见文本，复杂格式复用 MarkItDown 可选 `documents` extra；20 MiB 文件上限，未启用插件、云端解析、OCR 或音视频转写。JSON/JSONL 保留原始角色和时间信息，不承诺专用客户端语义适配或跨块完整性。
- `agent/file_tools.py` 隔离文件路径与本轮解析缓存；文件内容脱敏后进入模型工具结果，导入保留文件名及块编号。工具只提供给内置 Agent，不扩展对外 MCP server。
- 新行为使用合成文件、固定抽取和假模型验证。2026-10-29 经授权使用 Spark 做了两轮共 10 次真实请求（达到本轮上限），分别验证显式指定工具、只提供路径与自然语言意图的 read_file / import_file 链路，以及助手建议未被误写为用户事实；保留独立测试库及会话，不读取个人库。不继承本轮调用预算，后续真实测试须按当前授权执行。最终测试记录以本轮实际结果为准。
- 文件导入初轮全量验证为 530 passed；合并模型选择界面优化后的提交前最终全量为 536 passed。真实本地转换覆盖 HTML、DOCX、XLSX、PPTX，其他格式通过统一适配契约测试，不能当作每一种真实文件均已验证。已有两处异步界面测试改为等待目标状态；命令面板按最长用法计算列宽。新增解析依赖已安装到项目环境，日常启动器使用当前项目源码；相关功能纳入 0.3.2。

## 基础版本范围（444fe0f）

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

手动测试使用独立虚拟环境与配置，包以 editable 方式指向项目源码；修改保存后重启即可测试。不在交接文档保存本机路径。

测试 vault 是用户指定记忆库的独立副本，拥有自己的 Git 历史，无远程仓库；只复制长期记忆与人设，没有复制旧会话。新会话仅追加到副本的 `sessions/`。启动检查已验证原库文件哈希未变。

启动器通过进程环境变量隔离配置、vault、凭据与 tokenizer 缓存；凭据和个人配置仅存本地，不入库。

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

## TUI 命令精简

按用户要求同步 main 和 evolution：仅保留 /sessions 会话入口，移除 /resume、/recall、/remember 的面板与分发及闲置处理函数。自然语言记忆仍通过 Agent recall/memorize 工具，CLI 管理入口保留。导入完成回调改为按用途命名；中英文 README 同步。

命令精简验证：main 全量 537 passed（375.37 秒）；首轮临时目录缺失导致的准备错误已修复并完整复测。 测试记录保留，git diff --check 通过。

## 搜索基础兼容同步（2026-10-30）

- 搜索与抓取属于两分支共用基础能力，修复同步 main 与 feature-memory-evolution；记忆演化、画像和自动回顾仍仅在功能分支领先，不合并整条功能分支。
- 域名解析默认兼容代理合成 DNS 段 `198.18.0.0/15` 与 `2001:2::/48`，自动启动的搜索 daemon 继承相同 `FAKE_IP_CIDRS`。显式设置覆盖默认值，空值关闭；已有外部 daemon 不自动重启，需手动同步环境。
- 合成 DNS 例外只用于域名解析结果，直接填写非公网 IP 仍拒绝，混合返回中存在其他私网地址仍拒绝，重定向终点继续复验。双语 README 与回归测试同步。
- 开发使用功能分支自己的虚拟环境与空测试库，显式覆盖继承的配置变量；全局命令继续引用 main。开发配置与入口均在忽略目录，不保存个人库或真实凭据。
- 本次验证：main 全量 556 passed；功能分支全量 619 passed。直接公开网页请求返回 HTTP 200，URL 校验通过；未运行经 daemon 的端到端抓取，测试环境未发现可用 npx 或运行中的服务。零模型请求，专属测试临时目录已清理。
- 后续已补齐 Node/npm/npx 与 open-websearch，自动启动改为优先已安装命令，缺失时才回退 npx。真实服务冒烟两分支均返回搜索结果并抓取到公开页面正文，取代上方“尚未验证”的旧边界；测试进程和日志已清理。open-websearch 2.2.0 的混合 IPv4/IPv6 CIDR 比较存在上游问题，本机安装包已修正为同地址族比较；此修补不在 Python 包内，升级上游服务时需复核。
- 安装与命令选择调整后的最终全量回归：main 558 passed，feature-memory-evolution 621 passed；真实搜索/抓取两个分支均通过，零模型请求，测试临时目录均已清理。
- Spark 联网 Agent 真实冒烟：main 2 次请求、feature-memory-evolution 3 次请求，共 5 次，关闭重试和候补模型。该次冒烟只向模型提供联网工具，未覆盖记忆工具与联网工具混合注册的生产场景。两边均由模型选择 search/fetch_web，实际执行搜索与两次网页抓取，工具结果回传后生成带来源链接的回答；使用空临时库，事实写入数为 0。直接抓取覆盖 tibo.cc、Python asyncio 文档、GitHub 仓库页、MDN HTTP 文档、中文维基百科，两个分支均成功；知乎公开主题页远端 HTTP 403，上游服务将其包装为 502，属于远端访问限制而非本地 DNS 拦截。未做浏览器渲染或登录态访问。测试服务已停止，临时会话与日志已清理；本轮未改产品代码。

- 两分支自动召回增加轻量个人话题/连续追问门控，通用问题跳过预取与查询改写；模型主动 recall 保留。规则存在隐式个人意图漏判边界，不将关键词判断视为完整语义分类。


## 请求进度与状态栏

- 发送后立即在模型名称旁显示活动条、阶段和经过秒数；上下文用量独立右对齐。活动条表示忙碌，不代表完成百分比。
- Agent 的可选 on_progress 回调在上下文准备、模型请求及工具执行前通知界面；工具开始与结果分别展示，异常仍走原有收尾释放 busy。
- 两分支同步基础体验；按需召回跳过通用问题，模型自身首字等待仍取决于供应商。
- 最终隔离全量回归（2026-10-31）：main 572 passed，feature-memory-evolution 635 passed；覆盖工具执行前进度、两种窗口宽度、状态栏卸载后的定时刷新。此次界面改动未做真实模型延迟基准，不将加载提示视为模型耗时缩短。


## TUI language and search errors

- Fixed TUI labels and progress are English; model/user content retains its original language.
- Search accepts JSON-encoded engine arrays from model arguments. Empty results with upstream partialFailures now raise an error and are not cached; default Bing HTTP failures fall back to Baidu once, explicit engines remain unchanged.
- Reproduced upstream Bing TLS failure returned in partialFailures with HTTP 200. Live tool smoke on both branches returned three results for default search and JSON-encoded Baidu arguments; temporary vaults removed, no model calls. Cache keys are versioned to bypass legacy false-empty entries.
- Validation: full isolated suite main 580 passed / evolution 643 passed; after the cache-key update, all 81 search tests passed on each branch. Fixed TUI string literals are checked for unintended Chinese text; user and model content are unaffected.


## Windows IME cursor

- Windows interactive TUI input uses a native blinking bar cursor, leaving IME composition placement to the terminal; headless and other platforms retain the painted cursor. Blur/unmount hide the native cursor and reset its shape. Native IME behavior requires manual terminal verification; headless tests only cover cursor control, editing coordinates and cleanup.
