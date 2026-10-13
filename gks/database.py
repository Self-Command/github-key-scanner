# -*- coding: utf-8 -*-
"""
SQLite 数据库模块：存储 AI 推测结果。

表 api_keys 字段：
- api_key        密钥本体（唯一）
- provider       AI 推测的服务商
- api_base_url   AI 推测的 API_BASE 链接
- confidence     AI 置信度（0~1）
- category       密钥分类：llm / cloud / other / unknown
- source_url     原始文件链接（GitHub）
- repo / file_path / language  来源定位信息
- context        代码上下文（分析依据）
- notes          AI 备注
- status         人工标记状态：unknown / active / invalid / revoked
- first_seen / last_seen

表 scan_tasks   每轮扫描的任务记录（范围、命中数、新增数、状态）
表 scan_cursor  增量游标：最近成功扫描的时间窗口终点（下轮以此为起点）
表 scanned_files 文件指纹去重：repo/path 已扫描记录，避免重复处理
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class KeyDatabase:
    """SQLite 封装，线程安全（每操作独立连接，用完即关）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---------- 基础 ----------
    @contextmanager
    def _connect(self):
        """连接上下文：提交事务并在退出时关闭连接，避免文件锁残留。"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            # Gunicorn workers and daemon can initialize a shared volume at once.
            # Serialize the read/ALTER migration sequence across all processes.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_key     TEXT NOT NULL UNIQUE,
                    provider    TEXT DEFAULT 'unknown',
                    api_base_url TEXT DEFAULT '',
                    confidence  REAL DEFAULT 0,
                    category    TEXT DEFAULT 'unknown',
                    source_url  TEXT DEFAULT '',
                    repo        TEXT DEFAULT '',
                    file_path   TEXT DEFAULT '',
                    language    TEXT DEFAULT '',
                    context     TEXT DEFAULT '',
                    notes       TEXT DEFAULT '',
                    status      TEXT DEFAULT 'unknown',
                    first_seen  TEXT DEFAULT (datetime('now','localtime')),
                    last_seen   TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_provider ON api_keys(provider)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON api_keys(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_base ON api_keys(api_base_url)")
            # 旧库迁移：已有表缺少 category 列时补充（必须先补列再建索引）
            cols = [r[1] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()]
            if "category" not in cols:
                conn.execute("ALTER TABLE api_keys ADD COLUMN category TEXT DEFAULT 'unknown'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON api_keys(category)")
            # 旧库迁移：密钥有效性测试记录列（幂等）
            if "last_tested_at" not in cols:
                conn.execute("ALTER TABLE api_keys ADD COLUMN last_tested_at TEXT DEFAULT ''")
            if "last_test_result" not in cols:
                conn.execute("ALTER TABLE api_keys ADD COLUMN last_test_result TEXT DEFAULT ''")

            # ---- 扫描任务记录（每轮扫描一行）----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_tasks (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger        TEXT DEFAULT 'manual',   -- manual / schedule / web
                    mode           TEXT DEFAULT 'real',     -- real / demo
                    status         TEXT DEFAULT 'running',  -- running / success / error
                    window_from    TEXT DEFAULT '',         -- 本轮窗口起点（增量扫描起点）
                    window_to      TEXT DEFAULT '',         -- 本轮窗口终点（下轮游标）
                    pages          INTEGER DEFAULT 0,       -- 请求页数上限
                    hits           INTEGER DEFAULT 0,       -- 命中文件数
                    files_analyzed INTEGER DEFAULT 0,       -- 实际分析文件数
                    files_skipped  INTEGER DEFAULT 0,       -- 文件指纹去重跳过数
                    keys_found     INTEGER DEFAULT 0,       -- 提取密钥总数
                    keys_new       INTEGER DEFAULT 0,       -- 新增入库
                    keys_updated   INTEGER DEFAULT 0,       -- 更新已有
                    error          TEXT DEFAULT '',
                    started_at     TEXT DEFAULT (datetime('now','localtime')),
                    finished_at    TEXT DEFAULT '',
                    duration       REAL DEFAULT 0           -- 耗时秒
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_started ON scan_tasks(started_at DESC)")
            task_cols = {r[1] for r in conn.execute("PRAGMA table_info(scan_tasks)")}
            for name, definition in {
                "repo_time_preset": "TEXT DEFAULT ''", "repo_pushed_from": "TEXT DEFAULT ''",
                "repo_pushed_before": "TEXT DEFAULT ''", "repos_checked": "INTEGER DEFAULT 0",
                "files_outside_time": "INTEGER DEFAULT 0", "files_unverified_time": "INTEGER DEFAULT 0",
                "search_errors": "INTEGER DEFAULT 0", "search_incomplete_queries": "INTEGER DEFAULT 0",
            }.items():
                if name not in task_cols:
                    conn.execute(f"ALTER TABLE scan_tasks ADD COLUMN {name} {definition}")

            # ---- 增量游标（单行表）----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_cursor (
                    id             INTEGER PRIMARY KEY CHECK (id = 1),
                    last_scan_at   TEXT DEFAULT '',
                    last_window_to TEXT DEFAULT ''
                )
            """)
            conn.execute("INSERT OR IGNORE INTO scan_cursor (id) VALUES (1)")

            # ---- 文件指纹去重 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scanned_files (
                    file_key   TEXT PRIMARY KEY,   -- repo/path
                    first_seen TEXT DEFAULT (datetime('now','localtime')),
                    last_seen  TEXT DEFAULT (datetime('now','localtime'))
                )
            """)

    # ---------- 写入 ----------
    def upsert(self, record: dict) -> bool:
        """插入新密钥；已存在则更新 last_seen。返回是否为新插入。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        category = record.get("category") or "unknown"
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id FROM api_keys WHERE api_key = ?", (record["api_key"],))
            row = cur.fetchone()
            if row:
                conn.execute(
                    "UPDATE api_keys SET last_seen=?, provider=?, api_base_url=?, "
                    "confidence=?, category=?, source_url=?, repo=?, file_path=?, "
                    "language=?, notes=? WHERE id=?",
                    (now, record.get("provider", "unknown"),
                     record.get("api_base", ""), record.get("confidence", 0),
                     category, record.get("source_url", ""), record.get("repo", ""),
                     record.get("file_path", ""), record.get("language", ""),
                     record.get("notes", ""), row["id"]))
                return False
            conn.execute("""
                INSERT INTO api_keys
                    (api_key, provider, api_base_url, confidence, category,
                     source_url, repo, file_path, language, context, notes, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?, 'unknown')
            """, (record["api_key"], record.get("provider", "unknown"),
                  record.get("api_base", ""), record.get("confidence", 0),
                  category, record.get("source_url", ""), record.get("repo", ""),
                  record.get("file_path", ""), record.get("language", ""),
                  record.get("context", "")[:4000], record.get("notes", "")))
            return True

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]

    # ---------- 扫描任务与增量游标 ----------
    def start_task(self, trigger: str = "manual", mode: str = "real",
                   window_from: str = "", window_to: str = "", pages: int = 0) -> int:
        """登记一轮扫描开始，返回 task_id。"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO scan_tasks (trigger, mode, window_from, window_to, pages) "
                "VALUES (?,?,?,?,?)",
                (trigger, mode, window_from, window_to, pages))
            return cur.lastrowid

    def set_task_repo_scope(self, task_id: int, window) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE scan_tasks SET repo_time_preset=?, repo_pushed_from=?, repo_pushed_before=? WHERE id=?",
                         (window.preset, window.start.isoformat(), window.end.isoformat(), task_id))

    def set_task_repo_counts(self, task_id: int, counts: dict) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE scan_tasks SET repos_checked=?, files_outside_time=?, files_unverified_time=? WHERE id=?",
                         (counts["repos_checked"], counts["files_outside_time"], counts["files_unverified_time"], task_id))

    def set_task_search_counts(self, task_id: int, errors: int, incomplete: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE scan_tasks SET search_errors=?, search_incomplete_queries=? WHERE id=?",
                         (errors, incomplete, task_id))

    def finish_task(self, task_id: int, status: str = "success",
                    hits: int = 0, files_analyzed: int = 0, files_skipped: int = 0,
                    keys_found: int = 0, keys_new: int = 0, keys_updated: int = 0,
                    error: str = "", advance_cursor: bool = True) -> None:
        """结算一轮扫描；仅成功且允许时推进增量游标。"""
        with self._connect() as conn:
            row = conn.execute("SELECT started_at FROM scan_tasks WHERE id = ?",
                               (task_id,)).fetchone()
            duration = 0.0
            if row:
                try:
                    fmt = "%Y-%m-%d %H:%M:%S"
                    duration = (datetime.strptime(datetime.now().strftime(fmt), fmt)
                                - datetime.strptime(row["started_at"], fmt)).total_seconds()
                except ValueError:
                    duration = 0.0
            conn.execute(
                "UPDATE scan_tasks SET status=?, hits=?, files_analyzed=?, files_skipped=?, "
                "keys_found=?, keys_new=?, keys_updated=?, error=?, "
                "finished_at=datetime('now','localtime'), duration=? WHERE id=?",
                (status, hits, files_analyzed, files_skipped,
                 keys_found, keys_new, keys_updated, error[:500], round(duration, 1), task_id))
            if status == "success" and advance_cursor:
                row = conn.execute("SELECT window_to FROM scan_tasks WHERE id = ?",
                                   (task_id,)).fetchone()
                if row and row["window_to"]:
                    conn.execute(
                        "UPDATE scan_cursor SET last_scan_at=datetime('now','localtime'), "
                        "last_window_to=? WHERE id=1", (row["window_to"],))

    def recent_tasks(self, limit: int = 10) -> list[dict]:
        """最近扫描任务（倒序）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scan_tasks ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 200)),)).fetchall()
        return [dict(r) for r in rows]

    def recover_stale_tasks(self, max_age_seconds: int) -> int:
        """把超过阈值仍为 running 的任务标记为异常中断，返回恢复数量。"""
        max_age = int(max_age_seconds)
        if max_age <= 0:
            raise ValueError("陈旧任务阈值必须是正整数秒")
        modifier = f"-{max_age} seconds"
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE scan_tasks SET status='error', "
                "error=CASE WHEN error='' THEN '进程异常退出或任务超时' ELSE error END, "
                "finished_at=datetime('now','localtime'), "
                "duration=ROUND((julianday('now','localtime')-julianday(started_at))*86400,1) "
                "WHERE status='running' "
                "AND started_at <= datetime('now','localtime', ?)",
                (modifier,))
            return cur.rowcount

    def operational_snapshot(self) -> dict:
        """返回健康检查所需的数据库完整性与任务状态快照。"""
        with self._connect() as conn:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            running = conn.execute(
                "SELECT * FROM scan_tasks WHERE status='running' ORDER BY id DESC LIMIT 20"
            ).fetchall()
            last_task = conn.execute(
                "SELECT * FROM scan_tasks ORDER BY id DESC LIMIT 1").fetchone()
            last_success = conn.execute(
                "SELECT * FROM scan_tasks WHERE status='success' AND mode='real' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "integrity": integrity,
            "running_tasks": [dict(r) for r in running],
            "last_task": dict(last_task) if last_task else None,
            "last_real_success": dict(last_success) if last_success else None,
        }

    def get_cursor(self) -> dict:
        """增量游标：最近成功扫描时间与窗口终点。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scan_cursor WHERE id = 1").fetchone()
        return dict(row) if row else {"last_scan_at": "", "last_window_to": ""}

    # ---------- 文件指纹去重 ----------
    def should_scan_file(self, file_key: str, since: str = "") -> bool:
        """文件是否应扫描：从未扫过 → True；最近扫描时间早于窗口起点
        （说明文件被重新索引进新窗口）→ True；否则 False。"""
        with self._connect() as conn:
            row = conn.execute("SELECT last_seen FROM scanned_files WHERE file_key = ?",
                               (file_key,)).fetchone()
        if not row:
            return True
        return bool(since) and row["last_seen"] < since

    def mark_file_scanned(self, file_key: str) -> None:
        """登记文件已扫描（存在则刷新 last_seen）。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO scanned_files (file_key) VALUES (?) "
                "ON CONFLICT(file_key) DO UPDATE SET "
                "last_seen = datetime('now','localtime')", (file_key,))

    # ---------- 查询 ----------
    def query(self, q: str = "", provider: str = "", status: str = "",
              category: str = "", min_conf: float = 0.0,
              page: int = 1, size: int = 20) -> tuple[list[dict], int]:
        """筛选分页查询，返回 (记录列表, 总条数)。min_conf 为置信度下限（0~1，5 分制即分数/5）。"""
        where, args = [], []
        if q:
            like = f"%{q}%"
            where.append("(api_key LIKE ? OR provider LIKE ? OR repo LIKE ? "
                         "OR file_path LIKE ? OR notes LIKE ? OR api_base_url LIKE ?)")
            args += [like] * 6
        if provider:
            where.append("provider = ?")
            args.append(provider)
        if status:
            where.append("status = ?")
            args.append(status)
        if category:
            where.append("category = ?")
            args.append(category)
        if min_conf > 0:
            where.append("confidence >= ?")
            args.append(min_conf)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        offset = (max(page, 1) - 1) * size
        with self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM api_keys{cond}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM api_keys{cond} ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [size, offset]).fetchall()
        return [dict(r) for r in rows], total

    def get(self, key_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return dict(row) if row else None

    def find_key_id(self, api_key: str) -> int | None:
        """按密钥原文查 id（自动测试时定位记录）。"""
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM api_keys WHERE api_key = ?",
                               (api_key,)).fetchone()
        return row["id"] if row else None

    def update_status(self, key_id: int, status: str) -> bool:
        if status not in ("unknown", "active", "invalid", "revoked"):
            return False
        with self._connect() as conn:
            cur = conn.execute("UPDATE api_keys SET status=? WHERE id=?", (status, key_id))
            return cur.rowcount > 0

    def record_test(self, key_id: int, result: dict) -> None:
        """记录一次有效性测试结果（含时间戳），供前端展示。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE api_keys SET last_tested_at=?, last_test_result=? WHERE id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 json.dumps(result, ensure_ascii=False), key_id))

    # ---------- 统计 ----------
    def stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
            today = conn.execute(
                "SELECT COUNT(*) FROM api_keys WHERE first_seen >= date('now','localtime')"
            ).fetchone()[0]
            high_conf = conn.execute(
                "SELECT COUNT(*) FROM api_keys WHERE confidence >= 0.8").fetchone()[0]
            providers = conn.execute(
                "SELECT provider, COUNT(*) c FROM api_keys WHERE provider != '' "
                "GROUP BY provider ORDER BY c DESC LIMIT 12").fetchall()
            by_status = conn.execute(
                "SELECT status, COUNT(*) c FROM api_keys GROUP BY status").fetchall()
            by_category = conn.execute(
                "SELECT category, COUNT(*) c FROM api_keys GROUP BY category").fetchall()
        return {
            "total": total,
            "today": today,
            "high_conf": high_conf,
            "provider_count": len(providers),
            "providers": [{"name": r["provider"], "count": r["c"]} for r in providers],
            "status": {r["status"]: r["c"] for r in by_status},
            "category": {r["category"]: r["c"] for r in by_category},
        }

    # ---------- 导出 ----------
    def export_csv(self, q: str = "", provider: str = "", status: str = "",
                   category: str = "", min_conf: float = 0.0) -> str:
        """导出 CSV 文本（带 UTF-8 BOM，Excel 打开中文不乱码）。"""
        rows, _ = self.query(q=q, provider=provider, status=status, category=category,
                             min_conf=min_conf, page=1, size=10_000_000)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ID", "API_KEY", "服务商", "API_BASE_URL", "置信度", "分类",
                         "原链接", "仓库", "文件路径", "语言", "备注", "状态",
                         "首次发现", "最后出现"])
        for r in rows:
            writer.writerow([r["id"], r["api_key"], r["provider"], r["api_base_url"],
                             r["confidence"], r["category"], r["source_url"], r["repo"],
                             r["file_path"], r["language"], r["notes"], r["status"],
                             r["first_seen"], r["last_seen"]])
        return "\ufeff" + buf.getvalue()
