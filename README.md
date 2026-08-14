# GitHub 密钥扫描器（github-key-scanner）

用正则 `sk-[a-z0-9]{32}` 在 GitHub 公开代码中搜索疑似泄露的大模型 API Key，
调用 AI 整理出密钥对应的**服务商、API_BASE_URL、置信度与备注**，存入 SQLite，
并通过 Web 页面可视化浏览、筛选与标记。

> ⚠️ 本项目仅用于**安全研究与自查**（检测自己/组织泄露的密钥、研究公开泄露情报）。
> 请勿将收集到的密钥用于任何未经授权的访问；所有数据来自 GitHub 公开代码，密钥
> 一经发现建议立即在原仓库或服务商控制台吊销。

## 功能特性

- 🔍 **GitHub 代码搜索**：字面量搜索词（默认 `sk-`，可配）+ **按语言分查**
  （`language:python / javascript / typescript / json / yaml / toml / ini`，覆盖
  `py / json / js / ts / toml / yaml / yml / ini` 等）+ **全局兜底查询**
  （覆盖 `.env / .conf / .cfg` 等无语言类型），`sort=indexed` 按**最新被索引
  （即最新上传/更新）**排序，命中后本地严格正则过滤出真正的密钥，自动处理分页与限速
  > 为什么不用 `path:` / `indexed:` 限定符？实测（2026-08）在部分 fine-grained
  > 令牌下二者被静默忽略（HTTP 200 但 0 命中），正则 `{32}` 精确量词也几乎不生效；
  > 故改为**字面量 + 语言分查 + 本地严格过滤**，时间窗口语义由本地文件指纹去重承担
- 🧠 **AI 分析**：提示词内嵌 **50+ 个常见 API / Coding Plan 端点库**（DeepSeek、智谱 GLM、
  GLM Coding Plan、Kimi、Kimi Coding Plan、通义 Qwen、豆包/火山方舟、Grok、OpenAI、
  Anthropic、Groq、硅基流动、OpenRouter、**AnyRouter、OpenCode Zen、OpenCode Go**、
  OneAPI 网关 等），AI 从中挑选最可能的 provider 与 API_BASE_URL，输出结构化 JSON
  （密钥 / 服务商 / API_BASE / 置信度 / 判断依据 / 分类）；**无法判断时 AI 可如实回复 unknown**
  （notes 说明原因），不硬猜
- 🔑 **多 Key 逐条独立**：一个文件可能包含多个密钥（可属不同服务商），AI 必须**逐条独立
  输出**，每条单独入库为一条记录，绝不合并
- 🏷️ **四类分类体系**：每条记录带 `category` 分类——**大模型（llm）/ 云服务（cloud）/
  其他服务（other）/ 未知（unknown）**；兼容 sk- 变体（sk-proj-/sk-ant-/sk-or-/sk-ar-）、
  Groq（gsk_）、xAI（xai-）、Tavily（tvly-）、ElevenLabs（sk_）、AWS（AKIA/ASIA）、
  GitHub（ghp_）等各类密钥格式，Web 端可按分类筛选
- 🔎 **本地预检机制**：提交 AI 前先本地扫描文件，命中服务商关键词（deepseek / glm /
  kimi / anyrouter / opencode …）与文件内 URL，把预检结果注入提示词，引导 AI 判断
- 📌 **文件 URL 优先**：文件中显式给出的 `base_url` / `BASE_URL` / `ANTHROPIC_BASE_URL`
  等端点**优先采用**（5 分），自动剥离 `/chat/completions`、`/models` 等动作路径段，
  并**补齐版本路径**（默认 `/v1`；智谱 `/api/paas/v4`、阿里 `/compatible-mode/v1`、
  豆包 `/api/v3` 等按端点库约定补全，提示词已引导 AI 直接输出完整版本路径）
- ✂️ **上下文长度限制**：单文件提交 AI 的上下文默认 **≤ 2000 token**，超长文件只截取
  每个密钥前后各 1200 字符的窗口（可配），而非提交全文
- 🔒 **AI 凭据脱敏**：疑似密钥在发送到第三方 AI 前替换为 `KEY_CANDIDATE_N` 占位符；
  AI 只分析变量名、URL 和上下文，结果在本地映射回候选值。AI 输出还会与本地提取结果对账，
  丢弃虚构项并用规则引擎补齐遗漏项
- 🛡️ **规则引擎兜底**：未配置 AI 时，按密钥前缀强信号 → 文件 URL → 局部窗口关键词
  启发式逐条判断（各密钥独立判定互不串线），保证流程可用
- 🗄️ **SQLite 数据库**：存储 API KEY、API_BASE_URL、原链接、仓库、路径、上下文、备注、
  置信度、分类、状态，自动去重（同一密钥重复扫描仅更新时间），旧库自动迁移补列
- ⏰ **全自动托管（daemon）**：`python main.py daemon` 按固定间隔（默认 2 小时）自动循环
  扫描；**增量扫描**（文件指纹去重：只处理新出现/重新索引的文件，时间窗口在本地判定）
  保证每轮不重复劳动；每轮扫描自动登记任务记录（范围/命中/新增/状态），
  Web 端可查看任务历史与游标，并支持一键『立即扫描』（写入触发文件，daemon 下轮立即执行）
- 🌐 **Web 展示**：统计卡片、扫描任务面板、分类/服务商分布、密钥表格（掩码/一键复制/详情）、
  多维筛选（关键词/分类/服务商/**置信度 ≥3分/≥4分/5分**/有效性）、GitHub 项目**一键新标签页打开**
  （不展示长 URL，行高清爽）、备注**点击展开**、大模型密钥**一键测试有效性**
  （请求 `/v1/models` 只读探测）、CSV 导出（含分类列，Excel 直接打开不乱码）；页面每 30 秒自动刷新
- 🎬 **演示模式**：无需 GitHub 令牌，内置示例数据一键跑通全流程；演示任务不会推进真实扫描的增量游标

## 目录结构

```
github-key-scanner/
├── main.py                  # CLI 入口：scan / daemon / export / stats / tasks / status
├── config.py                # 配置加载（.env）
├── gks/
│   ├── endpoints.py         # 50+ 常见 API / Coding Plan 端点库（AI 提示词素材）
│   ├── github_search.py     # GitHub 代码搜索（正则 + 排序 + 限速 + 全文抓取 + 时间窗口）
│   ├── prompts.py           # AI 提示词构造（内嵌端点库）
│   ├── analyzer.py          # AI 分析器 + 规则引擎兜底
│   └── database.py          # SQLite 数据层（密钥 + 任务记录 + 增量游标 + 文件去重）
├── web/
│   ├── app.py               # Flask 展示服务（含任务/状态/触发接口）
│   ├── templates/index.html
│   └── static/              # 前端样式与逻辑
├── tests/                   # 单元测试 + 演示数据
├── data/                    # 数据库、导出与触发文件（git 忽略）
├── Dockerfile               # 容器镜像（python:3.11-slim + tzdata + 非 root）
├── docker-compose.yml       # 一键部署：web + daemon 双容器共享 data 卷
└── .dockerignore            # 构建排除：data/、.env 等敏感内容不进镜像
```

## 快速开始

### 1. 安装依赖

需要 **Python 3.10+**（代码使用 `X | None` 类型语法）。

```bash
pip install -r requirements.txt
```

### 2. 配置（可选）

```bash
cp .env.example .env
```

| 配置项 | 说明 |
|---|---|
| `GITHUB_TOKEN` | GitHub 令牌，真实扫描必需。Settings → Developer settings → Fine-grained tokens，权限勾选 Public repositories 的 Contents: Read-only |
| `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | OpenAI 兼容接口，可填 DeepSeek / GLM / Kimi / 通义等任意服务；不填则自动降级为规则引擎 |
| `GITHUB_MAX_PAGES` | 每个语言查询的最大抓取页数（每页 100；默认 5 页/语言 ≈ 8 个查询 5 分钟） |
| `SEARCH_PATTERN` | 搜索字面量（默认 `sk-`，GitHub 端子串匹配，本地再严格过滤） |
| `SEARCH_GLOBAL_PAGES` | 全局兜底查询页数（默认 1，覆盖 .env/.conf/.cfg 等无语言文件；0 = 关闭） |
| `FETCH_FILE_CONTENT` | 是否拉取命中文件全文（更准，更耗配额） |
| `AI_MAX_TOKENS` | 单文件提交 AI 的 token 上限（默认 2000，超限截取密钥上下文） |
| `AI_MAX_FILES` | 每轮最多调用 AI 的文件数（默认 200）；超出部分自动改用规则引擎，不会跳过文件 |
| `KEY_CONTEXT_CHARS` | 截取时每个密钥前后保留的字符数（默认 1200） |
| `AI_TEMPERATURE` | AI 分析温度（默认 0.05，越低越稳定保守） |
| `AUTO_TEST_LLM` | 新增大模型密钥入库后自动 `/v1/models` 测试（默认 true，只读零消耗） |
| `SCAN_INTERVAL` | daemon 两轮扫描间隔秒数（默认 7200 = 2 小时） |
| `SCAN_INCREMENTAL` | 增量扫描开关（默认 true，只扫上次成功后新索引的文件） |

### 3. 演示模式（无需任何令牌）

```bash
python main.py scan --demo --ai-mode rules
```

### 4. 真实扫描

```bash
python main.py scan                    # 单轮扫描（默认增量：只处理上次成功后新索引的文件）
python main.py scan --full             # 强制全量重扫（忽略文件指纹去重，密钥级去重仍生效）
python main.py scan --since 2026-08-01 # 手动补扫：重扫指定时间窗口之后的文件
python main.py scan --max-pages 5      # 只扫 5 页（界定单轮范围）
python main.py scan --extensions py js toml yaml   # 自定义扩展名
```

### 5. 全自动托管（推荐）

```bash
python main.py daemon                  # 常驻循环扫描，默认每 2 小时一轮，Ctrl+C 退出
python main.py daemon --interval 3600  # 自定义间隔（秒）
python main.py daemon --demo           # 演示模式轮询（无需令牌，可先体验）
```

**单次扫描的范围如何界定？** 每轮扫描的边界由两件事共同限定：

1. **页数上限**：每个语言查询最多拉 `GITHUB_MAX_PAGES` 页（默认 5 页 ≈ 500 条），
   外加全局兜底查询 `SEARCH_GLOBAL_PAGES` 页；查询按 `sort=indexed` 最新优先，
   一轮最多处理数百个最新被索引的候选文件
2. **文件指纹去重**：`scanned_files` 表记录已处理文件（repo/path），
   窗口内已扫过的文件直接跳过；`last_seen < 窗口起点` 说明被重新索引 → 重扫；
   `--full` 或显式 `--since` 时忽略该去重（强制重扫/补扫）

> GitHub 的 `indexed:` 时间窗口限定符在部分令牌下不可用（实测 0 命中），
> 因此窗口语义由本地 `scanned_files` 指纹判定，GitHub 端只负责"最新优先"排序。

每轮结果写入 `scan_tasks` 任务表（时间/触发方式/窗口/命中/新增/更新/状态），
**只有成功完成的轮次才推进增量游标**（失败不丢窗口，下轮从原起点重试）。
可用 `python main.py tasks` 查看历史、`python main.py status` 查看游标状态。

> 开机自启（Windows）：任务计划程序新建任务 → 触发器"登录时"→ 操作"启动程序" →
> 程序填 `python`，参数填 `C:\...\github-key-scanner\main.py daemon`，起始于项目目录。

### 6. 启动 Web 页面

```bash
python web/app.py                      # 打开 http://127.0.0.1:8765
```

Web 页面顶部为**扫描任务面板**：显示上次扫描状态、增量游标、最近任务列表（窗口/命中/
跳过/新增/更新/耗时），点击『立即扫描』可唤醒正在等待的 daemon 并触发一轮；
页面每 30 秒自动刷新。

> Web API 会展示和导出数据库中的密钥原文。默认仅监听 `127.0.0.1`；在增加身份认证、
> HTTPS 和访问控制前，不要把 `WEB_HOST` 改为公网地址。

其他命令：`python main.py stats` 打印统计，`python main.py export` 导出 CSV 到 `data/keys.csv`。

### 7. Docker 一键部署（可选）

项目自带 `Dockerfile` + `docker-compose.yml`，一条命令同时启动 **Web 仪表盘** 与
**常驻 daemon 扫描器**，数据持久化在命名卷里，重启不丢：

```bash
# 1. 准备配置（填入 GITHUB_TOKEN / AI_API_KEY 等）
cp .env.example .env

# 2. 构建并启动（web + daemon 两个容器）
docker compose up -d --build

# 3. 打开仪表盘
#    http://127.0.0.1:8765
```

常用操作：

```bash
docker compose logs -f daemon     # 查看扫描日志
docker compose logs -f web        # 查看 Web 日志
docker compose restart daemon     # 重启扫描器
docker compose down               # 停止（数据保留在 keydata 卷）
docker compose down -v            # 停止并删除数据（慎用）
```

说明：

- 两个容器共享 `keydata` 数据卷（SQLite 数据库、扫描日志、触发文件），
  Web 端「立即扫描」同样可唤醒容器内的 daemon
- 镜像不打包 `.env` 与 `data/`（见 `.dockerignore`），密钥仅通过 `env_file` 注入，
  **不要把真实 `.env` 提交到仓库**
- compose 自动注入 `WEB_HOST=0.0.0.0` 使端口映射生效；如需自定义宿主机端口：
  `WEB_PORT=8888 docker compose up -d`（读 `.env` 中的 `WEB_PORT`）
- 容器时区为 `Asia/Shanghai`，扫描日志与任务时间与本地一致

## AI 分析说明

每命中一个文件，程序依次执行：**本地预检**（关键词命中 + 文件内 URL 提取）→
**长度控制**（超过 2000 token 时截取密钥前后各 1200 字符的窗口，可配）→
**本地脱敏**（把密钥原文替换为 `KEY_CANDIDATE_N`）→ 把文件路径、预检结果、
脱敏后的代码上下文 + 端点库表格发给 AI。AI 返回占位符结果后，程序在本地映射并对账：

```json
{"keys": [{
  "api_key": "sk-...",
  "provider": "DeepSeek",
  "api_base": "https://api.deepseek.com/v1",
  "confidence": 5,
  "notes": "文件显式指定 base_url，按文件采用（已补全 /v1）",
  "category": "llm"
}, {
  "api_key": "tvly-...",
  "provider": "Tavily",
  "api_base": "",
  "confidence": 2,
  "notes": "仅 TAVILY_API_KEY 变量名，无其他佐证",
  "category": "other"
}]}
```

- **多 Key 处理**：一个文件可返回任意数量的密钥对象，每条**独立入库**（各自一条记录），
  不合并、不截断列表；AI 遗漏时由本地规则引擎兜底补提
- **分类体系**：`category` 四选一 —— `llm`（大模型/Coding Plan）、`cloud`（云厂商凭据，
  如 AWS AKIA）、`other`（其他第三方服务，如 ElevenLabs `sk_`、Tavily `tvly-`、GitHub `ghp_`）、
  `unknown`（无法判断）；AI 未返回分类时程序自动按密钥格式/上下文推断
- **反欺骗原则**：提示词明确要求 AI **不被变量名欺骗**——变量叫 `OPENAI_API_KEY` 只是
  命名习惯，不是证据；必须以文件内**实际出现的 URL/域名、注释、SDK 调用、模型 ID** 为
  判断依据，无任何佐证时不得仅凭变量名给高置信度
- 判断依据（权重从高到低）：**文件显式给出的 URL → 代码调用形态（SDK 初始化/base_url 赋值）
  → 预检命中的关键词（有佐证）→ 注释/模型名 → 变量名（仅 2 分）**
- 文件显式给出的 URL 直接采用（5 分），自动剥离 `/chat/completions` 等动作路径段，
  并补齐版本路径（默认 `/v1`，智谱 `/api/paas/v4`、阿里 `/compatible-mode/v1` 等
  按端点库约定补全）
- 无明确线索时 AI 被允许如实回复 `unknown`（1 分，notes 说明原因），不硬猜
- **置信度 5 分制**：`1`=无任何有效痕迹 / `2`=仅变量名指向 / `3`=注释等疑似指向 /
  `4`=代码调用/配置明确指向（无 URL）/ `5`=文件含明确 URL 直接指向该 API；
  程序以 `分数/5` 存储（与旧 0~1 数据兼容），Web 端展示为 `X/5`
- 分析温度默认 0.05（`AI_TEMPERATURE` 可调），结果稳定保守
- AI 调用失败自动降级规则引擎（同样支持文件 URL 优先、前缀信号与关键词启发式），扫描不中断
- **入库自动测试**：新入库的大模型密钥自动请求 `{API_BASE}/v1/models`（只读、零消耗）
  探测有效性，`200→疑似有效`、`401/403→无效` 自动写入状态（`AUTO_TEST_LLM` 可关闭）

## 端点库速览（部分）

| 类别 | 服务商（API_BASE） |
|---|---|
| Coding Plan | 智谱 GLM（open.bigmodel.cn/api/paas/v4）、Kimi（api.moonshot.cn/v1）、DeepSeek（api.deepseek.com）、通义 Qwen（dashscope compatible-mode）、豆包（ark.cn-beijing.volces.com/api/v3）、OpenCode Go（opencode.ai/zen/go/v1） |
| 国内 | 百度千帆、讯飞星火、腾讯混元、MiniMax、阶跃星辰、百川、零一万物、硅基流动、书生浦语、摩尔线程 等 |
| 海外 | OpenAI、Azure OpenAI、Anthropic、Gemini、xAI Grok、Groq、Mistral、OpenRouter、**AnyRouter（anyrouter.dev/api/v1，sk-ar- 前缀）**、**OpenCode Zen（opencode.ai/zen/v1）**、Together、Fireworks、NVIDIA NIM、Cerebras 等 |
| 本地/网关 | Ollama、vLLM、LM Studio、LiteLLM、NewAPI/OneAPI 等 |

完整列表见 [`gks/endpoints.py`](gks/endpoints.py)，该列表会自动渲染进 AI 提示词。

## 常见问题

**为什么搜不到 / 报 401？** 代码搜索必须携带令牌（未登录匿名请求会被拒），且建议使用
fine-grained PAT 并勾选 Contents read 权限。若查询返回 200 但 0 命中，多半是
`path:` / `indexed:` 限定符被令牌静默忽略（本工具已改用字面量 + 语言分查绕开）。

**为什么这么慢？** GitHub 代码搜索 API 限速 **10 次/分钟**（认证用户），程序内置自动
限速等待与配额耗尽休眠；默认 8 条查询（7 语言 + 1 全局）约 1 分钟搜索，
随后逐文件抓取全文并调用 AI 分析（数十个文件约 10~30 分钟），耐心等待即可。

**密钥已存在会重复入库吗？** 不会，`api_key` 字段唯一，重复扫描只更新时间戳；
文件级也有指纹去重，已扫描过的文件不会被重复分析。

**daemon 每轮都全量扫描吗？** 不是。默认增量：查询按最新索引排序取前 N 页 +
文件指纹去重（本地窗口判定），每轮只处理新出现/重新索引的文件；
只有失败轮次不会推进游标。

**如何手动补扫历史窗口？** `python main.py scan --since YYYY-MM-DD`（自动忽略文件去重，
按密钥去重重扫该窗口）；或 `--full` 全量重扫当前窗口。

**一个文件有多个密钥怎么处理？** 逐条独立入库，互不合并；若属于不同服务商，
规则引擎通过"密钥前缀 + 关联变量"逐条判定，AI 模式下由 AI 逐条标注。

**怎么区分密钥类型？** 每条记录有 `category` 分类（大模型 / 云服务 / 其他服务 / 未知），
Web 端可直接按分类筛选；规则引擎按密钥前缀（AKIA→云服务、tvly-→其他服务等）与
上下文关键词自动归类。

**如何提高 AI 准确率？** 开启 `FETCH_FILE_CONTENT=true` 让 AI 看到完整文件；
在 `.env` 中配置更强的模型（如 `deepseek-reasoner`、`glm-4.6`、`kimi-k2`）；
超长文件可调大 `KEY_CONTEXT_CHARS` / `AI_MAX_TOKENS` 扩大提交窗口。

## 测试

```bash
python tests/test_core.py
```

## 免责声明

本项目仅用于合法的安全研究、自身泄露自查与公开情报收集。GitHub 上的公开代码仍受版权
与使用条款约束；对他人密钥的任何未授权使用均可能构成违法，后果自负。
