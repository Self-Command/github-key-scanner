# -*- coding: utf-8 -*-
"""
github-key-scanner 命令行入口。

用法示例：
  # 真实扫描（需在 .env 配置 GITHUB_TOKEN；AI 分析需配置 AI_API_KEY，否则自动用规则引擎）
  python main.py scan

  # 演示模式：无需任何令牌，用内置示例数据跑通 搜索→分析→入库 全流程
  python main.py scan --demo

  # 只分析样例、不连 GitHub：--demo 等价；指定 AI 模式
  python main.py scan --demo --ai-mode ai|rules|auto

  # 常用参数
  python main.py scan --max-pages 5 --extensions py json js ts --no-fetch-content
  python main.py export        # 导出全量 CSV 到 data/keys.csv
  python main.py stats         # 打印数据库统计
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from config import BASE_DIR, config
from gks.analyzer import AIAnalyzer, RuleAnalyzer, make_analyzer
from gks.database import KeyDatabase
from gks.endpoints import normalize_base_url
from gks.github_search import GitHubCodeSearch, Hit, KEY_HINT_RE, extract_keys
from gks.health import collect_health

SAMPLE_FILE = BASE_DIR / "tests" / "sample_results.json"


# ----------------------------------------------------------------------
# 扫描主流程
# ----------------------------------------------------------------------
def load_sample_hits() -> list[Hit]:
    """读取演示数据（模拟 GitHub 搜索结果，含片段与全文）。"""
    data = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
    hits = []
    for it in data["items"]:
        hit = Hit(
            repo=it["repository"]["full_name"],
            path=it["path"],
            html_url=it["html_url"],
            language=it.get("language", ""),
            fragments=[m["fragment"] for m in it.get("text_matches", [])],
            full_text=it.get("content", ""),
        )
        hits.append(hit)
    return hits


def compute_window(db: KeyDatabase, since_override: str, incremental: bool) -> tuple[str, str]:
    """计算本轮扫描的时间窗口。

    - since_override 指定则以其为窗口起点（--since）；
    - 增量模式：起点 = 上次成功扫描的窗口终点（游标），无游标 = 首次全量（不限定起点）；
    - 窗口终点恒为今天（GitHub indexed:>= 粒度到日期），成功完成后作为下轮游标。
    返回 (window_from, window_to)，window_from 为空表示全量。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if since_override:
        return since_override, today
    if incremental:
        cursor = db.get_cursor()
        last = cursor.get("last_window_to", "")
        return (last, today) if last else ("", today)
    return "", today


def _recover_stale_tasks(db: KeyDatabase) -> int:
    """启动扫描前回收异常中断遗留任务。"""
    recovered = db.recover_stale_tasks(config.STALE_TASK_SECONDS)
    if recovered:
        print(f"[恢复] 已将 {recovered} 个陈旧 running 任务标记为 error。")
    return recovered


def do_scan(args: argparse.Namespace, db: KeyDatabase, trigger: str = "manual") -> dict:
    """执行一轮扫描：搜索 → 文件去重 → 分析 → 入库 → 结算任务记录。

    返回统计 dict（供 CLI 打印与 daemon 调度复用）。
    """
    window_from, window_to = compute_window(db, getattr(args, "since", ""),
                                            getattr(args, "incremental", True))
    pages = args.max_pages or config.MAX_PAGES
    task_id = db.start_task(trigger=trigger, mode="demo" if args.demo else "real",
                            window_from=window_from, window_to=window_to, pages=pages)

    stats = {"hits": 0, "analyzed": 0, "skipped": 0,
             "keys_found": 0, "keys_new": 0, "keys_updated": 0,
             "ok": False}
    new_llm: list[dict] = []   # 本轮新增的大模型密钥（入库后自动测试）
    try:
        # ---- 1. 获取命中文件 ----
        if args.demo:
            print("== 演示模式：读取内置示例数据（不访问 GitHub API）==")
            hits = load_sample_hits()
            print(f"   示例命中 {len(hits)} 个文件")
        else:
            if not config.GITHUB_TOKEN:
                print("[错误] 未配置 GITHUB_TOKEN。请复制 .env.example 为 .env 并填写，"
                      "或使用 --demo 先体验演示模式。", file=sys.stderr)
                db.finish_task(task_id, status="error", error="未配置 GITHUB_TOKEN")
                return stats
            print("== GitHub 代码搜索 ==")
            searcher = GitHubCodeSearch(
                token=config.GITHUB_TOKEN,
                per_page=config.PER_PAGE,
                max_pages=pages,
                sort=config.SORT,
                min_interval=config.MIN_INTERVAL,
                fetch_content=config.FETCH_FILE_CONTENT,
            )
            queries = searcher.build_queries(
                args.extensions or config.DEFAULT_EXTENSIONS,
                pattern=config.SEARCH_PATTERN,
                global_pages=config.SEARCH_GLOBAL_PAGES)
            scope = f"（本地窗口 {window_from or '全量'} ~ {window_to}，GitHub 端按最新索引排序）"
            print(f"查询数: {len(queries)} 条（字面量 {config.SEARCH_PATTERN!r} + language 分查） {scope}")
            hits = searcher.search(queries)
            print(f"搜索完成，共 {len(hits)} 个候选文件，开始预筛与抓取…")

        stats["hits"] = len(hits)

        # ---- 1.5 预筛与文件指纹去重（抓取前完成，避免无谓网络与配额开销）----
        # --full 或显式 --since（手动补扫）时忽略文件去重，强制重扫
        filtered: list[Hit] = []
        for hit in hits:
            fk = f"{hit.repo}/{hit.path}"
            if not args.full and not args.since and not db.should_scan_file(fk, window_from):
                stats["skipped"] += 1
                continue
            if not args.demo and not KEY_HINT_RE.search(hit.text):
                # 搜索片段无密钥特征（task-/mask- 等误匹配）→ 不抓全文、不分析
                stats["skipped"] += 1
                continue
            filtered.append(hit)
        hits = filtered
        if stats["skipped"]:
            print(f"   预筛/去重：跳过 {stats['skipped']} 个（已扫描过或无密钥特征）"
                  f"（--full 可强制重扫）")

        # ---- 1.6 抓取文件全文（raw 直取，不限 API 配额；抓取后按严格正则二次过滤）----
        if not args.demo and config.FETCH_FILE_CONTENT and hits:
            print(f"== 抓取文件全文（{len(hits)} 个）==")
            for i, hit in enumerate(hits, 1):
                hit.full_text = searcher.fetch_full_text(hit)
                print(f"  [{i}/{len(hits)}] 已抓取 {hit.repo}/{hit.path}")
            hits = [h for h in hits if extract_keys(h.text)]
        elif not args.demo:
            hits = [h for h in hits if extract_keys(h.text)]

        # ---- 2. 逐文件 AI / 规则分析 ----
        print("== 密钥分析 ==")
        if args.ai_mode == "rules":
            analyzer: RuleAnalyzer | AIAnalyzer = RuleAnalyzer()
            print("   分析模式：规则引擎（--ai-mode rules 指定）")
        elif args.ai_mode == "ai":
            if not config.ai_ready:
                print("[错误] --ai-mode ai 需要配置 AI_API_KEY 与 AI_BASE_URL", file=sys.stderr)
                db.finish_task(task_id, status="error", error="未配置 AI")
                return stats
            analyzer = AIAnalyzer(config.AI_BASE_URL, config.AI_API_KEY, config.AI_MODEL,
                                  timeout=config.AI_TIMEOUT)
            print(f"   分析模式：AI（{config.AI_MODEL} @ {config.AI_BASE_URL}）")
        else:  # auto
            if config.ai_ready:
                analyzer = AIAnalyzer(config.AI_BASE_URL, config.AI_API_KEY, config.AI_MODEL,
                                      timeout=config.AI_TIMEOUT)
                print(f"   分析模式：AI（{config.AI_MODEL} @ {config.AI_BASE_URL}）")
            else:
                analyzer = RuleAnalyzer()
                print("   分析模式：规则引擎（未配置 AI，建议在 .env 填写 AI_API_KEY 提升准确率）")

        # AI_MAX_FILES 只限制外部 AI 调用量；超出部分必须继续用本地规则处理，
        # 否则成功推进游标会永久遗漏本轮尚未分析的文件。
        ai_limit = max(0, config.AI_MAX_FILES) if isinstance(analyzer, AIAnalyzer) else len(hits)
        fallback_analyzer = RuleAnalyzer()
        if isinstance(analyzer, AIAnalyzer) and len(hits) > ai_limit:
            print(f"   AI 文件上限为 {ai_limit}；其余 {len(hits) - ai_limit} 个文件改用规则引擎")

        for i, hit in enumerate(hits, 1):
            keys_in_text = extract_keys(hit.text)
            if not keys_in_text:
                continue
            stats["keys_found"] += len(keys_in_text)
            active_analyzer = analyzer if i <= ai_limit else fallback_analyzer
            mode_label = "AI" if isinstance(active_analyzer, AIAnalyzer) else "规则"
            print(f"  [{i}/{len(hits)}] 分析 {hit.repo}/{hit.path}"
                  f"（提取到 {len(keys_in_text)} 个密钥，{mode_label}）")
            for rec in active_analyzer.analyze(hit):
                if rec.get("api_base"):
                    rec["api_base"] = normalize_base_url(rec["api_base"],
                                                         rec.get("provider", ""))
                rec.update({
                    "source_url": hit.html_url,
                    "repo": hit.repo,
                    "file_path": hit.path,
                    "language": hit.language,
                    "context": hit.text[:4000],
                })
                if db.upsert(rec):
                    stats["keys_new"] += 1
                    flag = "新增"
                    if config.AUTO_TEST_LLM and rec.get("category") == "llm":
                        new_llm.append(rec)
                else:
                    stats["keys_updated"] += 1
                    flag = "更新"
                print(f"      [{flag}] {rec['api_key']} → {rec['provider']}"
                      f" ({rec.get('category', 'unknown')})"
                      f" | base={rec['api_base'] or '-'} | conf={rec['confidence']}"
                      f" | {rec['notes'][:60]}")
            db.mark_file_scanned(f"{hit.repo}/{hit.path}")
            stats["analyzed"] += 1

        print("\n== 扫描完成 ==")
        print(f"   窗口范围    : {window_from or '全量'} ~ {window_to}")
        print(f"   命中文件 {stats['hits']} 个，去重跳过 {stats['skipped']}，"
              f"实际分析 {stats['analyzed']} 个。")
        print(f"   新增密钥 {stats['keys_new']} 条，更新 {stats['keys_updated']} 条"
              f"（共提取 {stats['keys_found']}）。")
        print(f"   数据库现有 {db.count()} 条记录：{config.DB_PATH}")

        # ---- 3. 新增大模型密钥自动有效性测试（GET /models 只读探测，零消耗）----
        if config.AUTO_TEST_LLM and new_llm:
            from gks.key_tester import pick_base, test_key
            print(f"\n== 自动测试新增 {len(new_llm)} 个大模型密钥（/v1/models 只读探测）==")
            for rec in new_llm:
                key_id = db.find_key_id(rec["api_key"])
                base = pick_base(rec)
                if not key_id or not base:
                    print(f"   [跳过] {rec['provider']} {rec['api_key'][:16]}…：无法定位记录或无端点")
                    continue
                r = test_key(rec["api_key"], base)
                new_status = None
                if r["verdict"] == "active":
                    new_status = "active"
                elif r["verdict"] == "invalid":
                    new_status = "invalid"
                if new_status:
                    db.update_status(key_id, new_status)
                db.record_test(key_id, r)
                extra = f" → 状态={new_status}" if new_status else ""
                print(f"   [{r['verdict']}] {rec['provider']} {rec['api_key'][:16]}…"
                      f" HTTP {r.get('status_code', '-')} 模型数 {r.get('models', '-')}"
                      f" {r.get('elapsed', '-')}s{extra}"
                      + (f" | {r['error'][:80]}" if r.get("error") else ""))

        db.finish_task(task_id, status="success",
                       hits=stats["hits"], files_analyzed=stats["analyzed"],
                       files_skipped=stats["skipped"], keys_found=stats["keys_found"],
                       keys_new=stats["keys_new"], keys_updated=stats["keys_updated"],
                       advance_cursor=not args.demo)
        stats["ok"] = True
    except KeyboardInterrupt:
        print("[中断] 本轮扫描已由用户终止。", file=sys.stderr)
        db.finish_task(task_id, status="error", error="用户中断")
    except Exception as e:  # noqa: BLE001 —— daemon 循环中单轮失败不中断
        print(f"[错误] 本轮扫描失败：{e}", file=sys.stderr)
        db.finish_task(task_id, status="error", error=str(e)[:500])
    return stats


def run_scan(args: argparse.Namespace) -> int:
    db = KeyDatabase(config.DB_PATH)
    _recover_stale_tasks(db)
    stats = do_scan(args, db, trigger="manual")
    return 0 if stats["ok"] else 1


def run_daemon(args: argparse.Namespace) -> int:
    """全自动托管：按固定间隔循环执行扫描，支持触发文件立即开跑。"""
    import signal
    import time

    db = KeyDatabase(config.DB_PATH)
    _recover_stale_tasks(db)
    interval = args.interval or config.SCAN_INTERVAL
    if interval <= 0:
        print("[错误] 扫描间隔必须是正整数秒。", file=sys.stderr)
        return 2
    stop = {"flag": False}

    def _stop(signum, frame):  # noqa: ARG001
        print("\n收到退出信号，完成本轮后停止…")
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print("== 全自动托管模式（daemon）==")
    print(f"   扫描间隔：{interval} 秒（--interval 可调，或 .env SCAN_INTERVAL）")
    print(f"   增量窗口：{'开启' if args.incremental else '关闭'}（--no-incremental 关闭）")
    print(f"   触发文件：{config.TRIGGER_FILE}（Web 端『立即扫描』会写入该文件）")
    if args.once:
        print("   --once：仅执行一轮后退出。")
    print("   Ctrl+C 退出。\n")

    first = True
    last_ok = True
    while not stop["flag"]:
        if not first:
            # 分段等待以响应 Web 触发文件，避免一次 sleep 阻塞整个扫描间隔。
            deadline = time.monotonic() + interval
            while not stop["flag"] and not config.TRIGGER_FILE.exists():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(1.0, remaining))
        first = False
        if stop["flag"]:
            break

        trigger = "schedule"
        if config.TRIGGER_FILE.exists():
            try:
                config.TRIGGER_FILE.unlink()
                trigger = "web"
                print("[触发] 收到手动触发，立即开始本轮扫描。")
            except OSError:
                pass
        last_tasks = db.recent_tasks(1)
        next_id = (last_tasks[0]["id"] + 1) if last_tasks else 1
        print(f"--- 第 {next_id} 轮扫描开始："
              f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
        result = do_scan(args, db, trigger=trigger)
        last_ok = result["ok"]
        if args.once:
            break
        print(f"--- 本轮结束，{interval} 秒后进行下一轮 ---\n")
    print("daemon 已退出。")
    return 0 if last_ok else 1


def run_tasks(args: argparse.Namespace) -> int:
    db = KeyDatabase(config.DB_PATH)
    rows = db.recent_tasks(args.limit)
    if not rows:
        print("暂无扫描任务记录（首次运行 scan 或 daemon 后可见）。")
        return 0
    print(f"最近 {len(rows)} 轮扫描任务：")
    for r in rows:
        scope = f"{r['window_from'] or '全量'}~{r['window_to']}" if r["window_from"] else "全量"
        print(f"  #{r['id']} [{r['status']}] {r['started_at']}"
              f" 触发={r['trigger']} 模式={r['mode']} 窗口={scope}"
              f" 命中={r['hits']} 跳过={r['files_skipped']} 分析={r['files_analyzed']}"
              f" 新增={r['keys_new']} 更新={r['keys_updated']} 耗时={r['duration']}s"
              + (f" 错误={r['error'][:60]}" if r["error"] else ""))
    return 0


def run_status(args: argparse.Namespace) -> int:  # noqa: ARG001
    db = KeyDatabase(config.DB_PATH)
    cursor = db.get_cursor()
    print("== 扫描状态 ==")
    print(f"   上次成功扫描 : {cursor.get('last_scan_at') or '从未'}")
    print(f"   增量游标     : {cursor.get('last_window_to') or '无（下轮将全量）'}")
    print(f"   扫描间隔     : {config.SCAN_INTERVAL} 秒（.env SCAN_INTERVAL 可调）")
    print(f"   增量模式     : {'开' if config.SCAN_INCREMENTAL else '关'}")
    print(f"   密钥总数     : {db.count()}")
    print("   最近任务     :")
    for r in db.recent_tasks(3):
        print(f"     #{r['id']} [{r['status']}] {r['started_at']}"
              f" 新增={r['keys_new']} 更新={r['keys_updated']}")
    return 0


def run_health(args: argparse.Namespace) -> int:
    """输出机器可读/人类可读健康报告，并按状态返回退出码。"""
    db = KeyDatabase(config.DB_PATH)
    recovered = db.recover_stale_tasks(config.STALE_TASK_SECONDS) \
        if args.recover_stale else 0
    report = collect_health(db, online=args.online)
    report["recovered_stale_tasks"] = recovered

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        labels = {"ok": "通过", "warning": "警告", "error": "失败"}
        print(f"== 健康检查：{report['status']} ==")
        for item in report["checks"]:
            print(f"  [{labels[item['status']]}] {item['name']}: {item['message']}")
        if recovered:
            print(f"  [恢复] 已结算 {recovered} 个陈旧 running 任务")

    return {"healthy": 0, "degraded": 1, "unhealthy": 2}[report["status"]]


def run_export(args: argparse.Namespace) -> int:
    db = KeyDatabase(config.DB_PATH)
    out = BASE_DIR / "data" / "keys.csv"
    out.write_text(db.export_csv(), encoding="utf-8")
    print(f"已导出 {db.count()} 条记录 → {out}")
    return 0


def run_stats(args: argparse.Namespace) -> int:
    db = KeyDatabase(config.DB_PATH)
    s = db.stats()
    print(f"密钥总数        : {s['total']}")
    print(f"今日新增        : {s['today']}")
    print(f"高置信度(≥0.8)  : {s['high_conf']}")
    print(f"服务商数量      : {s['provider_count']}")
    print(f"分类分布        : " + ", ".join(f"{k}={v}" for k, v in s.get("category", {}).items()) or "无")
    print("服务商分布:")
    for p in s["providers"]:
        print(f"  - {p['name']}: {p['count']}")
    return 0


# ----------------------------------------------------------------------
# 参数与入口
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="github-key-scanner",
        description="GitHub 公开代码 sk-* 密钥泄露监测：搜索 → AI 分析 → 入库 → Web 展示")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="扫描 GitHub 并分析入库（单轮）")
    p_scan.add_argument("--demo", action="store_true",
                        help="演示模式：使用内置示例数据，无需 GitHub 令牌")
    p_scan.add_argument("--ai-mode", choices=["auto", "ai", "rules"], default="auto",
                        help="分析模式：auto=有 AI 配置用 AI，ai=强制 AI，rules=仅规则引擎")
    p_scan.add_argument("--max-pages", type=int, default=None,
                        help="最大抓取页数（覆盖 .env 的 GITHUB_MAX_PAGES；界定单轮范围）")
    p_scan.add_argument("--extensions", nargs="+", default=None,
                        help="限定扩展名（默认 py json js ts toml yaml yml ini conf cfg env），如 py json js ts toml yaml")
    p_scan.add_argument("--no-fetch-content", action="store_true",
                        help="不拉取文件全文，仅用搜索片段分析")
    p_scan.add_argument("--incremental", action="store_true", default=None,
                        help="增量扫描：仅处理上次成功扫描后新索引的文件（默认开）")
    p_scan.add_argument("--no-incremental", dest="incremental", action="store_false",
                        help="关闭增量：本轮全量搜索（仍受页数上限与文件去重约束）")
    p_scan.add_argument("--full", action="store_true",
                        help="强制全量重扫：忽略文件指纹去重（密钥级去重仍生效）")
    p_scan.add_argument("--since", default="",
                        help="指定窗口起点 YYYY-MM-DD（覆盖游标，常用于手动补扫）")
    p_scan.set_defaults(func=run_scan)

    p_daemon = sub.add_parser("daemon", help="全自动托管：按间隔循环扫描（Ctrl+C 退出）")
    p_daemon.add_argument("--interval", type=int, default=None,
                          help="扫描间隔秒数（默认 .env SCAN_INTERVAL=7200 即 2 小时）")
    p_daemon.add_argument("--once", action="store_true",
                          help="只执行一轮后退出（便于测试）")
    p_daemon.add_argument("--demo", action="store_true",
                          help="演示模式轮询（无需 GitHub 令牌）")
    p_daemon.add_argument("--ai-mode", choices=["auto", "ai", "rules"], default="auto")
    p_daemon.add_argument("--max-pages", type=int, default=None)
    p_daemon.add_argument("--extensions", nargs="+", default=None)
    p_daemon.add_argument("--no-fetch-content", action="store_true")
    p_daemon.add_argument("--incremental", action="store_true", default=None)
    p_daemon.add_argument("--no-incremental", dest="incremental", action="store_false")
    p_daemon.add_argument("--full", action="store_true")
    p_daemon.add_argument("--since", default="")
    p_daemon.set_defaults(func=run_daemon)

    sub.add_parser("export", help="导出 CSV").set_defaults(func=run_export)
    sub.add_parser("stats", help="打印统计").set_defaults(func=run_stats)
    p_tasks = sub.add_parser("tasks", help="查看扫描任务历史")
    p_tasks.add_argument("--limit", type=int, default=10, help="显示条数")
    p_tasks.set_defaults(func=run_tasks)
    sub.add_parser("status", help="查看游标与调度状态").set_defaults(func=run_status)

    args = parser.parse_args(argv)
    if args.command in ("scan", "daemon") and args.no_fetch_content:
        config.FETCH_FILE_CONTENT = False
    # 默认增量开关：未显式指定时取 .env SCAN_INCREMENTAL
    if getattr(args, "incremental", None) is None:
        args.incremental = config.SCAN_INCREMENTAL
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
