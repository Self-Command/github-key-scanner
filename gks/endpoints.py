# -*- coding: utf-8 -*-
"""
市面上常见的大模型 API / Coding Plan 端点库。

用途：
1. 作为 AI 提示词的"候选服务商名单"，供 AI 根据上下文挑选最可能的
   provider 与 API_BASE_URL；
2. 作为本地规则引擎的兜底判断依据（无 AI 配置时按域名匹配）。

字段说明：
- provider   服务商展示名（AI 输出以此为规范名）
- domain     判断用主域名（规则引擎按子串匹配）
- base_urls  常见 API_BASE_URL（第一个为主端点）
- key_hint   密钥格式特征，辅助判断
- coding_plan 是否提供 Coding Plan 套餐
- note       补充说明
"""
import re
from urllib.parse import urlparse

# 结构：provider -> {domain, base_urls, key_hint, coding_plan, note}
ENDPOINTS: list[dict] = [
    # ================= 国内服务商 =================
    dict(provider="DeepSeek", domain="api.deepseek.com",
         base_urls=["https://api.deepseek.com", "https://api.deepseek.com/v1", "https://api.deepseek.com/beta"],
         key_hint="sk-[a-z0-9]{32}", coding_plan=True,
         note="DeepSeek API：deepseek-chat / deepseek-reasoner；/beta 为兼容端点；Coding Plan 同端点计费"),
    dict(provider="智谱 GLM", domain="bigmodel.cn",
         base_urls=["https://open.bigmodel.cn/api/paas/v4"],
         key_hint="sk- 或 id.xxx / 数字.xxx", coding_plan=True,
         note="智谱开放平台：GLM-4.5 / GLM-4.6 / GLM-Z1 等；GLM Coding Plan 与智能体走同一端点"),
    dict(provider="智谱 CodeGeeX", domain="codegeex.cn",
         base_urls=["https://codegeex.cn"],
         key_hint="", coding_plan=True,
         note="智谱旗下代码模型 CodeGeeX 系列"),
    dict(provider="Kimi 月之暗面", domain="api.moonshot.cn",
         base_urls=["https://api.moonshot.cn/v1"],
         key_hint="sk-[a-z0-9]{32}", coding_plan=True,
         note="Moonshot：kimi-k2 / kimi-latest；Kimi Coding Plan 同端点（kimi-k2-0711-preview 等）"),
    dict(provider="通义千问 Qwen", domain="dashscope.aliyuncs.com",
         base_urls=["https://dashscope.aliyuncs.com/compatible-mode/v1"],
         key_hint="sk-[a-z0-9]{32}", coding_plan=True,
         note="阿里云百炼：qwen-max / qwen-coder-plus 等；Qwen Coding Plan 走 compatible-mode"),
    dict(provider="阿里云百炼国际版", domain="dashscope-intl.aliyuncs.com",
         base_urls=["https://dashscope-intl.aliyuncs.com/compatible-mode/v1"],
         key_hint="sk-", coding_plan=False,
         note="阿里云国际版 Model Studio"),
    dict(provider="豆包/火山方舟", domain="volces.com",
         base_urls=["https://ark.cn-beijing.volces.com/api/v3"],
         key_hint="sk- 或 ARK 资源 ID", coding_plan=True,
         note="字节跳动火山引擎：doubao-pro / doubao-1.5-pro / doubao-seed 等；Coding Plan 同端点"),
    dict(provider="百度千帆", domain="qianfan.baidubce.com",
         base_urls=["https://qianfan.baidubce.com/v2"],
         key_hint="AK/SK 或 Bearer", coding_plan=False,
         note="百度文心 ERNIE 系列，OpenAI 兼容模式 /v2/chat/completions"),
    dict(provider="讯飞星火", domain="spark-api-open.xf-yun.com",
         base_urls=["https://spark-api-open.xf-yun.com/v1"],
         key_hint="", coding_plan=False,
         note="科大讯飞：Spark 系列，OpenAI 兼容端点"),
    dict(provider="腾讯混元", domain="hunyuan.cloud.tencent.com",
         base_urls=["https://api.hunyuan.cloud.tencent.com/v1"],
         key_hint="", coding_plan=False,
         note="腾讯云混元大模型，OpenAI 兼容端点"),
    dict(provider="MiniMax", domain="api.minimax.chat",
         base_urls=["https://api.minimax.chat/v1", "https://api.minimaxi.com/v1"],
         key_hint="", coding_plan=False,
         note="MiniMax：abab / MiniMax-Text 系列；海螺问答"),
    dict(provider="阶跃星辰 StepFun", domain="api.stepfun.com",
         base_urls=["https://api.stepfun.com/v1"],
         key_hint="sk-", coding_plan=True,
         note="StepFun：step-2 / step-1x 系列，提供 Coding Plan"),
    dict(provider="百川智能 Baichuan", domain="api.baichuan-ai.com",
         base_urls=["https://api.baichuan-ai.com/v1"],
         key_hint="sk-", coding_plan=False,
         note="Baichuan 系列模型"),
    dict(provider="零一万物 Yi", domain="api.lingyiwanwu.com",
         base_urls=["https://api.lingyiwanwu.com/v1"],
         key_hint="sk-", coding_plan=False,
         note="Yi-Lightning / Yi-1.5 系列"),
    dict(provider="硅基流动 SiliconFlow", domain="api.siliconflow.cn",
         base_urls=["https://api.siliconflow.cn/v1"],
         key_hint="sk-[a-z0-9]{32}", coding_plan=False,
         note="聚合平台：DeepSeek / Qwen / GLM 等开源模型托管"),
    dict(provider="无问芯穹 Infini-AI", domain="cloud.infini-ai.com",
         base_urls=["https://cloud.infini-ai.com/maas/v1"],
         key_hint="sk-", coding_plan=False,
         note="上海无问芯穹 MaaS 平台"),
    dict(provider="书生·浦语 InternLM", domain="api.intern-ai.org.cn",
         base_urls=["https://api.intern-ai.org.cn/v1"],
         key_hint="sk-", coding_plan=False,
         note="上海人工智能实验室 InternLM 系列"),
    dict(provider="智源 BAAI", domain="api.baai.ac.cn",
         base_urls=["https://api.baai.ac.cn"],
         key_hint="", coding_plan=False,
         note="北京智源人工智能研究院"),
    dict(provider="摩尔线程", domain="api.mthreads.com",
         base_urls=["https://api.mthreads.com/v1"],
         key_hint="sk-", coding_plan=False,
         note="摩尔线程 MUSA 推理平台"),
    dict(provider="天工 AI", domain="api.tiangong.cn",
         base_urls=["https://api.tiangong.cn"],
         key_hint="", coding_plan=False,
         note="昆仑万维天工大模型"),

    # ================= 海外服务商 =================
    dict(provider="OpenAI", domain="api.openai.com",
         base_urls=["https://api.openai.com/v1"],
         key_hint="sk- / sk-proj- / sk-svcacct-", coding_plan=False,
         note="GPT 系列 / o 系列；新密钥多为 sk-proj- 前缀"),
    dict(provider="Azure OpenAI", domain="openai.azure.com",
         base_urls=["https://<resource>.openai.azure.com/openai/v1"],
         key_hint="", coding_plan=False,
         note="微软 Azure 托管 OpenAI，端点含资源名与部署名"),
    dict(provider="GitHub Models", domain="models.inference.ai.azure.com",
         base_urls=["https://models.inference.ai.azure.com"],
         key_hint="github_pat_ / ghp_", coding_plan=False,
         note="GitHub 免费模型托管（OpenAI 兼容）"),
    dict(provider="Anthropic Claude", domain="api.anthropic.com",
         base_urls=["https://api.anthropic.com/v1"],
         key_hint="sk-ant-", coding_plan=False,
         note="Claude 系列；消息端点 /v1/messages"),
    dict(provider="Google Gemini", domain="generativelanguage.googleapis.com",
         base_urls=["https://generativelanguage.googleapis.com/v1beta",
                    "https://generativelanguage.googleapis.com/v1beta/openai"],
         key_hint="AIza...", coding_plan=False,
         note="Gemini 系列，OpenAI 兼容端点为 /v1beta/openai"),
    dict(provider="xAI Grok", domain="api.x.ai",
         base_urls=["https://api.x.ai/v1"],
         key_hint="xai- 或 sk-", coding_plan=False,
         note="马斯克 xAI：grok-2 / grok-3 / grok-code 等"),
    dict(provider="Groq", domain="api.groq.com",
         base_urls=["https://api.groq.com/openai/v1"],
         key_hint="gsk_", coding_plan=False,
         note="极速推理：llama / qwen / deepseek 托管"),
    dict(provider="Mistral", domain="api.mistral.ai",
         base_urls=["https://api.mistral.ai/v1"],
         key_hint="", coding_plan=False,
         note="Mistral Large / Codestral 代码模型"),
    dict(provider="Cohere", domain="api.cohere.com",
         base_urls=["https://api.cohere.com/v1"],
         key_hint="", coding_plan=False,
         note="Command 系列与企业 RAG"),
    dict(provider="Together AI", domain="api.together.xyz",
         base_urls=["https://api.together.xyz/v1"],
         key_hint="", coding_plan=False,
         note="开源模型聚合推理平台"),
    dict(provider="Fireworks AI", domain="api.fireworks.ai",
         base_urls=["https://api.fireworks.ai/inference/v1"],
         key_hint="", coding_plan=False,
         note="开源模型聚合推理平台"),
    dict(provider="OpenRouter", domain="openrouter.ai",
         base_urls=["https://openrouter.ai/api/v1"],
         key_hint="sk-or-", coding_plan=False,
         note="全模型聚合路由平台，密钥 sk-or- 前缀"),
    dict(provider="AnyRouter", domain="anyrouter.dev",
         base_urls=["https://anyrouter.dev/api/v1", "https://anyrouter.dev/api"],
         key_hint="sk-ar-v1- / sk-ar- / ak_", coding_plan=False,
         note="OpenAI/Anthropic 双兼容聚合网关：/api/v1 走 OpenAI 兼容，/api 走 Anthropic 兼容；LLM 密钥 sk-ar- 前缀，管理密钥 ak_"),
    dict(provider="OpenCode Zen", domain="opencode.ai/zen",
         base_urls=["https://opencode.ai/zen/v1", "https://opencode.ai/zen"],
         key_hint="", coding_plan=False,
         note="OpenCode 官方网关（按量付费）：精选 GPT/Claude/Gemini 等模型；OpenAI 兼容用 /zen/v1，Anthropic 兼容用 /zen，密钥环境变量 OPENCODE_API_KEY"),
    dict(provider="OpenCode Go", domain="opencode.ai/zen/go",
         base_urls=["https://opencode.ai/zen/go/v1"],
         key_hint="", coding_plan=True,
         note="OpenCode Go 订阅计划（$10/月，托管在美/欧/新加坡）：开源编程模型，模型 id 用 opencode-go/<model> 格式"),
    dict(provider="Perplexity", domain="api.perplexity.ai",
         base_urls=["https://api.perplexity.ai"],
         key_hint="pplx-", coding_plan=False,
         note="Perplexity 在线搜索模型"),
    dict(provider="HuggingFace", domain="huggingface.co",
         base_urls=["https://api-inference.huggingface.co"],
         key_hint="hf_", coding_plan=False,
         note="HF Inference API 与 Inference Endpoints"),
    dict(provider="Replicate", domain="api.replicate.com",
         base_urls=["https://api.replicate.com/v1"],
         key_hint="r8_", coding_plan=False,
         note="模型托管运行平台"),
    dict(provider="NVIDIA NIM", domain="integrate.api.nvidia.com",
         base_urls=["https://integrate.api.nvidia.com/v1"],
         key_hint="nvapi-", coding_plan=False,
         note="NVIDIA 加速推理微服务"),
    dict(provider="DeepInfra", domain="api.deepinfra.com",
         base_urls=["https://api.deepinfra.com/v1/openai"],
         key_hint="", coding_plan=False,
         note="开源模型推理平台"),
    dict(provider="Novita AI", domain="api.novita.ai",
         base_urls=["https://api.novita.ai/v3/openai"],
         key_hint="", coding_plan=False,
         note="开源模型与图像生成推理"),
    dict(provider="SambaNova", domain="api.sambanova.ai",
         base_urls=["https://api.sambanova.ai/v1"],
         key_hint="", coding_plan=False,
         note="RDU 加速推理，Llama 托管"),
    dict(provider="Cerebras", domain="api.cerebras.ai",
         base_urls=["https://api.cerebras.ai/v1"],
         key_hint="", coding_plan=False,
         note="WSE 芯片极速推理"),
    dict(provider="Lambda", domain="api.lambda.ai",
         base_urls=["https://api.lambda.ai/v1"],
         key_hint="", coding_plan=False,
         note="Lambda 云 GPU 推理"),
    dict(provider="Cloudflare Workers AI", domain="api.cloudflare.com",
         base_urls=["https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"],
         key_hint="", coding_plan=False,
         note="Cloudflare Workers AI 网关"),
    dict(provider="01.AI 零一万物国际版", domain="api.01.ai",
         base_urls=["https://api.01.ai/v1"],
         key_hint="sk-", coding_plan=False,
         note="01.AI 海外端点"),
    dict(provider="Lepton AI", domain="llm.lepton.ai",
         base_urls=["https://llm.lepton.ai"],
         key_hint="", coding_plan=False,
         note="Lepton 模型托管"),
    dict(provider="Anyscale Endpoints", domain="api.endpoints.anyscale.com",
         base_urls=["https://api.endpoints.anyscale.com/v1"],
         key_hint="", coding_plan=False,
         note="Anyscale 模型端点"),
    dict(provider="OctoAI", domain="text.octoai.run",
         base_urls=["https://text.octoai.run/v1"],
         key_hint="", coding_plan=False,
         note="OctoAI 文本推理"),
    dict(provider="FriendliAI", domain="api.friendli.ai",
         base_urls=["https://api.friendli.ai/server/v1"],
         key_hint="", coding_plan=False,
         note="Friendli 高性能推理"),
    dict(provider="Jina AI", domain="api.jina.ai",
         base_urls=["https://api.jina.ai/v1"],
         key_hint="", coding_plan=False,
         note="Jina Embeddings / Reranker / Reader"),
    dict(provider="IBM watsonx", domain="ml.cloud.ibm.com",
         base_urls=["https://{region}.ml.cloud.ibm.com"],
         key_hint="", coding_plan=False,
         note="IBM watsonx.ai"),
    dict(provider="Oracle OCI GenAI", domain="inference.generativeai.us-chicago-1.oci.oraclecloud.com",
         base_urls=["https://inference.generativeai.us-chicago-1.oci.oraclecloud.com"],
         key_hint="", coding_plan=False,
         note="Oracle 生成式 AI 服务"),

    # ================= 本地 / 自建网关 =================
    dict(provider="Ollama", domain="localhost:11434",
         base_urls=["http://localhost:11434/v1"],
         key_hint="", coding_plan=False,
         note="本地 Ollama OpenAI 兼容端点"),
    dict(provider="vLLM 自建", domain="localhost:8000",
         base_urls=["http://localhost:8000/v1"],
         key_hint="", coding_plan=False,
         note="vLLM 服务默认端口"),
    dict(provider="LM Studio", domain="localhost:1234",
         base_urls=["http://localhost:1234/v1"],
         key_hint="", coding_plan=False,
         note="LM Studio 本地推理"),
    dict(provider="LiteLLM 网关", domain="localhost:4000",
         base_urls=["http://localhost:4000/v1"],
         key_hint="", coding_plan=False,
         note="LiteLLM Proxy 默认端口"),
    dict(provider="NewAPI/OneAPI 网关", domain="localhost:3000",
         base_urls=["http://localhost:3000/v1"],
         key_hint="sk-", coding_plan=False,
         note="New API / One API 等自建聚合网关（常见端口 3000/8080/8000）"),
]


def to_markdown_table() -> str:
    """将端点库渲染成 Markdown 表格，用于注入 AI 提示词。"""
    lines = ["| 服务商 | 常用 API_BASE_URL | 密钥特征 | Coding Plan | 备注 |",
             "|---|---|---|---|---|"]
    for ep in ENDPOINTS:
        base = "<br>".join(ep["base_urls"])
        plan = "✅" if ep["coding_plan"] else ""
        lines.append(f"| {ep['provider']} | {base} | {ep['key_hint'] or '-'} | {plan} | {ep['note']} |")
    return "\n".join(lines)


def match_by_domain(text: str) -> list[dict]:
    """按域名子串匹配端点（规则引擎用），返回按匹配长度排序的候选列表。"""
    lower = text.lower()
    hits = []
    for ep in ENDPOINTS:
        if ep["domain"] in lower:
            hits.append(ep)
    hits.sort(key=lambda e: len(e["domain"]), reverse=True)
    return hits


def find(provider: str) -> dict | None:
    """按服务商名查找端点。"""
    for ep in ENDPOINTS:
        if ep["provider"] == provider:
            return ep
    return None


# ---------------------------------------------------------------
# api_base 规范化：AI/规则引擎给出的端点常缺版本路径（只有根域名），
# 这里统一剥离动作路径段并补齐版本路径（默认 /v1，按端点库约定优先）。
# ---------------------------------------------------------------
# 动作路径段：从 URL 尾部剥离，直到露出真正的版本路径
_ACTION_SEGMENTS = ("chat/completions", "chat", "completions",
                    "models", "embeddings", "messages", "generate")
# 版本路径形态（仅匹配 URL 路径部分，避免误伤域名里的 api 等字样）
_VERSION_PATH_RE = re.compile(
    r"/v\d+(?:\.\d+)?(?:beta)?(?:/|$)"                       # /v1 /v1beta /v2
    r"|/api(?:/|$)|/openai(?:/|$)|/compatible-mode(?:/|$)|/maas(?:/|$)"
    r"|/inference(?:/|$)|/server(?:/|$)|/zen(?:/|$)|/paas(?:/|$)|/client(?:/|$)",
    re.IGNORECASE,
)


def _default_base_for_host(url: str) -> str | None:
    """按域名匹配端点库：返回该域名下含版本路径的第一个默认端点，否则返回主端点。"""
    host = urlparse(url).netloc.lower()
    if not host:
        return None
    for ep in ENDPOINTS:
        if any(urlparse(b).netloc.lower() == host for b in ep["base_urls"]):
            for b in ep["base_urls"]:
                if _VERSION_PATH_RE.search(urlparse(b).path):
                    return b.rstrip("/")
            return ep["base_urls"][0].rstrip("/")
    return None


def normalize_base_url(url: str, provider: str = "") -> str:
    """规范化 API_BASE_URL：剥离动作路径段、补齐版本路径。

    规则：1) 剥离 /chat/completions、/models 等动作路径段；
         2) 已有版本路径（/v1、/api/v3、/compatible-mode/v1 等）保持不变；
         3) 缺失时按端点库同域默认端点补全（智谱 /api/paas/v4、阿里
            /compatible-mode/v1、豆包 /api/v3 等，各服务商版本路径不同，
            并非都是 /v1）；端点库无匹配（如私有网关）则补 /v1。
    """
    if not url:
        return url
    url = url.strip().rstrip("/")
    changed = True
    while changed:
        changed = False
        for seg in _ACTION_SEGMENTS:
            if url.endswith("/" + seg):
                url = url[: -(len(seg) + 1)]
                changed = True
    url = url.rstrip("/")
    if not url:
        return url
    if _VERSION_PATH_RE.search(urlparse(url).path):
        return url
    base = _default_base_for_host(url)
    if base:
        return base
    return url + "/v1"


# ---------------------------------------------------------------
# 预检关键词表：文件中大概率出现这些字样 → 提示候选服务商。
# 顺序即优先级（排在前面视为更强线索），供规则引擎与 AI 预检共用。
# ---------------------------------------------------------------
KEYWORD_HINTS: dict[str, list[str]] = {
    # ---- 国内 ----
    "deepseek": ["DeepSeek"],
    "moonshot": ["Kimi 月之暗面"], "kimi": ["Kimi 月之暗面"],
    "zhipu": ["智谱 GLM"], "bigmodel": ["智谱 GLM"], "glm": ["智谱 GLM"],
    "codegeex": ["智谱 CodeGeeX"],
    "dashscope": ["通义千问 Qwen"], "qwen": ["通义千问 Qwen"], "tongyi": ["通义千问 Qwen"],
    "doubao": ["豆包/火山方舟"], "ark": ["豆包/火山方舟"], "volcengine": ["豆包/火山方舟"], "volces": ["豆包/火山方舟"],
    "qianfan": ["百度千帆"], "ernie": ["百度千帆"],
    "xfyun": ["讯飞星火"], "spark": ["讯飞星火"],
    "hunyuan": ["腾讯混元"],
    "minimax": ["MiniMax"], "hailuo": ["MiniMax"],
    "stepfun": ["阶跃星辰 StepFun"], "step-2": ["阶跃星辰 StepFun"],
    "baichuan": ["百川智能 Baichuan"],
    "lingyiwanwu": ["零一万物 Yi"],
    "siliconflow": ["硅基流动 SiliconFlow"],
    "internlm": ["书生·浦语 InternLM"],
    "baai": ["智源 BAAI"],
    "tiangong": ["天工 AI"],
    # ---- 海外 ----
    "openai": ["OpenAI"], "gpt-5": ["OpenAI"], "gpt-4": ["OpenAI"], "gpt-3": ["OpenAI"],
    "anthropic": ["Anthropic Claude"], "claude": ["Anthropic Claude"],
    "x-ai": ["xAI Grok"], "grok": ["xAI Grok"], "xai": ["xAI Grok"],
    "gemini": ["Google Gemini"],
    "groq": ["Groq"],
    "mistral": ["Mistral"], "codestral": ["Mistral"],
    "openrouter": ["OpenRouter"],
    "anyrouter": ["AnyRouter"],
    "opencode": ["OpenCode Go", "OpenCode Zen"],
    "huggingface": ["HuggingFace"],
    "replicate": ["Replicate"],
    "perplexity": ["Perplexity"],
    "cohere": ["Cohere"],
    "together.ai": ["Together AI"],
    "fireworks": ["Fireworks AI"],
    "nvidia": ["NVIDIA NIM"], "nvapi": ["NVIDIA NIM"],
    # ---- 本地 / 自建网关 ----
    "ollama": ["Ollama"], "vllm": ["vLLM 自建"], "litellm": ["LiteLLM 网关"],
    "new-api": ["NewAPI/OneAPI 网关"], "newapi": ["NewAPI/OneAPI 网关"], "oneapi": ["NewAPI/OneAPI 网关"],
    "lmstudio": ["LM Studio"],
}


def match_keywords(text: str) -> list[tuple[str, list[str]]]:
    """按预检关键词表子串匹配，返回 [(关键词, 候选服务商列表), ...]（按表内优先级排序）。"""
    lower = text.lower()
    out = []
    for kw, names in KEYWORD_HINTS.items():
        if kw in lower:
            out.append((kw, names))
    return out


def candidates_from_keywords(text: str) -> list[str]:
    """返回关键词命中的候选服务商名（去重保序）。"""
    seen: set[str] = set()
    out = []
    for _, names in match_keywords(text):
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out
