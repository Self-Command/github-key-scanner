# -*- coding: utf-8 -*-
"""
GitHub 代码搜索模块。

- 使用 GitHub REST 代码搜索 API；**实测（2026-08）该令牌下 `path:` 正则与
  `indexed:` 限定符均静默失效（HTTP 200 但 0 命中）**，因此采用替代策略：
  字面量查询词 + `language:` 分语言查询（sort=indexed 最新优先）+ 兜底全局
  查询（覆盖 .env/.conf/.cfg 等无语言类型的文件），命中后由本地严格正则过滤；
- 代码搜索 API 限速 10 次/分钟（认证用户），内置自动限速等待；
- 文件全文改用 raw.githubusercontent.com 抓取（不限速），失败自动放弃。

注意：GitHub 代码搜索必须携带令牌，未认证请求会返回 401。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

SEARCH_API = "https://api.github.com/search/code"
RAW_API = "https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}"
ACCEPT_HEADER = "application/vnd.github.text-match+json"

# 扩展名 → GitHub language 限定符映射（linguist 识别）。
# 不在映射内的扩展名（env/conf/cfg 等）由全局兜底查询覆盖。
LANGUAGE_MAP: dict[str, str] = {
    "py": "python", "pyw": "python",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "json": "json",
    "toml": "toml",
    "yaml": "yaml", "yml": "yaml",
    "ini": "ini",
}

# 多种常见密钥格式（按前缀区分；搜索仍以 sk-[a-z0-9]{32} 为准，分析阶段提取更多类型）
KEY_PATTERNS: list[tuple[str, re.Pattern]] = [
    # 标准 sk- 32 位小写字母数字（主格式，保持严格边界）
    ("sk", re.compile(r"(?<![A-Za-z0-9_])sk-[a-z0-9]{32}(?![A-Za-z0-9_])")),
    # OpenAI 项目密钥 / Anthropic / OpenRouter / AnyRouter / 硅基流动等专用前缀变体
    ("sk-var", re.compile(
        r"(?<![A-Za-z0-9_])sk-(?:proj|ant|or|ar|sv|project|admin|live|test)-[A-Za-z0-9_-]{16,80}(?![A-Za-z0-9_])")),
    # Groq
    ("gsk", re.compile(r"(?<![A-Za-z0-9_])gsk_[A-Za-z0-9]{16,64}(?![A-Za-z0-9_])")),
    # xAI
    ("xai", re.compile(r"(?<![A-Za-z0-9_])xai-[A-Za-z0-9]{16,64}(?![A-Za-z0-9_])")),
    # Tavily
    ("tvly", re.compile(r"(?<![A-Za-z0-9_])tvly-[A-Za-z0-9]{16,64}(?![A-Za-z0-9_])")),
    # ElevenLabs（sk_ 前缀）
    ("sk_", re.compile(r"(?<![A-Za-z0-9_])sk_[A-Za-z0-9]{16,64}(?![A-Za-z0-9_])")),
    # AWS Access Key
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # GitHub Personal Access Token
    ("ghp", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
]

# 主搜索模式：sk- 前缀 + 32 位小写字母数字（GitHub 代码搜索正则）
KEY_PATTERN = KEY_PATTERNS[0][1]

# 预筛特征：sk- 后跟 16+ 位字符，或专用前缀变体（proj-/ant-/or- 等）后跟 16+ 位。
# 用于抓取前按搜索片段快速淘汰 task-/mask-/risk- 等误匹配，避免无谓的全文抓取。
KEY_HINT_RE = re.compile(
    r"sk-(?:[a-z0-9]{16,}|(?:proj|ant|or|ar|sv|project|admin|live|test)-[A-Za-z0-9_-]{16,})")


@dataclass
class Hit:
    """一条搜索结果：命中的文件及其上下文。"""
    repo: str                      # owner/name
    path: str                      # 仓库内路径
    html_url: str                  # 原文件链接
    language: str                  # 语言（GitHub 识别）
    fragments: list[str] = field(default_factory=list)   # text_matches 片段
    full_text: str = ""            # 文件全文（可选抓取）
    key_count: int = 0             # 提取出的密钥数量（搜索后填充）
    repo_pushed_at: str = ""       # 经 GitHub 仓库元数据核验的 UTC 时间
    content_sha: str = ""          # GitHub blob SHA；同路径修改后仍可重新分析

    @property
    def text(self) -> str:
        """用于分析的综合文本：全文优先，否则拼接片段。"""
        if self.full_text:
            return self.full_text
        return "\n---\n".join(self.fragments)


class GitHubCodeSearch:
    """封装 GitHub 代码搜索 API 的分页拉取与限速控制。"""

    def __init__(self, token: str, per_page: int = 100, max_pages: int = 10,
                 sort: str = "indexed", min_interval: float = 6.5,
                 fetch_content: bool = True):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": ACCEPT_HEADER,
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self.per_page = per_page
        self.max_pages = max_pages
        self.sort = sort
        self.min_interval = min_interval
        self.fetch_content = fetch_content
        self._last_request_at = 0.0
        self._last_raw_at = 0.0
        self._last_metadata_at = 0.0
        self.search_errors = 0
        self.search_incomplete_queries = 0

    # ---------- 内部工具 ----------
    def _throttle(self) -> None:
        """保证两次请求间隔不小于 min_interval（代码搜索 10 次/分钟）。"""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _throttle_raw(self) -> None:
        """raw.githubusercontent.com 抓取轻节流（不限 API 配额，0.2s 足够）。"""
        elapsed = time.monotonic() - self._last_raw_at
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self._last_raw_at = time.monotonic()

    def _wait_until_reset(self, resp: requests.Response) -> None:
        """命中限速时按 X-RateLimit-Reset 休眠到重置时刻。"""
        reset = resp.headers.get("X-RateLimit-Reset")
        remaining = resp.headers.get("X-RateLimit-Remaining", "1")
        if resp.status_code in (403, 429) and reset and remaining == "0":
            wait = max(0, int(reset) - int(time.time())) + 2
            print(f"[限速] 配额已用尽，休眠 {wait}s 等待重置…")
            time.sleep(wait)

    # ---------- 对外接口 ----------
    @staticmethod
    def build_queries(extensions: list[str], pattern: str = "sk-",
                      global_pages: int = 1) -> list[tuple[str, int]]:
        """构造查询列表：[(查询串, 页数上限), ...]。

        - 每个可识别语言一条查询（`{pattern} language:{lang}`），页数取 max_pages；
        - 存在无语言映射的扩展名（.env/.conf/.cfg 等）时，追加一条不带 language
          的全局兜底查询（任意文件类型，最新索引优先），页数取 global_pages；
        - 不用 `path:` 正则与 `indexed:` 限定符：实测（2026-08）在部分令牌下
          二者静默返回 0 命中；窗口语义改由本地文件指纹去重承担。
        """
        clean_exts = [e.lstrip(".").lower() for e in extensions
                      if e and re.fullmatch(r"[A-Za-z0-9_+-]+", e.lstrip("."))]
        if not clean_exts:
            raise ValueError("至少需要一个合法扩展名")
        pattern = pattern.strip() or "sk-"
        langs = sorted({LANGUAGE_MAP[e] for e in clean_exts if e in LANGUAGE_MAP})
        queries: list[tuple[str, int]] = []
        for lang in langs:
            queries.append((f"{pattern} language:{lang}", 0))  # 0 = 用 max_pages
        uncovered = [e for e in clean_exts if e not in LANGUAGE_MAP]
        if uncovered and global_pages > 0:
            queries.append((pattern, global_pages))  # 全局兜底：任意文件类型
        if not queries:
            queries.append((pattern, global_pages))
        return queries

    def search(self, queries: list[tuple[str, int]] | str) -> list[Hit]:
        """执行一组查询（或单个查询串）的分页搜索，按查询顺序合并去重。

        每查询独立按 sort=indexed 倒序拉取（最新被索引的优先），
        同一文件（repo/path）在多个查询中重复命中时只保留首次出现。
        """
        if isinstance(queries, str):
            queries = [(queries, 0)]
        hits: list[Hit] = []
        self.search_errors = 0
        self.search_incomplete_queries = 0
        seen: set[str] = set()
        for query, pages in queries:
            # REST uses legacy code-search syntax; is:public is not supported
            # there and narrows the search as a text term. Enforce visibility
            # below before reading fragments or constructing a Hit.
            for page in range(1, (pages or self.max_pages) + 1):
                self._throttle()
                resp = self.session.get(SEARCH_API, params={
                    "q": query, "sort": self.sort, "order": "desc",
                    "per_page": self.per_page, "page": page,
                })
                self._wait_until_reset(resp)
                if resp.status_code != 200:
                    self.search_errors += 1
                    msg = resp.json().get("message", resp.text[:200]) if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:200]
                    if page == 1:
                        print(f"[警告] 查询“{query}”失败（HTTP {resp.status_code}）：{msg[:120]}，跳过该查询。")
                        break
                    print(f"[警告] 查询“{query}”第 {page} 页拉取失败（{resp.status_code}），提前结束该查询。")
                    break

                data = resp.json()
                if data.get("incomplete_results") is True:
                    self.search_incomplete_queries += 1
                items = data.get("items", [])
                for it in items:
                    # Fail closed: a token with private-repository access must
                    # still only feed explicitly public results to monitoring.
                    if it.get("repository", {}).get("private") is not False:
                        continue
                    repo = it["repository"]["full_name"]
                    path = it.get("path", "")
                    fk = f"{repo}/{path}"
                    if fk in seen:
                        continue
                    seen.add(fk)
                    hit = Hit(
                        repo=repo,
                        path=path,
                        html_url=it.get("html_url", ""),
                        language=it.get("language") or "",
                        fragments=[m.get("fragment", "") for m in it.get("text_matches", [])],
                        content_sha=it.get("sha", ""),
                    )
                    hits.append(hit)

                total = data.get("total_count", 0)
                print(f"  查询“{query}”第 {page} 页：命中 {len(items)} 条"
                      f"（累计 {len(hits)}，总数约 {total}）")
                if len(items) < self.per_page or len(hits) >= total:
                    break
        return hits

    def filter_recent_repositories(self, hits: list[Hit], window) -> tuple[list[Hit], dict]:
        """Verify push time before content fetch; metadata cached within this round."""
        cache = {}
        accepted = []
        stats = {"repos_checked": 0, "files_outside_time": 0, "files_unverified_time": 0}
        quota_exhausted = False
        for hit in hits:
            if hit.repo not in cache:
                pushed_at = None
                if not quota_exhausted and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", hit.repo):
                    elapsed = time.monotonic() - self._last_metadata_at
                    if elapsed < 0.1:
                        time.sleep(0.1 - elapsed)
                    self._last_metadata_at = time.monotonic()
                    stats["repos_checked"] += 1
                    try:
                        resp = self.session.get(f"https://api.github.com/repos/{hit.repo}",
                                                headers={"Accept": "application/vnd.github+json"}, timeout=15)
                        if resp.status_code in (403, 429):
                            quota_exhausted = True
                        if resp.status_code == 200:
                            metadata = resp.json()
                            if isinstance(metadata, dict) and metadata.get("private") is False:
                                raw = metadata.get("pushed_at")
                                if isinstance(raw, str):
                                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                                    if parsed.tzinfo is not None:
                                        pushed_at = parsed.astimezone(timezone.utc)
                    except (requests.RequestException, ValueError, TypeError):
                        pass
                cache[hit.repo] = pushed_at
                if stats["repos_checked"] and stats["repos_checked"] % 50 == 0:
                    print(f"   已核验 {stats['repos_checked']} 个候选仓库的推送时间")
            pushed_at = cache[hit.repo]
            if pushed_at is None:
                stats["files_unverified_time"] += 1
            elif not window.contains(pushed_at):
                stats["files_outside_time"] += 1
            else:
                hit.repo_pushed_at = pushed_at.isoformat()
                accepted.append(hit)
        accepted.sort(key=lambda hit: hit.repo_pushed_at, reverse=True)
        return accepted, stats

    def fetch_full_text(self, hit: Hit) -> str:
        """抓取文件全文（raw 域名直取，免 API 限速；失败忽略，如大文件/二进制）。"""
        if not hit.repo or not hit.path:
            return ""
        owner, repo = hit.repo.split("/", 1)
        url = RAW_API.format(owner=owner, repo=repo, path=hit.path)
        self._throttle_raw()
        try:
            resp = self.session.get(url, timeout=30)
        except requests.RequestException:
            return ""
        if resp.status_code != 200:
            return ""
        return resp.text[:200_000]  # 防止超大文件拖垮分析


def extract_keys(text: str) -> list[str]:
    """从文本中提取所有已知格式的密钥（去重保序）。

    覆盖 sk-* 变体（OpenAI/Anthropic/OpenRouter/AnyRouter）、gsk_（Groq）、
    xai-（xAI）、tvly-（Tavily）、sk_（ElevenLabs）、AKIA（AWS）、ghp_（GitHub）。
    """
    seen: set[str] = set()
    keys = []
    for _, pat in KEY_PATTERNS:
        for m in pat.finditer(text):
            k = m.group(0)
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys
