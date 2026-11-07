"""附件服务：手工上传、失效复检、列表。"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import config
from .collectors.base import Fetcher
from .db import audit, db, now
from .normalize import clean_text, classify_attachment

ALLOWED_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".ofd", ".txt", ".jpg", ".jpeg", ".png"}
KINDS = ("中标公告", "招标文件", "参数文件", "其他")


def _safe_name(name: str) -> str:
    name = clean_text(name or "").replace("\\", "_").replace("/", "_")
    return name[:120] or "附件"


def save_upload(announcement_id: int, file_storage, kind: str, username: str, ip: str = "") -> dict:
    """保存手工上传的参数/招标文件，落库并写审计。"""
    if kind not in KINDS:
        kind = "其他"
    filename = _safe_name(file_storage.filename or "")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"不支持的附件类型：{ext or '无扩展名'}")
    with db() as conn:
        ann = conn.execute("SELECT id, title, url FROM announcements WHERE id=?", (announcement_id,)).fetchone()
        if not ann:
            raise ValueError("公告不存在")
        dest_dir = config.ATTACHMENT_DIR / str(announcement_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"manual_{uuid.uuid4().hex[:12]}{ext}"
        file_storage.save(dest)
        raw = dest.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        size = len(raw)
        try:
            rel_path = str(dest.relative_to(config.BASE_DIR))
        except ValueError:
            rel_path = str(dest)
        cur = conn.execute(
            "INSERT INTO attachments(announcement_id, name, url, local_path, sha256, size, status, "
            "kind, source, uploader, uploaded_at, is_valid, checked_at, last_checked_at, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                announcement_id, filename, ann["url"], rel_path,
                sha, size, "ok", kind, "manual", username, now(), 1, now(), now(),
                f"手工上传 {username}",
            ),
        )
        att_id = cur.lastrowid
        audit(conn, username, "attachment_upload", f"ann={announcement_id} id={att_id} {filename}", ip)
        return dict(conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone())


def list_for_announcement(announcement_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM attachments WHERE announcement_id=? ORDER BY "
            "CASE kind WHEN '中标公告' THEN 3 WHEN '参数文件' THEN 0 WHEN '招标文件' THEN 1 ELSE 2 END, id",
            (announcement_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def check_one(att_id: int, fetcher: Fetcher | None = None, actor: str = "system") -> dict:
    """复检单个附件链接：本地文件存在即有效；否则请求远端并标记。"""
    with db() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "附件不存在"}
    if row["local_path"] and (config.BASE_DIR / row["local_path"]).exists():
        with db() as conn:
            conn.execute(
                "UPDATE attachments SET is_valid=1, status='ok', last_checked_at=? WHERE id=?",
                (now(), att_id),
            )
        return {"ok": True, "id": att_id, "valid": True, "note": "本地文件有效"}

    fetcher = fetcher or Fetcher()
    valid, code, note = False, None, ""
    try:
        resp = fetcher.get(row["url"], timeout=10, retries=0, stream=True)
        if resp is not None:
            code = resp.status_code
            valid = code == 200
            note = f"HTTP {code}"
        else:
            note = "无响应"
    except Exception as exc:  # noqa: BLE001
        note = str(exc)[:120]
    with db() as conn:
        conn.execute(
            "UPDATE attachments SET is_valid=?, status=?, last_checked_at=?, checked_at=?, note=? WHERE id=?",
            (1 if valid else 0, "ok" if valid else "invalid", now(), now(), note, att_id),
        )
        audit(conn, actor, "attachment_check", f"id={att_id} valid={valid} {note}")
    return {"ok": True, "id": att_id, "valid": valid, "http_code": code, "note": note}


def recheck_all(limit: int | None = None, actor: str = "system") -> dict:
    """批量复检远端附件（不含公告原文页面与已有本地文件的记录）。"""
    with db() as conn:
        sql = "SELECT id FROM attachments WHERE source IN ('direct','manual') AND local_path IS NULL"
        if limit:
            sql += f" LIMIT {int(limit)}"
        ids = [r["id"] for r in conn.execute(sql).fetchall()]
    fetcher = Fetcher()
    valid = invalid = 0
    for att_id in ids:
        result = check_one(att_id, fetcher, actor)
        if result.get("valid"):
            valid += 1
        else:
            invalid += 1
    return {"checked": len(ids), "valid": valid, "invalid": invalid}


def stats_by_kind() -> dict:
    with db() as conn:
        rows = conn.execute(
            "SELECT kind, COUNT(*) c, SUM(CASE WHEN is_valid=1 THEN 1 ELSE 0 END) valid FROM attachments GROUP BY kind"
        ).fetchall()
    return {r["kind"]: {"count": r["c"], "valid": r["valid"] or 0} for r in rows}


def parse_kind_from_form(value: str) -> str:
    return value if value in KINDS else classify_attachment(value)
