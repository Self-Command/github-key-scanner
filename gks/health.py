# -*- coding: utf-8 -*-
"""运行健康检查：配置、数据库、任务新鲜度与可选 GitHub 连通性。"""
from __future__ import annotations

from datetime import datetime

import requests

from config import config
from gks.database import KeyDatabase
from gks.github_search import GitHubCodeSearch, SEARCH_API


def _age_seconds(timestamp: str) -> float | None:
    if not timestamp:
        return None
    try:
        value = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return max(0.0, (datetime.now() - value).total_seconds())


def collect_health(db: KeyDatabase, online: bool = False) -> dict:
    """收集健康状态；不返回或记录任何凭据原文。"""
    checks: list[dict] = []

    def add(name: str, status: str, message: str) -> None:
        checks.append({"name": name, "status": status, "message": message})

    # 配置有效性
    if 1 <= config.PER_PAGE <= 100 and config.MAX_PAGES > 0:
        add("search_config", "ok", f"per_page={config.PER_PAGE}, max_pages={config.MAX_PAGES}")
    else:
        add("search_config", "error", "GITHUB_PER_PAGE 必须为 1~100，GITHUB_MAX_PAGES 必须为正数")

    if config.SCAN_INTERVAL > 0:
        add("scan_interval", "ok", f"{config.SCAN_INTERVAL} 秒")
    else:
        add("scan_interval", "error", "SCAN_INTERVAL 必须为正数")

    if config.STALE_TASK_SECONDS > config.SCAN_INTERVAL:
        add("stale_threshold", "ok", f"{config.STALE_TASK_SECONDS} 秒")
    else:
        add("stale_threshold", "warning", "STALE_TASK_SECONDS 建议大于 SCAN_INTERVAL")

    if config.GITHUB_TOKEN:
        add("github_token", "ok", "已配置（未显示）")
    else:
        add("github_token", "error", "未配置 GITHUB_TOKEN")

    if not config.AI_ENABLED:
        add("ai_config", "ok", "AI 已禁用，将使用规则引擎")
    elif config.ai_ready:
        add("ai_config", "ok", f"{config.AI_MODEL} @ {config.AI_BASE_URL}")
    else:
        add("ai_config", "warning", "AI 已启用但配置不完整，将降级为规则引擎")

    if config.WEB_HOST in {"127.0.0.1", "localhost", "::1"}:
        add("web_bind", "ok", config.WEB_HOST)
    else:
        add("web_bind", "warning", f"Web 正监听 {config.WEB_HOST}，请确认已配置认证和 HTTPS")

    # 数据库完整性与任务状态
    snapshot = None
    try:
        snapshot = db.operational_snapshot()
        integrity = snapshot["integrity"]
        add("database", "ok" if integrity == "ok" else "error", f"quick_check={integrity}")
    except Exception as exc:  # noqa: BLE001
        add("database", "error", f"数据库检查失败：{str(exc)[:160]}")

    if snapshot is not None:
        running = snapshot["running_tasks"]
        stale = []
        for task in running:
            age = _age_seconds(task.get("started_at", ""))
            if age is None or age >= config.STALE_TASK_SECONDS:
                stale.append(task["id"])
        if stale:
            add("running_tasks", "error", f"陈旧 running 任务：{stale}")
        elif running:
            add("running_tasks", "ok", f"当前运行任务：{[task['id'] for task in running]}")
        else:
            add("running_tasks", "ok", "无运行中任务")

        last_task = snapshot["last_task"]
        if last_task and last_task["status"] == "error":
            add("last_task", "warning", f"最近任务 #{last_task['id']} 失败：{last_task['error'][:100]}")
        elif last_task:
            add("last_task", "ok", f"最近任务 #{last_task['id']} 状态={last_task['status']}")
        else:
            add("last_task", "warning", "尚无扫描任务")

        last_success = snapshot["last_real_success"]
        if not last_success:
            add("scan_freshness", "warning", "尚无成功的真实扫描")
        else:
            reference = last_success.get("finished_at") or last_success.get("started_at", "")
            age = _age_seconds(reference)
            if age is None:
                add("scan_freshness", "warning", "无法解析最近成功扫描时间")
            elif age > config.HEALTH_MAX_SCAN_AGE:
                add("scan_freshness", "warning", f"最近真实成功扫描距今 {int(age)} 秒，已超过阈值")
            else:
                add("scan_freshness", "ok", f"最近真实成功扫描距今 {int(age)} 秒")

    # 显式在线检查才访问 GitHub；使用无匹配探针，不搜索敏感内容。
    if online and config.GITHUB_TOKEN:
        headers = {
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github.text-match+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            user_resp = requests.get("https://api.github.com/user", headers=headers, timeout=20)
            if user_resp.status_code == 200:
                add("github_auth", "ok", f"login={user_resp.json().get('login', 'unknown')}")
            else:
                add("github_auth", "error", f"HTTP {user_resp.status_code}")

            query = GitHubCodeSearch.build_query(
                ["py"], pattern="kiro-health-probe-[a-z]{12}")
            search_resp = requests.get(
                SEARCH_API, headers=headers,
                params={"q": query, "per_page": 1, "page": 1}, timeout=20)
            if search_resp.status_code == 200:
                add("github_code_search", "ok", "搜索端点与查询语法可用")
            else:
                message = search_resp.json().get("message", "")[:100]
                add("github_code_search", "error",
                    f"HTTP {search_resp.status_code}: {message}")
        except requests.RequestException as exc:
            add("github_online", "error", f"网络检查失败：{str(exc)[:160]}")

    statuses = {item["status"] for item in checks}
    overall = "unhealthy" if "error" in statuses else (
        "degraded" if "warning" in statuses else "healthy")
    return {
        "status": overall,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "checks": checks,
    }
