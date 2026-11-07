"""Flask Web 应用：页面、API、导出、权限与审计。"""
from __future__ import annotations

import json
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for

from . import aggregate, attachments, config, export, pipeline, tianyancha
from .db import audit, db, now
from .collectors.base import PAUSE_EVENT, STOP_EVENT
from .collectors.registry import SOURCES

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = config.APP_SECRET
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024

COLLECT_STATE = {"running": False, "task_id": None, "last": None, "error": None}
STATE_LOCK = threading.Lock()


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "未登录"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def role_required(*roles):
    def deco(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            if session.get("role") not in roles:
                return jsonify({"ok": False, "error": "权限不足"}), 403
            return view(*args, **kwargs)

        return wrapper

    return deco


@app.get("/login")
def login():
    error = ""
    if request.args.get("error"):
        error = "用户名或密码不正确"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>登录 · 湖南HIS中标情报</title>
<style>body{{font-family:"Microsoft YaHei",sans-serif;background:#0f2e2a;display:flex;align-items:center;
justify-content:center;height:100vh;margin:0;color:#e8f3ef}}
.box{{background:#fff;color:#17322c;padding:36px 42px;border-radius:14px;width:320px;box-shadow:0 18px 50px rgba(0,0,0,.3)}}
h1{{font-size:18px;margin:0 0 18px}}input{{width:100%;padding:10px;margin:6px 0 14px;border:1px solid #cfe0da;
border-radius:8px;box-sizing:border-box}}button{{width:100%;padding:11px;background:#1f6f5c;color:#fff;border:0;
border-radius:8px;font-size:15px;cursor:pointer}}.err{{color:#b42318;font-size:13px}}.tip{{color:#6b8a80;font-size:12px;margin-top:12px}}</style>
</head><body><form class="box" method="post" action="/login">
<h1>湖南公立医院 HIS 中标情报</h1>{f'<p class="err">{error}</p>' if error else ''}
<label>用户名</label><input name="username" value="admin" autocomplete="username">
<label>密码</label><input name="password" type="password" autocomplete="current-password">
<button type="submit">登录</button><p class="tip">默认账号 admin / admin123，可在 .env 修改</p></form></body></html>"""


@app.post("/login")
def do_login():
    from werkzeug.security import check_password_hash

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return redirect(url_for("login", error=1))
    session["user"] = username
    session["role"] = row["role"]
    with db() as conn:
        audit(conn, username, "login", "", request.remote_addr or "")
    return redirect(request.args.get("next") or url_for("index"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def index():
    return render_template("index.html", user=session.get("user"), role=session.get("role"),
                           cities=aggregate.city_summary(), sources=SOURCES)


def _filtered_records() -> list[dict]:
    city = request.args.get("city") or None
    brand = request.args.get("brand") or None
    year = request.args.get("year") or None
    q = (request.args.get("q") or "").strip()
    rows = aggregate.latest_two(city_code=city, brand=brand, year=year)
    if q:
        rows = [r for r in rows if q in r["hospital_name"] or q in r["title"]]
    return rows


@app.get("/api/records")
@login_required
def api_records():
    rows = _filtered_records()
    return jsonify({"ok": True, "total": len(rows), "rows": rows})


@app.get("/api/summary")
@login_required
def api_summary():
    return jsonify(
        {
            "ok": True,
            "cities": aggregate.city_summary(),
            "quality": aggregate.quality_stats(),
            "brands": aggregate.brand_share(),
            "health": aggregate.source_health(),
            "collect": COLLECT_STATE,
        }
    )


@app.get("/api/hospitals")
@login_required
def api_hospitals():
    return jsonify({"ok": True, "rows": aggregate.hospitals_list(
        city_code=request.args.get("city") or None, q=request.args.get("q") or None)})


@app.get("/api/hospital/<int:hospital_id>")
@login_required
def api_hospital(hospital_id: int):
    with db() as conn:
        h = conn.execute("SELECT * FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
    if not h:
        return jsonify({"ok": False, "error": "医院不存在"}), 404
    return jsonify({"ok": True, "hospital": dict(h), "timeline": aggregate.hospital_timeline(hospital_id)})


@app.get("/api/quality")
@login_required
def api_quality():
    return jsonify({"ok": True, **aggregate.quality_stats()})


@app.get("/api/tasks")
@login_required
def api_tasks():
    return jsonify({"ok": True, "rows": aggregate.tasks()})


@app.get("/api/tasks/<int:task_id>/logs")
@login_required
def api_task_logs(task_id: int):
    return jsonify({"ok": True, "rows": aggregate.task_logs(task_id)})


@app.get("/api/health")
@login_required
def api_health():
    return jsonify({"ok": True, "rows": aggregate.source_health(), "sources": SOURCES})


@app.post("/api/health/refresh")
@login_required
def api_health_refresh():
    rows = pipeline.refresh_health()
    return jsonify({"ok": True, "rows": rows})


def _run_collect(sources, keywords, since, max_pages, actor):
    try:
        stats = pipeline.run_collection(sources=sources, keywords=keywords, since=since,
                                        max_pages=max_pages, actor=actor)
        with STATE_LOCK:
            COLLECT_STATE.update({"running": False, "last": stats, "task_id": stats.get("task_id")})
    except Exception as exc:  # noqa: BLE001
        with STATE_LOCK:
            COLLECT_STATE.update({"running": False, "error": str(exc)})
    finally:
        STOP_EVENT.clear()
        PAUSE_EVENT.clear()


@app.post("/api/collect")
@role_required("admin", "analyst")
def api_collect():
    with STATE_LOCK:
        if COLLECT_STATE["running"]:
            return jsonify({"ok": False, "error": "已有采集任务在运行"}), 409
        payload = request.get_json(silent=True) or {}
        sources = payload.get("sources") or ["hunan_gov_search", "huaihua_ggzy"]
        keywords = payload.get("keywords") or None
        since = payload.get("since") or config.COLLECT_SINCE
        max_pages = int(payload.get("max_pages") or config.COLLECT_MAX_PAGES)
        COLLECT_STATE.update({"running": True, "task_id": None, "last": None, "error": None})
    actor = session.get("user", "system")
    threading.Thread(target=_run_collect, args=(sources, keywords, since, max_pages, actor), daemon=True).start()
    return jsonify({"ok": True, "message": "采集任务已启动"})


@app.post("/api/collect/pause")
@role_required("admin", "analyst")
def api_pause():
    PAUSE_EVENT.set()
    return jsonify({"ok": True, "message": "已暂停，当前请求结束后停止"})


@app.post("/api/collect/resume")
@role_required("admin", "analyst")
def api_resume():
    PAUSE_EVENT.clear()
    return jsonify({"ok": True, "message": "已恢复"})


@app.post("/api/collect/stop")
@role_required("admin", "analyst")
def api_stop():
    STOP_EVENT.set()
    PAUSE_EVENT.clear()
    return jsonify({"ok": True, "message": "正在停止"})


@app.get("/api/collect/status")
@login_required
def api_collect_status():
    return jsonify({"ok": True, **COLLECT_STATE})


@app.post("/api/export")
@role_required("admin", "analyst", "viewer")
def api_export():
    path = export.export_excel()
    with db() as conn:
        audit(conn, session.get("user", ""), "export", path, request.remote_addr or "")
    return jsonify({"ok": True, "path": path, "download": url_for("download", filename=Path(path).name)})


@app.get("/download/<path:filename>")
@login_required
def download(filename: str):
    target = config.EXPORT_DIR / filename
    if not target.exists():
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    return send_file(target, as_attachment=True)


@app.get("/attachment/<int:att_id>")
@login_required
def attachment(att_id: int):
    with db() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "附件不存在"}), 404
    force_download = request.args.get("download") == "1"
    if row["local_path"]:
        local = config.BASE_DIR / row["local_path"]
        if local.exists():
            inline = (not force_download) and local.suffix.lower() in (".pdf", ".txt", ".jpg", ".jpeg", ".png")
            return send_file(local, as_attachment=not inline, download_name=row["name"] or local.name)
    return redirect(row["url"])


@app.get("/api/announcement/<int:ann_id>/attachments")
@login_required
def api_announcement_attachments(ann_id: int):
    return jsonify({"ok": True, "rows": attachments.list_for_announcement(ann_id)})


@app.post("/api/attachments/upload")
@role_required("admin", "analyst")
def api_attachment_upload():
    ann_id = request.form.get("announcement_id", type=int)
    kind = request.form.get("kind", "其他")
    file = request.files.get("file")
    if not ann_id or not file:
        return jsonify({"ok": False, "error": "缺少 announcement_id 或文件"}), 400
    try:
        row = attachments.save_upload(ann_id, file, kind, session.get("user", ""), request.remote_addr or "")
        return jsonify({"ok": True, "row": row})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"上传失败：{exc}"}), 500


@app.post("/api/attachments/<int:att_id>/check")
@role_required("admin", "analyst")
def api_attachment_check(att_id: int):
    return jsonify(attachments.check_one(att_id, actor=session.get("user", "system")))


@app.post("/api/attachments/check")
@role_required("admin", "analyst")
def api_attachments_check_all():
    payload = request.get_json(silent=True) or {}
    result = attachments.recheck_all(limit=payload.get("limit"), actor=session.get("user", "system"))
    return jsonify({"ok": True, **result})



@app.get("/api/tianyancha")
@login_required
def api_tianyancha():
    name = request.args.get("name", "").strip()
    if not tianyancha.is_enabled():
        return jsonify({"ok": False, "disabled": True,
                        "message": "天眼查模块未启用：需在 .env 配置官方开放平台 TYC_API_KEY。"})
    if not name:
        return jsonify({"ok": False, "error": "缺少 name 参数"})
    try:
        with db() as conn:
            audit(conn, session.get("user", ""), "tianyancha_query", name, request.remote_addr or "")
        return jsonify({"ok": True, "data": tianyancha.search_company(name)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/audit")
@role_required("admin")
def api_audit():
    with db() as conn:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify({"ok": True, "rows": [dict(r) for r in rows]})


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "接口不存在"}), 404
    return redirect(url_for("index"))


def _attachment_check_loop():
    """定时复检远端附件链接；本地已留存的直接判有效。"""
    while True:
        time.sleep(max(1, config.ATTACHMENT_CHECK_HOURS) * 3600)
        try:
            attachments.recheck_all(actor="scheduler")
        except Exception as exc:  # noqa: BLE001
            print(f"附件复检失败: {exc}")


def run():
    from waitress import serve

    if config.ATTACHMENT_CHECK_HOURS > 0:
        threading.Thread(target=_attachment_check_loop, daemon=True).start()
    print(f"服务启动: http://{config.APP_HOST}:{config.APP_PORT}")
    serve(app, host=config.APP_HOST, port=config.APP_PORT, threads=6)
