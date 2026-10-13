"""Redacted public-repository monitoring: no usable credentials at rest."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from gks.database import KeyDatabase
from gks.endpoints import ENDPOINTS, match_by_domain
from gks.github_search import extract_keys

IDENTITY_RE = re.compile(r"^(?P<mask>[^\r\n|]+…[^\r\n|]+) \| sha256:(?P<fingerprint>[0-9a-f]{64})$")
MONITORING_APP_ID = 1701147275
SECRET_NAME = r"[\w.-]*(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|pwd|credential|authorization)[\w.-]*"
ASSIGNMENT_RE = re.compile(
    rf"(?<![\w.-])[\"']?{SECRET_NAME}[\"']?\s*[:=]\s*"
    r"(?:(?P<quote>[\"'])(?P<quoted>[^\r\n]*?)(?P=quote)|(?P<plain>[^\s,;\}\]]+))", re.I)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
PLACEHOLDER_RE = re.compile(r"^(?:KEY_CANDIDATE_\d+|\[REDACTED\])$")


def _decoded_views(text: str):
    """Keep original offsets while decoding percent escapes, including nested ones."""
    mapping = [(i, i + 1) for i in range(len(text))]
    for _ in range(9):
        yield text, mapping
        chars, spans, i = [], [], 0
        changed = False
        while i < len(text):
            if text[i] == "%" and re.fullmatch(r"[0-9a-fA-F]{2}", text[i + 1:i + 3]):
                chars.append(chr(int(text[i + 1:i + 3], 16)))
                spans.append((mapping[i][0], mapping[i + 2][1]))
                i += 3
                changed = True
            else:
                chars.append(text[i])
                spans.append(mapping[i])
                i += 1
        if not changed:
            break
        text, mapping = "".join(chars), spans


def _secret_spans(text: str, known_keys):
    candidates = set(known_keys) | set(extract_keys(text))
    for key in candidates:
        if key and not IDENTITY_RE.fullmatch(key):
            for match in re.finditer(re.escape(key), text):
                yield match.span()
    for match in ASSIGNMENT_RE.finditer(text):
        group = "quoted" if match.group("quote") else "plain"
        if match.group(group) and not PLACEHOLDER_RE.fullmatch(match.group(group)):
            yield match.span(group)
    # URL query values and userinfo need no provider-specific key prefix.
    for match in URL_RE.finditer(text):
        url, offset = match.group(), match.start()
        authority_end = min((i for i in (url.find("/", url.find("://") + 3),
                                        url.find("?"), url.find("#")) if i >= 0), default=len(url))
        at = url.rfind("@", 0, authority_end)
        if at >= 0:
            yield offset + url.find("://") + 3, offset + at
        query = url.find("?")
        if query >= 0:
            end = url.find("#", query)
            end = len(url) if end < 0 else end
            for value in re.finditer(r"(?:^|[&;])[^=&;]*=([^&;]+)", url[query + 1:end]):
                if not PLACEHOLDER_RE.fullmatch(value.group(1)):
                    yield offset + query + 1 + value.start(1), offset + query + 1 + value.end(1)



def key_identity(key: str) -> str:
    """Stable digest plus a partial mask; hashing twice must not change identity."""
    if IDENTITY_RE.fullmatch(key):
        return key
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()
    prefix = key[:8] if len(key) > 16 else key[:2]
    suffix = key[-4:] if len(key) > 16 else key[-2:]
    return f"{prefix}…{suffix} | sha256:{fingerprint}"


def redact_text(text: str, known_keys: list[str] | tuple[str, ...] = ()) -> str:
    """Scrub credential formats, named secret values and encoded URL credentials."""
    spans = []
    for view, mapping in _decoded_views(text):
        for start, end in _secret_spans(view, known_keys):
            if end > start:
                spans.append((mapping[start][0], mapping[end - 1][1]))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts, cursor = [], 0
    for start, end in merged:
        parts.extend((text[cursor:start], "[REDACTED]"))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def sanitize_record(record: dict) -> dict:
    raw_key = record["api_key"]
    known_keys = [raw_key]
    for value in record.values():
        if isinstance(value, str):
            known_keys.extend(extract_keys(value))
    clean = {name: redact_text(value, known_keys) if isinstance(value, str) else value
             for name, value in record.items()}
    clean["api_key"] = key_identity(raw_key)
    # Full source files can contain unrelated passwords or secrets too.
    clean["context"] = ""
    # Endpoint paths and free-form notes can embed unlabelled gateway passwords.
    # Retain only known, static endpoint metadata; unknown gateways stay unknown.
    known_providers = {endpoint["provider"] for endpoint in ENDPOINTS}
    other_bases = {"AWS": "", "GitHub": "https://api.github.com",
                   "Tavily": "https://api.tavily.com", "ElevenLabs": "https://api.elevenlabs.io"}
    if clean.get("provider") not in known_providers | set(other_bases):
        clean["provider"] = "unknown"
    raw_base = str(record.get("api_base", ""))
    endpoints = match_by_domain(raw_base)
    if endpoints:
        bases = endpoints[0]["base_urls"]
        matches = [base for base in bases if raw_base == base or any(raw_base.startswith(base + sep) for sep in ("/", "?", "#"))]
        clean["api_base"] = max(matches, key=len) if matches else bases[0]
    else:
        clean["api_base"] = other_bases.get(clean["provider"], "")
    clean["notes"] = "按公开文件线索推测；端点仅保留已知服务商的标准地址；未验证有效性。"
    return clean


def public_fields(row: dict | None) -> dict | None:
    if row is None:
        return None
    match = IDENTITY_RE.fullmatch(row["api_key"])
    if not match:
        raise RuntimeError("Refusing to serve a plaintext credential record")
    return {**row, "key_mask": match["mask"], "key_fingerprint": match["fingerprint"]}


class MonitoringDatabase(KeyDatabase):
    """Enforce redaction at the persistence boundary and refuse legacy raw data."""

    def __init__(self, db_path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize first initialization across web workers. Never migrate a legacy
        # file: deleted plaintext can remain in SQLite free pages and journals.
        with closing(sqlite3.connect(path, timeout=30)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            app_id = conn.execute("PRAGMA application_id").fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id != MONITORING_APP_ID:
                if tables:
                    raise RuntimeError("Legacy plaintext database cannot be used for public monitoring; use a new database")
                conn.execute(f"PRAGMA application_id={MONITORING_APP_ID}")
        super().__init__(db_path)
        with self._connect() as conn:
            for row in conn.execute("SELECT * FROM api_keys"):
                if not IDENTITY_RE.fullmatch(row["api_key"]) or row["context"]:
                    raise RuntimeError("Refusing inconsistent monitoring database")
                for name, value in dict(row).items():
                    if name != "api_key" and isinstance(value, str) and redact_text(value) != value:
                        raise RuntimeError("Refusing unsanitized monitoring metadata")

    def upsert(self, record: dict) -> bool:
        return super().upsert(sanitize_record(record))

    def query(self, *args, **kwargs):
        rows, total = super().query(*args, **kwargs)
        return [public_fields(row) for row in rows], total

    def get(self, key_id: int):
        return public_fields(super().get(key_id))

    def find_key_id(self, api_key: str):
        return super().find_key_id(key_identity(api_key))

    @staticmethod
    def _file_identity(file_key: str) -> str:
        return "sha256:" + hashlib.sha256(file_key.encode("utf-8")).hexdigest()

    def should_scan_file(self, file_key: str, since: str = "") -> bool:
        return super().should_scan_file(self._file_identity(file_key), since)

    def mark_file_scanned(self, file_key: str) -> None:
        super().mark_file_scanned(self._file_identity(file_key))

    def finish_task(self, task_id: int, **kwargs):
        if "error" in kwargs:
            kwargs["error"] = redact_text(str(kwargs["error"]))
        return super().finish_task(task_id, **kwargs)

    def record_test(self, key_id: int, result: dict) -> None:
        raise RuntimeError("Credential validity probing is disabled in public monitoring")

    def export_csv(self, *args, **kwargs):
        return super().export_csv(*args, **kwargs).replace("ID,API_KEY,", "ID,密钥掩码与SHA256指纹,", 1)
