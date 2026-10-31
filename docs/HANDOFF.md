# 开发交接

更新日期：2026-10-31。代码版本 `0.3.2`，发行标记使用 `v0.3.2`；基础功能参照 `444fe0f`，文件导入与模型选择界面参照 `bef5199`。包版本、锁文件与 MCP 客户端标识保持一致；远端发布结果以 GitHub Release、Actions 与 PyPI 为准。

## 搜索质量优化（2026-10-31，base/chat-stability）

按《联网搜索质量优化实施方案》分阶段完成 P0（Phase 0-3），逐阶段提交：Phase 0/1 `d3ec502`、Phase 2 `79ccbb0`、Phase 3 `87a66fd`。

- 离线基准：`evals/web_search_eval.py`（33 条合成查询、固定候选池、PoolTransport mock；`--save/--compare/--json`），基线快照 `evals/web_search_baseline.json`。合成数据零个人信息，可公开提交。
- QueryAnalysis（`infra/search/quality.py`）：core/context/intent 三层分离——core 只来自原始查询并决定相关性；context 低权重消歧（0.5/0.25），不能单独令结果通过质量门控；时间词（今天/最新/latest…）与事件词（发布/官宣/更新…）不进 core。拉丁词元词边界匹配（`tibo` 不命中 `tibonacci`），CJK 按受控停用词/时间/事件词表轻量切分并保留短语序，≥4 字片段补二元词元；无高置信实体时 `ambiguous=True`。
- 渐进式搜索 + QualityGate：每次请求向上游拉 `fetch_limit = min(30, max(10, limit*3))` 条，进统一 `CandidatePool`（URL 去重、www. 宿主归一、保留各引擎名次证据与更完整标题/摘要）。早停条件：≥2 条标题级强命中，或单条标题覆盖全部 core 词，或单实体导航型查询 1 条强命中；都不满足才继续变体/候补引擎。低相关候选收尾时原样返回并附提示，核心词命中路径按成功语义缓存，零核心命中路径短缓存。
- 超时预算：`SearchQuery.timeout` 由 router 按剩余预算下发（`round(remaining,3)`），HTTP transport 逐请求生效、MCP transport 取 `min(self.timeout, timeout)`；预算计时仍用 `time.perf_counter`。
- 确定性 ranker：`ScoreBreakdown` 逐特征贡献（标题核心 3/词元、摘要 1/词元、全核心覆盖 +2、intent 0.5/词、context 0.5/0.25、引擎内名次 1.2/0.6/0.3、跨引擎交叉 +1.0/+1.5、时效查询有 published +0.3）；平分保持首次入池顺序；无日期不伪造。
- engine 归属：`_map_result` 逐条读取 daemon 的 `engine`/`source` 字段，router 只回填空值（不再一律覆盖成槽位引擎）；daemon `partialFailures` 有部分结果时转成结构化 `SearchFailure`（`provider.last_partial_failures` 通道），不再静默吞掉。
- 缓存键升到 v7（排序策略变化即换键）；低相关候选短缓存、核心命中正常 TTL、失败永不入缓存的政策不变。
- 质量对比（同一固定候选池 vs 基线）：nDCG@5 0.7387→0.8944（+21% 相对），Success@5 0.5455→0.7273，P@5 0.1879→0.2364，误接受率 0.1515→0.0606；平均请求数 1.48→2.30（弱命中查询按设计多试候补，强命中仍单请求早停）。全量 653 测试绿。
- 遗留（P1 待办）：`SearchQuery.lang/freshness` 是否被 open-websearch daemon 支持尚未核实，透传前先验证；`SearchDiagnostics` 结构化诊断未做；模糊实体泛匹配误接受（如 se5「wjx-handler ai」命中 handler/ai 泛词）为词法门控的已知边界，按任务书不引入 LLM rerank。

## 免费联网搜索增强（2026-10-31，base/chat-stability）

- 引擎候补链：首选引擎空结果、结果与查询核心词不相关或命中过弱（质量门控未达标）或请求失败时，按 `search.fallback_engines`（默认 baidu→duckduckgo→sogou）自动降级，最多 `search.max_engines`（3）个引擎，共享 `search.timeout_budget`（25s）超时预算且单次请求超时不超过剩余预算；显式 `search_engine` 保持单次请求语义不变。引擎回退只对声明 `engines` 能力的 provider 生效（OpenWebSearchProvider 两种 transport 均已声明），provider 级降级顺序保留。
- 查询变体：`search`/MCP `search` 新增 `context` 参数（对话中确认的专有名词，最多 5 个）；原始查询无有效结果时确定性派生最多 2 个变体（追加 context 词项，不改写原拼写），不调用 LLM。
- 缓存分级：真空结果短缓存 5 分钟、「今天/最新/latest」类时效查询成功结果 10 分钟、低相关候选 10 分钟；任何上游失败一律不入缓存。`SearchCache.set` 支持按条目覆盖 TTL（不超过实例 TTL）；键版本见上方"搜索质量优化"（当前 v7）。
- 结果处理：统一候选池去重合并后按确定性 ranker 排序截断（见上方"搜索质量优化"）；`SearchResult.engine` 记录实际来源引擎并进入 payload；零核心命中时如实提示"候补引擎未找到…以下为候选"。
- 工具上限：search 工具 limit 硬上限 10→15（schema 同步）；默认值仍来自 `search.max_results`。
- 计时注意：Windows `time.monotonic` 分辨率约 15.6ms，搜索预算计时用 `time.perf_counter`，勿改回。
- 已知上游限制：open-websearch 无验证码识别（上游弹验证码表现为空结果或跑题内容）；bing 对小众英文账号（如 thsottiaux）可能返回无关内容，依赖 baidu 候补兜底；baidu 返回的是跳转链接（baidu.com/link?url=...，QM 不主动解析，需经 fetch_web）；fetch_web 偶发 daemon HTTP 502。真实冒烟 7 条查询对比与 LLM 端到端在 be6d632 前后完成（冒烟脚本在系统临时目录，不入库）。

## 多厂商搜索适配（Phase B，2026-10-31，base/chat-stability）

- 外部厂商：`baidu_search`（qianfan，双鉴权头可配置待冒烟定夺）、`tavily`、`exa`（type 固定 auto）、`you`（X-API-Key，日期字段未核实保持 None）、`volcengine`（global/custom 双模式）。注册表在 `router._EXTERNAL_PROVIDERS`，Key 走环境变量（如 `BAIDU_SEARCH_API_KEY`），未配置 Key 静默跳过；适配器全部经 `http_client.JsonApiClient`（非 2xx 一律抛 `ProviderError`，错误 detail 只含状态码不回传响应体）。全部 MockTransport 测试覆盖，真实冒烟未做。
- 请求预算：`search.max_requests`（默认 9）为一次搜索跨所有 provider 共享的请求次数上限；查询变体只在第一家 provider 上执行。耗尽时返回池内最佳候选并附截断提示，该结果**不写入缓存**（缓存会丢截断提示，下次搜索应重新请求）。`ProviderErrorKind.BUDGET_EXHAUSTED` 表示本地预算耗尽，与厂商账户配额 `QUOTA`（402 等）严格区分。
- 交叉来源统计：`CandidatePool` 排名证据为 `(provider, engine, rank)` 三元组，corroboration 按 distinct (provider, engine) 计数——外部厂商 engine 为空时凭 provider 身份参与统计；`SearchResult.engine` 语义不变（上游引擎名）。
- 火山引擎 Query 官方上限 100 字符：超限抛 `ProviderError(BAD_RESPONSE)`，不静默截断。
- 待办（v1.2 方案卡 09-14）：独立 FetchRouter 与四家 Fetch 适配、`qm setup` 搜索 Key 菜单、配额保护、文档同步；五家统一受控真实冒烟（每家 ≤10 次）待授权执行，冒烟后固化 Baidu 鉴权头、You.com 日期字段、火山 Custom NeedContent 的真实行为。
- FetchRouter（卡 09）：抓取调度从 `SearchRouter.fetch` 抽到 `infra/search/fetch_router.py`，SearchRouter 保留兼容入口内部委托；新增 `[fetch]` 配置（`providers`，默认 `["open_websearch"]`，与 search.providers 独立，旧配置缺省即原行为）；agent 的 `fetch_web` 工具名/参数/返回结构不变，显式注入 search_router 的旧 DI 路径保留。
- 四家 Fetch（卡 10）：`TavilyFetchProvider`（/extract，Bearer，单 URL Basic）、`ExaFetchProvider`（/contents，x-api-key，body 恰为 {urls, text:true}）、`YouFetchProvider`（/v1/contents，X-API-Key，{urls, formats:["markdown"]}，响应形状二手资料待冒烟）、`VolcengineFetchProvider`（/search_api/fetch，字段名按 Search 同族约定待冒烟）。正文经 `truncate_fetch_content`（base.py）截断；空正文/失败抛 ProviderError 由 FetchRouter 回退。注册表 `fetch_router._EXTERNAL_FETCH_PROVIDERS`，Key 环境变量与对应 Search 相同。
- Key 读取（卡 11）：`config.read_api_key`（env 优先、Keyring 回退）统一供 search/fetch registry 与 setup 使用；`qm setup` 顶层分「模型与 LLM」/「搜索与网页抓取」两个向导（providers.py 的 run_setup/_run_llm_setup/run_search_setup），官方厂商不自动启用、无 Key 拒绝启用，TOML 不落 Key。测试用假 ask + 假 questionary + 假 keyring 脚本化交互。

## 忙碌态解耦与会话一致性（2026-10-31，分支 base/chat-stability）

- 分支约定：基础层修复与改进使用 `base/` 前缀；验证通过后经用户同意才分别合入 main 与 feature-memory-evolution（后者定位为记忆内容前沿版本）。
- 等待 LLM 期间输入保持可用、会话切换立即执行；等待期提交（消息与 /命令）进入全局 FIFO 队列，按提交顺序执行且任意时刻至多一个请求（并发第二个请求会被 exclusive worker 协作取消且 runtime.ask 无重入保护，故只能排队）；导航/只读命令（/new、/sessions、/exit 等）等待期直接执行。旧的全局 busy 布尔拆为 navigation_busy 与 ask_active 两态，输入框只在导航期间禁用。
- 数据一致性：`runtime.ask` 支持显式 session 并新增 on_session 回调；`_auto_recall`/`_flush_memories` 显式绑定请求会话，不再读 self.current；`last_plan` 带 session 标识，状态栏不串会话；后台任务执行时从磁盘新鲜加载会话对象，完成后若目标是当前会话则 reload 治愈内存快照；切走/新建时对生成中的会话延迟打 closed/distill_deferred 标记（保证排在最后的 assistant 记录之后）；生成期间跳过后台补沉淀，留给启动补记；删除守卫拒绝删除生成中或被队列引用的会话——物理删除后后台追加会原路重建僵尸文件（已用临时库脚本实证）。
- 切回生成中的会话：磁盘重建负责已完成轮次，界面只重放未落盘的活跃段（已完成段必然已随 assistant 记录落盘，天然去重）。
- 输入光标：原生光标仅用于支持 DECSCUSR 的终端（WT_SESSION/WT_PROFILE_ID/TERM_PROGRAM，conhost 退回自绘）；`on_resize` 重算 app.cursor_position，修复布局变化（折叠侧栏、窗口缩放）后硬件光标停在旧坐标的问题。rich 宽度表与终端在 emoji/歧义宽度字符上的渲染差异仍需真实 PowerShell 人工验证，headless 测试无法覆盖。
- 测试：新增 10 个回归（跨会话全局 FIFO 验收场景、等待期输入可用、立即切换、流式重放、关闭标记顺序、删除守卫、补沉淀跳过、光标终端门控与重算等）；同步修稳两个既有负载抖动测试——启动补记可能晚于测试打点触发 refresh_sessions（干扰计数与点击旧控件），driver.write 全量捕获会被帧渲染写入污染 `writes[-1]` 断言。
- 验证：全量 `uv run pytest` 592 passed 连续两轮；分支未合并、未推送。

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


## Review follow-up: queued targets and search budget

- Pending commands retain the full session-bound queue item and recheck its target after intervening navigation; synchronous completion resumes FIFO processing. Pending targets are covered by the session deletion guard.
- Search budget exhaustion stops further requests while retaining collected candidates with the existing candidate hint and timeout diagnostics.
- Validation: targeted queue/budget regressions 5 passed; full isolated suite 617 passed. No real model or network requests were used.


## Input caret end spacing

- BarInput removes Textual Input's extra end-of-value scroll cell from the terminal caret coordinate; scrolling retains its existing padding. Empty input and trailing user spaces retain their actual insertion positions. Regression coverage includes Chinese, mixed ASCII/CJK, middle edits and literal trailing spaces.
- Validation: insertion-coordinate cases and native cursor checks passed; full isolated suites base 623 passed, evolution working tree 651 passed. Real terminal/IME rendering remains a manual check.


## Generation cancellation and visible send queue

- Esc stops the active generation, preserving the draft and partial response in the append-only session. Open palettes/modals consume Esc first. TUI streaming uses cancellable async transport inside the worker, including first-token waits; no second request starts until cleanup completes.
- Enter during generation appends to the session-bound FIFO, shown above the input. Completion or cancellation drains it automatically. The queue is process-local, not persisted across exit.
- Active synchronous context preparation/tools finish safely; remaining tools receive cancelled results to keep tool-call pairs valid. Completed writes are not rolled back. UI text remains English.

- Validation: final full isolated suites base/main 632 passed, evolution working tree 672 passed. Installed LiteLLM async stream wiring tested with local mock responses, including repeated requests; cancellation covers first-token waits, partial output, draft preservation and tool-result pairing. No live model/network verification or evaluation-data changes. Evolution retains its pre-existing uncommitted work and memory-specific behavior.
