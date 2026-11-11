"""Flask Web 应用：页面、API、导出、权限与审计。"""
from __future__ import annotations

import io
import json
import re
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for

from . import aggregate, attachments, brands_ops, config, export, hospital_master, importer, pipeline, tianyancha, version
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
<link rel="icon" type="image/png" href="/static/favicon.png">
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
                           cities=aggregate.city_summary(), sources=SOURCES, app_version=version.__version__)


def _build_filters(data) -> dict:
    """把 GET 参数或 JSON 体归一为筛选条件；金额入参单位为万元，内部换算为元。"""
    def get(key: str) -> str:
        value = data.get(key) if hasattr(data, "get") else None
        return str(value).strip() if value is not None else ""

    filters: dict = {}
    if get("city"):
        filters["city"] = get("city")
    if get("hospital_id"):
        try:
            filters["hospital_id"] = int(get("hospital_id"))
        except ValueError:
            pass
    if get("brand"):
        filters["brand"] = get("brand")
    if get("year"):
        filters["year"] = get("year")
    for key in ("date_from", "date_to"):
        value = get(key)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            filters[key] = value
    for key in ("amount_min", "amount_max"):
        value = get(key)
        if value:
            try:
                filters[key] = float(value) * 10000  # 万元 -> 元
            except ValueError:
                pass
    if get("q"):
        filters["q"] = get("q")
    if get("has_note") in ("0", "1"):
        filters["has_note"] = get("has_note")
    if get("tag"):
        filters["tag"] = get("tag")
    if get("status"):
        filters["status"] = get("status")
    return filters


@app.get("/api/records")
@login_required
def api_records():
    filters = _build_filters(request.args)
    rows = aggregate.latest_two(filters)
    return jsonify({"ok": True, "total": len(rows), "rows": rows, "filters": filters})


@app.get("/api/filters")
@login_required
def api_filters():
    return jsonify({"ok": True, **aggregate.filter_options()})


@app.post("/api/records/<int:ann_id>/note")
@role_required("admin", "analyst")
def api_note_save(ann_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        note = aggregate.upsert_note(
            ann_id, payload.get("note", ""), payload.get("tags", ""), payload.get("status", ""),
            session.get("user", "system"),
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    with db() as conn:
        audit(conn, session.get("user", ""), "note_update", f"ann={ann_id} status={payload.get('status','')}",
              request.remote_addr or "")
    return jsonify({"ok": True, "note": note})


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


@app.get("/api/query-trace")
@login_required
def api_query_trace():
    limit = request.args.get("limit") or 200
    return jsonify({"ok": True, "rows": aggregate.query_trace(limit=int(limit))})


@app.get("/api/low-relevance")
@login_required
def api_low_relevance():
    limit = request.args.get("limit") or 200
    return jsonify({"ok": True, "rows": aggregate.low_relevance_list(limit=int(limit))})


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


@app.get("/api/version")
@login_required
def api_version():
    return jsonify({"ok": True, **version.update_info()})


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
        sources = payload.get("sources") or ["hunan_ccgp", "hunan_gov_search", "huaihua_ggzy"]
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
    filters = _build_filters(request.get_json(silent=True) or {})
    path = export.export_excel(filters=filters)
    with db() as conn:
        audit(conn, session.get("user", ""), "export",
              json.dumps({"path": path, "filters": filters}, ensure_ascii=False), request.remote_addr or "")
    return jsonify({"ok": True, "path": path, "download": url_for("download", filename=Path(path).name)})


@app.post("/api/import")
@role_required("admin", "analyst")
def api_import():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "请选择 CSV 或 XLSX 文件"}), 400
    try:
        result = importer.import_records(
            file.stream, file.filename, actor=session.get("user", "system"), ip=request.remote_addr or ""
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"导入失败：{exc}"}), 500
    return jsonify({"ok": True, **result})


@app.get("/api/import/template")
@login_required
def api_import_template():
    data = importer.template_csv()
    return send_file(
        io.BytesIO(data), mimetype="text/csv; charset=utf-8", as_attachment=True,
        download_name="中标数据导入模板.csv",
    )


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



@app.get("/api/notifications")
@login_required
def api_notifications():
    limit = request.args.get("limit", type=int) or 50
    rows = aggregate.notifications(limit)
    unread = sum(1 for r in rows if not r["is_read"])
    return jsonify({"ok": True, "rows": rows, "unread": unread})


@app.post("/api/notifications/read")
@login_required
def api_notifications_read():
    payload = request.get_json(silent=True) or {}
    count = aggregate.mark_notifications_read(payload.get("ids"))
    return jsonify({"ok": True, "updated": count})


@app.get("/api/review/queue")
@login_required
def api_review_queue():
    limit = request.args.get("limit", type=int) or 200
    return jsonify({"ok": True, "rows": aggregate.review_queue(limit),
                    "stats": brands_ops.unknown_ratio()})


@app.post("/api/review/<int:award_id>/confirm")
@role_required("admin", "analyst")
def api_review_confirm(award_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        result = brands_ops.confirm_award(
            award_id, payload.get("brand", ""), session.get("user", "system"), request.remote_addr or ""
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(result)


@app.post("/api/review/<int:award_id>/pending")
@role_required("admin", "analyst")
def api_review_pending(award_id: int):
    return jsonify(brands_ops.mark_pending(award_id, session.get("user", "system"), request.remote_addr or ""))


@app.post("/api/review/recompute")
@role_required("admin", "analyst")
def api_review_recompute():
    payload = request.get_json(silent=True) or {}
    result = brands_ops.recompute_awards(payload.get("announcement_id"),
                                         session.get("user", "system"))
    return jsonify({"ok": True, **result, "stats": brands_ops.unknown_ratio()})


@app.get("/api/rules")
@login_required
def api_rules():
    return jsonify({"ok": True, "rows": brands_ops.rules_list()})


@app.post("/api/rules")
@role_required("admin", "analyst")
def api_rule_add():
    payload = request.get_json(silent=True) or {}
    try:
        row = brands_ops.add_rule(
            payload.get("pattern", ""), payload.get("brand_name", ""),
            payload.get("match_type") or "contains", payload.get("priority") or 1,
            payload.get("note", ""), session.get("user", "system"), request.remote_addr or "",
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "row": row})


@app.put("/api/rules/<int:rule_id>")
@role_required("admin", "analyst")
def api_rule_update(rule_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        row = brands_ops.update_rule(rule_id, **payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "row": row})


@app.delete("/api/rules/<int:rule_id>")
@role_required("admin", "analyst")
def api_rule_delete(rule_id: int):
    return jsonify(brands_ops.delete_rule(rule_id, session.get("user", "system"), request.remote_addr or ""))


@app.get("/api/hospitals/unmatched")
@login_required
def api_hospitals_unmatched():
    return jsonify({"ok": True, "rows": aggregate.unmatched_hospitals(),
                    "suggestions": hospital_master.suggestions()})


@app.get("/api/hospitals/search")
@login_required
def api_hospitals_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": True, "rows": []})
    return jsonify({"ok": True, "rows": aggregate.hospitals_list(q=q)})


@app.post("/api/hospitals/merge")
@role_required("admin", "analyst")
def api_hospital_merge():
    payload = request.get_json(silent=True) or {}
    try:
        result = hospital_master.merge(
            int(payload.get("source_id") or 0), int(payload.get("target_id") or 0),
            session.get("user", "system"), request.remote_addr or "",
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(result)


@app.get("/api/hospitals/master/template")
@login_required
def api_hospital_master_template():
    return send_file(
        io.BytesIO(hospital_master.template_csv()), mimetype="text/csv; charset=utf-8",
        as_attachment=True, download_name="医院主数据导入模板.csv",
    )


@app.post("/api/hospitals/master/import")
@role_required("admin", "analyst")
def api_hospital_master_import():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "请选择 CSV 或 XLSX 文件"}), 400
    try:
        result = hospital_master.import_master(
            file.stream, file.filename, session.get("user", "system"), request.remote_addr or ""
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"导入失败：{exc}"}), 500
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
