# AGENTS.md

本文件面向 AI Agent 和代码贡献者，记录开发约定。
面向用户的安装、命令和功能说明在 [README.md](README.md)，两者不要混写。
README 必须与当前代码架构保持一致；目录结构、依赖方向、CLI/TUI 命令、MCP 工具、配置项或存储格式变化时，必须在同一次改动中同步更新 README。

## 快速命令

```bash
uv sync
uv run pytest
uv run qm            # 直接进入对话
uv run qm setup      # 配置模型与 API Key
```

依赖只用 `uv` 管理；新增依赖写进 `pyproject.toml`，不要用 `pip install` 改本地环境。
全局 `qm` 是 editable 安装：只同步代码、**不同步依赖**。改动依赖后必须重跑
`uv tool install --force -e '.[local-embed]'` 重建工具环境，否则全局命令启动即崩（ModuleNotFoundError）。

## 技术栈

Python 3.12+、uv、hatchling、Typer、FastMCP、LiteLLM、Pydantic v2、pytest + pytest-asyncio（`asyncio_mode = "auto"`）、jieba + rank-bm25 + numpy（BM25 与向量余弦、RRF 混合检索）、fastembed（本地 embedding，可选 extra `local-embed`）、Textual（全屏 TUI）、rich（CLI 输出）、questionary（CLI 配置向导）、keyring、tiktoken。

非核心能力优先复用现成库（分词、检索、CLI、密钥、模型适配）；自研只保留核心差异：记忆裁决、上下文压缩、来源链。

## 分层

```text
app → agent → memory → retrieval → infra
memory/agent → llm → infra
```

- `app/`：Typer CLI、TUI、FastMCP 入口，只做参数解析和界面编排。
- `agent/`：Agent 循环、上下文压缩、工具注册、会话记录。
- `memory/`：抽取、裁决、存储、队列、审计。
- `retrieval/`：检索接口与实现，`Retriever` 是替换点。
- `llm/`：LiteLLM 适配、摘要。
- `infra/`：配置、供应商、Git、token 估算、终端交互。

新增代码不要制造新的反向依赖或循环依赖；跨模块使用相对导入。

## 代码风格

- 实现模块开头使用 `from __future__ import annotations`；公共函数和方法写类型标注。
- 代码标识符用英文；用户可见文案、CLI 帮助和错误信息用中文。
- 领域模型用 Pydantic，集中在 `memory/models.py`；轻量内部结果用 dataclass（如 `ToolOutcome`、`ToolSpec`）。
- 注释只解释非显而易见的约束和原因；TUI 布局、键位、写入裁决等重点位置保留简短注释。
- 单个源码文件控制在 800 行以内；能放进现有模块就不新建文件，不提前抽象。
- 只做任务要求的改动；不顺手重构，不为假想场景添加防御代码。

## 记忆与存储不变量

- `memories/<topic>.md` 是长期记忆的唯一事实来源。
- 文件头 front matter 只存文件级元数据：`topic`、`title`、`created`、`updated`。
- 条目是 Markdown bullet 行加行尾反引号标签；条目 ID 必须稳定。
- 更正采用“新条目 + `supersedes`”，旧条目置为 `superseded`；删除是软删除 `deleted`，不物理移除。
- 待确认候选写 `.quiet-memo/pending.jsonl`，处理结果追加到 `resolved.jsonl`；更正、删除等操作写 `audit.jsonl`。
- `.quiet-memo/vectors.npz` 是向量检索的派生缓存（含 `model_id`，换模型自动失效），vault 内 gitignore，可删除重建，不进 Git 历史。
- `sessions/<id>.jsonl` 是追加写的事件流，不重写旧行；上下文压缩只追加 `summary` 记录，不删除原文。
- `vault/SOUL.md` 是可选自定义人设：存在时替换系统提示词的人设段，机制规则固定拼接在后不可覆盖；它是指令不是记忆，不参与抽取与检索。
- 密钥、验证码、API Key 在写入前拦截；不把密钥写入 vault、配置示例或测试。
- 迁移 vault 存量数据用一次性脚本（放 `scripts/`，不随包发布）：**默认 dry-run** 出前后对照报告，`--apply` 才落盘；条目 ID 与行尾标签必须逐字节保留；落盘后自动验证（ID 集合一致、条数不变、无空正文）并提交 vault git 作为回滚点；向量缓存依赖哈希自动增量重建，不手动处理。
- `quiet-memo.toml`、`vault/`、`sessions/`、`.env`、密钥文件都不提交。

## 测试

- 新行为必须带测试，放在 `tests/` 下对应的领域文件中。
- 测试禁止读取本机 keyring、真实 API Key 或调用付费模型；使用 `tests/conftest.py` 的 fixture、`StaticExtractor` 和假模型。
- **测试环境隔离是夹具级红线**：conftest autouse 已把 `QUIET_MEMO_CONFIG`/`QUIET_MEMO_VAULT` 钉在每个测试的临时目录，任何测试和代码路径都不得绕过它直连本机配置/记忆库。把源码回退到旧版本再跑新测试是危险组合（旧路径可能绕过 mock 写真实文件），必须先确认隔离护栏在位。
- 未接真实 API Key 验证时，不声称“已完成线上验证”；真实调用只做冒烟（提前约定次数上限），解耦层的 mock 单测覆盖不了 service 接线的真实形状，关键链路补一次真实冒烟。
- **每次提交前运行全量 `uv run pytest`，全绿才允许 commit**，不要只跑改动文件。


## 可移植性

CI 在 ubuntu-latest 与 windows-latest 双矩阵运行，**本地全绿不等于 CI 绿**：

- 禁用平台专属编码与 API（如 `mbcs`）——ubuntu 上直接 LookupError，英文 Windows 上行为不同；用全平台内置的替代（如 `gbk`），确需平台分支时以 `sys.platform` 显式守卫。
- 文件路径、换行、终端转义同理：优先标准库可移植写法。

## 界面不变量

- 忙碌态（补沉淀/切换/请求中）收到的用户意图**必须排队接续，不得静默丢弃**；异常必须释放 busy 并把错误显示出来，不能让输入框永久禁用。
- 流式输出：循环级不做重试（重试会重复 delta）；完成时就地定稿，不重复挂载；中途断流保留半截并显式标注。
- git、LLM 等慢操作在 worker 线程执行，UI 线程只经 `call_from_thread` 接收回调。

## 文档同步

README 面向用户，必须始终与 `src/quiet_memo` 的当前架构一致。以下变化必须在同一次改动内更新 README，不能留到后续：

- README 有中英文两版：`README.md` 与 `README.en.md`；任何 README 改动都必须同步两版，并保持顶部语言切换链接。
- 模块、目录或依赖方向变化：更新“结构”树与依赖说明。
- 新增或删除 CLI 子命令：更新“命令行”示例。
- 新增或删除 TUI 斜杠命令：更新“会话内命令”表。
- 新增或删除 MCP 工具：同步 `app/server.py` 与 README 工具列表。
- 配置字段或默认值变化：同步 `config.example.toml`、README 配置示例与 `infra/config.py`。
- 存储格式或写入规则变化：更新 README 的“写入规则”和“结构”章节。
- README 不写会快速过期的固定数字（如测试数量），只写稳定的覆盖范围。
- 功能待办以 GitHub Issues 为准（创建时写清现状/方案/验收标准，完成时带验收记录关闭）；README Roadmap 只留条目级摘要并指向 Issues。
- README 呈现约束：不加装饰性 emoji；代码块里**不做依赖跨行字符对齐的 ASCII 排版**（GitHub 等宽字体无 CJK 字形，中文回退渲染必然错位），只保留框内自洽的元素。
- 约定本身变化时更新本文件。

## 脱敏

- 仓库内容（代码、测试、文档、评测、提交信息）不得出现任何真实个人信息：姓名、住址与地名、学校、雇主、联系人、私人邮箱、真实 API Key；类别以提到的为准，不限于清单。
- 评测与示例一律用合成数据（参考 `evals/retrieval_eval.py`：虚构条目 + 占位人物）；真实个人库只允许以汇总指标形式出现（Recall/MRR 等），不得引用条目原文或可定位到个人的用例。
- 在真实用户数据上调试或评测后，提交前先扫描再入库：`git grep` 工作区、`git log -S` 历史，确认敏感标识零残留。
- 本地 vault、会话文件、密钥永不入库（见"记忆与存储不变量"与 `.gitignore`）。

## 提交

- 沿用现有提交风格：`feat(scope): ...`、`fix(scope): ...`、`refactor(scope): ...`、`style(scope): ...`，主题行简短。
- 不提交生成物、本地配置或用户数据。
- 只有明确要求时才执行 `git push`。

## 边界

- 项目是本地优先：不引入遥测、云同步或额外外部服务，除非明确要求。
- 不通过修改用户 vault 里的历史内容来“修数据”；需要迁移时写一次性脚本并先说明影响。
- 不删除或覆盖用户未提交的改动。
