# -*- coding: utf-8 -*-
"""
Web 展示服务：仪表盘 + 密钥表格 + 筛选 + 详情 + 状态管理 + CSV 导出 + 扫描任务面板。

启动：python web/app.py  →  http://127.0.0.1:8765
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, render_template, request, Response

from config import config
from gks.privacy import MonitoringDatabase as KeyDatabase
from gks.scan_scope import scope_snapshot, save_settings

app = Flask(__name__)
db = KeyDatabase(config.DB_PATH)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/stats")
def api_stats():
    return jsonify(db.stats())


@app.get("/api/tasks")
def api_tasks():
    """最近扫描任务历史。"""
    limit = min(max(int(request.args.get("limit", 10)), 1), 200)
    return jsonify({"items": db.recent_tasks(limit)})


@app.get("/api/status")
def api_status():
    """调度状态：增量游标 + 上次任务 + 托管配置。"""
    cursor = db.get_cursor()
    last = db.recent_tasks(1)
    try:
        scan_scope = scope_snapshot(config.SCAN_SETTINGS_FILE)
    except ValueError as error:
        scan_scope = {"error": str(error)}
    return jsonify({
        "cursor": cursor,
        "last_task": last[0] if last else None,
        "interval": config.SCAN_INTERVAL,
        "incremental": config.SCAN_INCREMENTAL,
        "monitoring_mode": "redacted",
        "validity_test_enabled": False,
        "scan_scope": scan_scope,
        "trigger_file": str(config.TRIGGER_FILE),
        "now": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.get("/api/scan/settings")
def api_scan_settings():
    try:
        return jsonify(scope_snapshot(config.SCAN_SETTINGS_FILE))
    except ValueError as error:
        return jsonify({"error": str(error)}), 500


@app.put("/api/scan/settings")
def api_save_scan_settings():
    try:
        save_settings(config.SCAN_SETTINGS_FILE, request.get_json(silent=True))
        return jsonify({"ok": True, **scope_snapshot(config.SCAN_SETTINGS_FILE),
                        "message": "已保存，从下一轮扫描生效"})
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except OSError:
        return jsonify({"error": "时间范围保存失败，请重试"}), 500


@app.post("/api/scan/trigger")
def api_scan_trigger():
    """写入触发文件，daemon 下一轮醒来即执行。无 daemon 时返回提示。"""
    try:
        config.TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.TRIGGER_FILE.write_text("1", encoding="utf-8")
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True,
                    "message": "已请求立即扫描：若 daemon 托管正在运行，下一轮醒来即执行。"})


@app.get("/api/keys")
def api_keys():
    page = max(int(request.args.get("page", 1)), 1)
    size = min(max(int(request.args.get("size", 20)), 1), 100)
    try:
        min_conf = min(max(float(request.args.get("min_conf", 0)), 0.0), 1.0)
    except ValueError:
        min_conf = 0.0
    rows, total = db.query(
        q=request.args.get("q", "").strip(),
        provider=request.args.get("provider", "").strip(),
        status=request.args.get("status", "").strip(),
        category=request.args.get("category", "").strip(),
        min_conf=min_conf,
        page=page, size=size,
    )
    return jsonify({"items": rows, "total": total, "page": page, "size": size})


@app.get("/api/keys/<int:key_id>")
def api_key_detail(key_id: int):
    row = db.get(key_id)
    if not row:
        return jsonify({"error": "未找到"}), 404
    return jsonify(row)


@app.patch("/api/keys/<int:key_id>/status")
def api_update_status(key_id: int):
    data = request.get_json(silent=True) or {}
    status = data.get("status", "")
    if db.update_status(key_id, status):
        return jsonify({"ok": True})
    return jsonify({"error": "非法状态值"}), 400


@app.post("/api/keys/<int:key_id>/test")
def api_key_test(key_id: int):
    """Public monitoring never authenticates to providers with discovered keys."""
    return jsonify({"error": "脱敏监测不保存完整密钥，已关闭有效性测试"}), 403


@app.get("/api/export.csv")
def api_export():
    try:
        min_conf = min(max(float(request.args.get("min_conf", 0)), 0.0), 1.0)
    except ValueError:
        min_conf = 0.0
    csv_text = db.export_csv(
        q=request.args.get("q", "").strip(),
        provider=request.args.get("provider", "").strip(),
        status=request.args.get("status", "").strip(),
        category=request.args.get("category", "").strip(),
        min_conf=min_conf,
    )
    return Response(csv_text, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=keys.csv"})


if __name__ == "__main__":
    print(f"Web 服务已启动：http://{config.WEB_HOST}:{config.WEB_PORT}")
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False, threaded=True)
