# -*- coding: utf-8 -*-
"""
密钥分析模块：AI 分析为主，规则引擎兜底。

AIAnalyzer    —— 调用 OpenAI 兼容接口（DeepSeek/GLM/Kimi/通义等均可），
                 提示词内嵌端点库，让 AI 挑选 provider 与 API_BASE_URL；
RuleAnalyzer  —— 无 AI 配置或 AI 调用失败时，按域名/变量名启发式判断。

两者输出统一的记录结构：
{"api_key","provider","api_base","confidence","notes"}
"""
from __future__ import annotations

import json
import re
import time

import requests

from gks.endpoints import (candidates_from_keywords, find as find_endpoint,
                           match_by_domain, match_keywords)
from gks.github_search import KEY_PATTERNS, extract_keys
from gks.prompts import SYSTEM_PROMPT

# ---------- 文本预检与截断工具 ----------

# 通用 URL（去除尾部常见标点）
URL_RE = re.compile(r"https?://[^\s\"'`<>()\[\]{}，。；、]+")
_URL_TRAIL = ".,;:!?)]}>\"'，。；"
# 显式 base/endpoint 变量赋值
BASE_VAR_RE = re.compile(
    r"(?:base_url|api_base|api_url|baseurl|endpoint|api_endpoint|base_urls|api_base_url|BASE_URL)"
    r"\s*[:=]\s*[\"']?(https?://[^\"'\s]+)", re.IGNORECASE)

# 任意 <VAR> = "https://..." 赋值（用于找密钥变量对应的 BASE 变量）
ANY_URL_ASSIGN_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*[\"']?(https?://[^\"'\s]+)", re.IGNORECASE)

# 密钥赋值变量名：VAR="sk-..."（同一行）
KEY_VAR_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*[\"']?")


def extract_urls(text: str) -> list[str]:
    """提取文本中所有 http(s) URL（去重保序）。"""
    seen: set[str] = set()
    out = []
    for m in URL_RE.finditer(text):
        u = m.group(0).rstrip(_URL_TRAIL)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


# API 动作路径段：作为 base 入库时应剥掉（chat/completions、messages 等）
_API_ACTION_SEGS = ("/chat/completions", "/chat-completions", "/responses",
                     "/messages", "/completions", "/embeddings", "/generations",
                     "/generate", "/models")


def normalize_base_url(url: str) -> str:
    """把（可能是完整请求 URL 的）URL 归一化为 API_BASE：剥掉末尾动作段与多余斜杠。"""
    u = url.strip().rstrip("/")
    for seg in _API_ACTION_SEGS:
        if u.endswith(seg):
            u = u[: -len(seg)].rstrip("/")
            break
    return u


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：英文/代码约 4 字符 1 token，中文约 1.5 字符 1 token。"""
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff' or '\uff00' <= ch <= '\uffef')
    ascii_n = len(text) - cjk
    return max(1, int(ascii_n / 4 + cjk / 1.5) + 1)


def truncate_context(text: str, keys: list[str], max_tokens: int = 2000,
                     window: int = 1200) -> str:
    """限制提交给 AI 的上下文长度。

    未超限时原样返回；超限时仅保留每个密钥前后 window 字符的窗口
    （重叠窗口合并），其余部分以省略标记代替。
    """
    if estimate_tokens(text) <= max_tokens:
        return text
    positions = []
    for k in keys:
        start = 0
        while True:
            i = text.find(k, start)
            if i < 0:
                break
            positions.append(i)
            start = i + len(k)
    if not positions:
        head = text[:max_tokens * 4]
        return head + "\n…[内容过长已截断]…"
    return _truncate_at(text, positions, max_tokens, window)


def _truncate_at(text: str, positions: list[int], max_tokens: int, window: int) -> str:
    """按位置窗口拼接上下文，超限时递归缩小窗口。"""
    intervals = sorted((max(0, p - window), min(len(text), p + window)) for p in positions)
    merged = []
    for s, e in intervals:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    parts, prev = [], 0
    for s, e in merged:
        if s > prev:
            parts.append("\n…[中间省略]…\n")
        parts.append(text[s:e])
        prev = e
    out = "".join(parts)
    if estimate_tokens(out) <= max_tokens or window <= 64:
        return out
    return _truncate_at(text, positions, max_tokens, window // 2)


def preflight(text: str) -> dict:
    """本地预检：关键词命中 + 文件内 URL + 疑似 API 端点 + 候选服务商。

    结果注入 AI 提示词，引导 AI 优先依据文件自身给出的线索判断。
    """
    urls = extract_urls(text)
    base_urls, seen = [], set()
    for m in BASE_VAR_RE.finditer(text):
        u = normalize_base_url(m.group(1).rstrip(_URL_TRAIL))
        if u and u not in seen:
            seen.add(u)
            base_urls.append(u)
    for u in urls:
        if u not in seen and re.search(r"(/api|/v\d|base|open|endpoint)", u, re.IGNORECASE):
            seen.add(u)
            base_urls.append(normalize_base_url(u))
    kws = [kw for kw, _ in match_keywords(text)]
    providers = candidates_from_keywords(text)
    return {
        "keywords": kws,
        "providers": providers,
        "urls": urls,
        "base_urls": base_urls,
        "suggested": providers[0] if len(providers) == 1 else None,
    }


# ---------- 密钥分类 ----------

# 云服务上下文关键词（命中即判为 cloud）
CLOUD_KEYWORDS = (
    "aws", "amazon web", "azure", "google cloud", "gcp", "aliyun", "alicloud",
    "tencent cloud", "qcloud", "huawei cloud", "secretid", "secret_id",
    "access_key_id", "aws_access", "azure_subscription", "gcp_", "s3",
)
# 其他第三方服务关键词（命中即判为 other）
OTHER_KEYWORDS = (
    "elevenlabs", "tavily", "stripe", "twilio", "sendgrid", "slack",
    "openweather", "mapbox", "paypal", "github_token", "gh_token",
    "npm_token", "pypi_token", "notion", "airtable", "hubspot", "mailgun",
    "resend", "brevo", "dropbox", "digitalocean", "cloudflare",
)


def classify_key(key: str, text: str = "") -> str:
    """按密钥格式与上下文关键词推测分类：llm / cloud / other / unknown。

    - 密钥前缀强信号：AKIA/ASIA → cloud；tvly-/sk_/ghp_ → other；gsk_/xai- → llm；
    - 否则按上下文关键词（云服务 > 其他服务 > 大模型）；全无 → unknown。
    """
    lower = text.lower()
    if key.startswith(("AKIA", "ASIA")):
        return "cloud"
    if key.startswith("tvly-"):
        return "other"
    if key.startswith("sk_"):
        return "other"  # ElevenLabs 等
    if key.startswith(("ghp_", "gho_", "ghu_", "ghs_", "github_pat_")):
        return "other"
    if key.startswith(("gsk_", "xai-")):
        return "llm"
    if any(kw in lower for kw in CLOUD_KEYWORDS):
        return "cloud"
    if any(kw in lower for kw in OTHER_KEYWORDS):
        return "other"
    if match_keywords(lower):
        return "llm"
    return "unknown"


# 密钥前缀强信号：prefix → (provider, category, api_base)。
# 顺序即优先级（专用变体在前）。LLM 服务取端点库默认 base，非 LLM 服务给出官方 base。
_PREFIX_HINTS: list[tuple[str, tuple[str, str, str]]] = [
    ("sk-proj-", ("OpenAI", "llm", "https://api.openai.com/v1")),
    ("sk-ant-", ("Anthropic Claude", "llm", "https://api.anthropic.com")),
    ("sk-or-", ("OpenRouter", "llm", "https://openrouter.ai/api/v1")),
    ("sk-ar-", ("AnyRouter", "llm", "https://anyrouter.dev/api/v1")),
    ("sk-sv-", ("硅基流动 SiliconFlow", "llm", "https://api.siliconflow.cn/v1")),
    ("gsk_", ("Groq", "llm", "https://api.groq.com/openai/v1")),
    ("xai-", ("xAI Grok", "llm", "https://api.x.ai/v1")),
    ("tvly-", ("Tavily", "other", "https://api.tavily.com")),
    ("sk_", ("ElevenLabs", "other", "https://api.elevenlabs.io")),
    ("AKIA", ("AWS", "cloud", "")),
    ("ASIA", ("AWS", "cloud", "")),
    ("ghp_", ("GitHub", "other", "https://api.github.com")),
    ("gho_", ("GitHub", "other", "https://api.github.com")),
    ("ghu_", ("GitHub", "other", "https://api.github.com")),
    ("ghs_", ("GitHub", "other", "https://api.github.com")),
]


def _prefix_hint(key: str) -> tuple[str, str, str] | None:
    """按密钥前缀返回 (provider, category, api_base) 强信号，无匹配返回 None。"""
    for prefix, hint in _PREFIX_HINTS:
        if key.startswith(prefix):
            return hint
    return None


class RuleAnalyzer:
    """零依赖兜底：按上下文中的域名、服务商名、变量名启发式匹配端点库。"""

    def analyze(self, hit) -> list[dict]:
        text = hit.text
        results = []
        for key in extract_keys(text):
            results.append(self._judge(key, text))
        return results

    @staticmethod
    def _judge(key: str, text: str) -> dict:
        i = text.find(key)
        provider, base, conf, note = "unknown", "", 0.2, "上下文无明确服务商线索"
        hint = _prefix_hint(key)  # 密钥前缀强信号
        category = hint[1] if hint else "unknown"

        # ---- 0) 找出与该密钥关联的 base URL（仅变量前缀匹配，避免混合文件串线）----
        base_url = None
        line_start = text.rfind("\n", 0, max(i, 0)) + 1
        line_end = text.find("\n", i)
        line = text[line_start:line_end if line_end > 0 else len(text)]
        m = KEY_VAR_RE.match(line)
        if m:
            var_name = m.group(1)
            prefix = re.sub(r"_(?:API_)?(?:KEY|TOKEN|SECRET|PASSWORD|ID)$", "",
                            var_name, flags=re.IGNORECASE)
            for m2 in ANY_URL_ASSIGN_RE.finditer(text):
                vn = m2.group(1)
                if re.search(r"(base|endpoint|url)", vn, re.IGNORECASE) \
                        and vn.lower().startswith(prefix.lower()):
                    base_url = m2.group(2).rstrip(_URL_TRAIL)
                    break
        # 无前缀匹配时：全文若只有唯一一个 base 赋值（单端点文件），直接采用
        if not base_url:
            assigns = [m2.group(2).rstrip(_URL_TRAIL) for m2 in ANY_URL_ASSIGN_RE.finditer(text)
                       if re.search(r"(base|endpoint|url)", m2.group(1), re.IGNORECASE)]
            uniq = list(dict.fromkeys(assigns))
            if len(uniq) == 1:
                base_url = uniq[0]

        # ---- 1) 文件 URL 优先：由密钥关联的 URL 判定 provider ----
        if base_url:
            bn = normalize_base_url(base_url)
            hits = match_by_domain(bn)
            if hits:
                ep = hits[0]
                provider = ep["provider"]
                base = bn
                conf = 1.0
                note = f"文件显式给出 URL {bn}，按文件指定"
            elif hint:
                provider, category, _ = hint
                base = bn
                conf = 0.8
                note = f"密钥前缀特征 {key.split('-')[0] if '-' in key else key[:4]} + 文件 URL {bn}"
            else:
                base = bn
                conf = 0.6
                note = f"文件给出 URL {bn}，但无法匹配已知服务商"
        elif hint:
            # ---- 2) 密钥前缀强信号（sk-proj-/tvly-/AKIA/ghp_ 等）----
            provider, category, base = hint
            conf = 0.8
            note = f"密钥前缀特征: {key.split('-')[0] if '-' in key else key[:4]}"
        else:
            start = max(0, i - 300)
            end = min(len(text), i + len(key) + 300)
            local = text[start:end]
            # ---- 3a) 窗口内 URL 域名匹配（取距密钥最近的命中，优先于关键词表序）----
            for u in sorted(extract_urls(local), key=lambda u: abs(local.find(u) + start - i)):
                hits = match_by_domain(u)
                if hits:
                    ep = hits[0]
                    provider = ep["provider"]
                    base = normalize_base_url(u)
                    conf = 0.8
                    note = f"窗口内出现域名 {ep['domain']}"
                    break
            # ---- 3b) 局部窗口关键词（以密钥为中心 300 字符，互不串线）----
            if provider == "unknown":
                for token, names in match_keywords(local.lower()):
                    ep = find_endpoint(names[0])
                    if ep:
                        provider = names[0]
                        base = ep["base_urls"][0]
                        conf = 0.6
                        note = f"上下文出现关键词: {token}"
                        break
            # ---- 4) 全文回退：仅当全文只有单一服务商信号时（混合文件不可靠）----
            if provider == "unknown":
                full_names = {n for _, names in match_keywords(text.lower()) for n in names}
                if len(full_names) == 1:
                    ep = find_endpoint(next(iter(full_names)))
                    if ep:
                        provider = ep["provider"]
                        base = ep["base_urls"][0]
                        conf = 0.6
                        note = f"全文出现关键词: {next(iter(full_names))}"

        # 分类收尾：优先前缀信号分类；文件 URL 命中端点库 → llm；否则按局部上下文推断
        if provider != "unknown" and category == "unknown":
            if base and match_by_domain(base):
                category = "llm"
            else:
                category = classify_key(key, text[max(0, i - 300):i + len(key) + 300])
        elif provider == "unknown":
            category = classify_key(key, text[max(0, i - 300):i + len(key) + 300])

        return {"api_key": key, "provider": provider, "api_base": base,
                "confidence": round(conf, 2), "notes": note,
                "category": category}


class AIAnalyzer:
    """调用 OpenAI 兼容接口分析单文件（一次性返回全部密钥判断）。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 60, max_retries: int = 2,
                 max_tokens: int | None = None, context_window: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        # 单文件提交 token 上限与截取窗口（None 时使用 config 默认值）
        from config import config as cfg
        self.max_tokens = max_tokens or cfg.AI_MAX_TOKENS
        self.context_window = context_window or cfg.KEY_CONTEXT_CHARS
        self.temperature = cfg.AI_TEMPERATURE

    def analyze(self, hit) -> list[dict]:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        # Send only signals chosen from our static endpoint/keyword/format lists.
        # Arbitrary source values and metadata never enter an external AI request.
        keys = extract_keys(hit.text)
        placeholder_to_key: dict[str, str] = {}
        candidates = []
        for index, key in enumerate(keys, 1):
            placeholder = f"KEY_CANDIDATE_{index}"
            placeholder_to_key[placeholder] = key
            at = hit.text.find(key)
            local = hit.text[max(0, at - self.context_window):at + len(key) + self.context_window]
            endpoints = []
            for source_url in extract_urls(local):
                for endpoint in match_by_domain(source_url):
                    if endpoint["provider"] not in [e["provider"] for e in endpoints]:
                        endpoints.append({"provider": endpoint["provider"],
                                          "domain": endpoint["domain"],
                                          "base_urls": endpoint["base_urls"]})
            candidates.append({
                "api_key": placeholder,
                "format": next((name for name, pattern in KEY_PATTERNS if pattern.fullmatch(key)), "unknown"),
                "category_hint": classify_key(key, local),
                "keywords": [name for name, _ in match_keywords(local)],
                "providers": candidates_from_keywords(local),
                "known_endpoints": endpoints,
            })
        from gks.privacy import redact_text
        user_message = json.dumps({"mode": "public_redacted_signals", "candidates": candidates}, ensure_ascii=False)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "temperature": self.temperature,
        }
        last_err = None
        for attempt in range(self.max_retries + 1):
            body = dict(payload)
            if attempt == 0:
                body["response_format"] = {"type": "json_object"}  # 部分服务不支持，失败重试时去掉
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=self.timeout)
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    parsed = self._parse(content)
                    for item in parsed:
                        item["api_key"] = placeholder_to_key.get(item["api_key"],
                                                                  item["api_key"])
                    return self._reconcile(parsed, keys, hit)
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as e:  # noqa: BLE001 —— 网络异常统一降级
                last_err = str(e)[:200]
            time.sleep(1.5)
        last_err = redact_text(str(last_err), keys + [self.api_key])
        print(f"  [AI 失败] 候选文件：{last_err}，降级为规则引擎")
        return RuleAnalyzer().analyze(hit)

    @staticmethod
    def _reconcile(parsed: list[dict], expected_keys: list[str], hit) -> list[dict]:
        """只接纳文件中实际提取到的密钥，并用规则结果补齐 AI 遗漏项。

        AI 输出属于不可信输入：可能重复、遗漏，甚至生成文件中不存在的密钥。
        最终结果严格按本地提取顺序返回，每个候选恰好一条。
        """
        expected = set(expected_keys)
        by_key: dict[str, dict] = {}
        for item in parsed:
            key = item.get("api_key", "")
            if key in expected and key not in by_key:
                by_key[key] = item

        if len(by_key) < len(expected_keys):
            rule_results = {item["api_key"]: item for item in RuleAnalyzer().analyze(hit)}
            for key in expected_keys:
                if key not in by_key and key in rule_results:
                    by_key[key] = rule_results[key]

        return [by_key[key] for key in expected_keys if key in by_key]

    @staticmethod
    def _parse(content: str) -> list[dict]:
        """解析 AI 输出 JSON；容忍 ```json 代码块包裹。

        每条密钥独立输出一个 dict（不合并、不去重之外的规整），
        category 缺失时按规则引擎分类兜底。
        """
        text = content.strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        data = json.loads(text)
        keys = data.get("keys", []) if isinstance(data, dict) else []
        out = []
        for item in keys:
            if not isinstance(item, dict) or not item.get("api_key"):
                continue
            api_key = str(item.get("api_key", "")).strip()
            out.append({
                "api_key": api_key,
                "provider": str(item.get("provider") or "unknown").strip(),
                "api_base": str(item.get("api_base") or "").strip(),
                "confidence": _norm_confidence(item.get("confidence")),
                "notes": str(item.get("notes") or "").strip(),
                "category": str(item.get("category") or classify_key(api_key, "")).strip()
                            or "unknown",
            })
        return out


def _norm_confidence(value) -> float:
    """AI 置信度归一化：5 分制（1~5）除以 5 存为 0.2~1.0；旧格式 0~1 浮点直接保留。

    - JSON 整数（1/3/5）→ 5 分制（/5）；
    - 浮点 ≥ 1（1.0/4.0/5.0 等 5 分制写法，或旧格式 1.0）→ 按 5 分制（/5）；
    - 浮点 < 1（0.8/0.95）→ 旧格式直接保留。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.2
    if (isinstance(value, int) and not isinstance(value, bool)) or v >= 1.0:
        v /= 5.0
    return round(min(1.0, max(0.1, v)), 2)


# 分析器工厂：AI 可用返回 AIAnalyzer，否则返回 RuleAnalyzer（供其他模块直接使用）
make_analyzer = lambda ai_ready, ai_cfg=None: (  # noqa: E731
    AIAnalyzer(base_url=(ai_cfg or {}).get("base_url"),
               api_key=(ai_cfg or {}).get("api_key"),
               model=(ai_cfg or {}).get("model"))
    if ai_ready else RuleAnalyzer()
)
