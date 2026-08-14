# -*- coding: utf-8 -*-
"""
大模型 API Key 有效性测试模块。

原理：向 OpenAI 兼容端点发送 GET {api_base}/models（只读、无消耗），
按响应判定密钥状态：

- 200        → active（有效，可列出模型）
- 401/403    → invalid（鉴权失败，密钥无效）
- 404        → unsupported（端点不支持 /models，可能非 OpenAI 兼容）
- 429        → rate_limited（限流，状态未知，勿改）
- 其他/异常  → network / error（状态未知，勿改）

测试端点选择：优先记录中的 api_base_url，其次端点库该服务商的默认地址。
"""
from __future__ import annotations

import time

import requests

from .endpoints import find as find_endpoint
from .endpoints import normalize_base_url

TEST_TIMEOUT = 15  # 秒
USER_AGENT = "github-key-scanner/1.0 (key validity probe)"


def pick_base(record: dict) -> str | None:
    """确定测试端点：优先记录里的 api_base_url（兼容分析器的 api_base），其次端点库默认地址。

    记录中的 base 先做规范化（剥离动作路径段、补齐 /v1 等版本路径），
    避免 /chat/completions 之类的旧值拼出错误测试 URL。
    """
    base = (record.get("api_base_url") or record.get("api_base") or "").strip()
    if base:
        return normalize_base_url(base, record.get("provider") or "")
    ep = find_endpoint(record.get("provider") or "")
    if ep:
        return ep["base_urls"][0]
    return None


def test_key(api_key: str, base: str) -> dict:
    """请求 {base}/models 测试密钥，返回判定结果。

    返回字段：ok / verdict / status_code / models / error / elapsed
    verdict ∈ {active, invalid, unsupported, rate_limited, network, error}
    """
    url = base.rstrip("/") + "/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    t0 = time.monotonic()
    try:
        resp = requests.get(url, headers=headers, timeout=TEST_TIMEOUT)
    except requests.Timeout:
        return {"ok": False, "verdict": "network",
                "error": f"请求超时（>{TEST_TIMEOUT}s），端点可能不可达", "elapsed": round(time.monotonic() - t0, 1)}
    except requests.RequestException as e:
        return {"ok": False, "verdict": "network",
                "error": f"网络错误：{e.__class__.__name__}", "elapsed": round(time.monotonic() - t0, 1)}

    elapsed = round(time.monotonic() - t0, 1)
    status = resp.status_code
    try:
        data = resp.json()
    except Exception:
        data = None

    if status == 200:
        models = data.get("data", []) if isinstance(data, dict) else []
        return {"ok": True, "status_code": status, "verdict": "active",
                "models": len(models) if isinstance(models, list) else 0,
                "elapsed": elapsed}

    if status in (401, 403):
        return {"ok": False, "status_code": status, "verdict": "invalid",
                "error": f"鉴权失败（HTTP {status}）：{_extract_error(data)}", "elapsed": elapsed}

    if status == 404:
        return {"ok": False, "status_code": status, "verdict": "unsupported",
                "error": "端点返回 404：不支持 /models 接口，可能不是 OpenAI 兼容端点", "elapsed": elapsed}

    if status == 429:
        return {"ok": False, "status_code": status, "verdict": "rate_limited",
                "error": "触发限流（HTTP 429），稍后重试", "elapsed": elapsed}

    return {"ok": False, "status_code": status, "verdict": "error",
            "error": f"HTTP {status}：{_extract_error(data)}", "elapsed": elapsed}


def _extract_error(data) -> str:
    """从响应 JSON 中提取错误消息摘要。"""
    if not isinstance(data, dict):
        return ""
    err = data.get("error", data.get("message", ""))
    if isinstance(err, dict):
        err = err.get("message", err.get("code", ""))
    text = str(err).strip()
    return text[:200] if text else ""
