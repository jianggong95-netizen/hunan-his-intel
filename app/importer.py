"""手工导入中标数据（CSV/XLSX）：按公告链接合并，保留已有备注。"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from openpyxl import load_workbook

from . import aggregate
from .db import audit, db, now
from .normalize import extract_purchaser, normalize_company, normalize_name, parse_date
from . import brands_ops
from .pipeline import content_hash, upsert_hospital
from .regions import CITIES, CITY_BY_SHORT, detect_city

TEMPLATE_HEADERS = [
    "公告链接", "项目名称", "医院", "市州", "中标日期", "中标单位", "HIS厂家", "中标金额(元)",
    "备注", "标签", "处理状态",
]

_ALIASES = {
    "url": ("url", "公告链接", "公告原文", "原文链接", "链接"),
    "title": ("title", "项目名称", "招标项目名称", "公告标题", "标题"),
    "hospital": ("hospital", "医院", "医院名称", "采购单位", "采购人"),
    "city": ("city", "市州", "地区", "所属市州", "城市"),
    "publish_date": ("publish_date", "中标日期", "公告日期", "发布日期", "日期"),
    "supplier": ("supplier", "中标单位", "中标供应商", "成交供应商", "中标人"),
    "brand": ("brand", "his厂家", "his厂家品牌", "厂家", "品牌"),
    "amount": ("amount", "中标金额", "中标金额(元)", "金额", "成交金额"),
    "note": ("note", "备注", "文字备注"),
    "tags": ("tags", "标签"),
    "status": ("status", "处理状态", "跟进状态"),
}

_HEADER_STRIP = re.compile(r"[\s（）()：:]+")


def _norm_header(value) -> str:
    return _HEADER_STRIP.sub("", str(value or "").replace("\ufeff", "")).lower()


_HEADER_MAP = {
    _norm_header(alias): field for field, aliases in _ALIASES.items() for alias in aliases
}


def _read_csv(raw: bytes) -> list[dict]:
    for encoding in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSV 编码无法识别，请使用 UTF-8 或 GBK")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    headers = [_norm_header(h) for h in rows[0]]
    out = []
    for cells in rows[1:]:
        if not any(str(c).strip() for c in cells):
            continue
        record = {}
        for idx, header in enumerate(headers):
            field = _HEADER_MAP.get(header)
            if field:
                record[field] = cells[idx].strip() if idx < len(cells) else ""
        out.append(record)
    return out


def _read_xlsx(raw: bytes) -> list[dict]:
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [_norm_header(h) for h in rows[0]]
    out = []
    for cells in rows[1:]:
        if not any(str(c).strip() for c in cells if c is not None):
            continue
        record = {}
        for idx, header in enumerate(headers):
            field = _HEADER_MAP.get(header)
            if field:
                value = cells[idx] if idx < len(cells) else ""
                record[field] = str(value).strip() if value is not None else ""
        out.append(record)
    return out


def _parse_amount(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = str(value).strip().replace(",", "").replace("，", "")
    if not text:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(亿元|万元|万|元)?", text)
    if not m:
        return None
    number = float(m.group(1))
    unit = m.group(2) or ""
    if unit == "亿元":
        number *= 100000000
    elif unit in ("万元", "万"):
        number *= 10000
    return number if number > 0 else None


def _city_code(value: str, fallback_text: str = "") -> str | None:
    text = (value or "").strip()
    if text:
        for city in CITIES:
            if text in (city["short"], city["name"]) or text.replace("市", "") == city["short"]:
                return city["code"]
    short = detect_city(text, fallback_text)
    return CITY_BY_SHORT[short]["code"] if short in CITY_BY_SHORT else None


def _city_short(code: str | None) -> str | None:
    return next((c["short"] for c in CITIES if c["code"] == code), None)


def _note_upsert(conn, ann_id: int, url: str, note: str, tags, status: str, actor: str) -> bool:
    note = (note or "").strip()[:2000]
    tags_norm = aggregate.normalize_tags(tags)
    status = (status or "").strip()[:40]
    if not (note or tags_norm or status):
        return False
    old = conn.execute("SELECT * FROM record_notes WHERE ann_url=?", (url,)).fetchone()
    conn.execute(
        "INSERT INTO record_notes(ann_url, announcement_id, note, tags, status, created_by, created_at, "
        "updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ann_url) DO UPDATE SET announcement_id=excluded.announcement_id, note=excluded.note, "
        "tags=excluded.tags, status=excluded.status, updated_by=excluded.updated_by, "
        "updated_at=excluded.updated_at",
        (url, ann_id, note, tags_norm, status, actor, now(), actor, now()),
    )
    new = conn.execute("SELECT * FROM record_notes WHERE ann_url=?", (url,)).fetchone()
    conn.execute(
        "INSERT INTO change_history(entity, entity_id, field, old_value, new_value, changed_at, actor) "
        "VALUES (?,?,?,?,?,?,?)",
        ("note", ann_id, "import", json.dumps(dict(old), ensure_ascii=False) if old else "",
         json.dumps(dict(new), ensure_ascii=False), now(), actor),
    )
    return True


def _ensure_award(conn, ann_id: int, supplier: str, brand: str, amount: float | None,
                  award_date: str, basis: str) -> str:
    """写入或更新中标结果，返回 inserted/updated。"""
    supplier = (supplier or "").strip()
    brand = (brand or "").strip()
    existing = None
    if supplier:
        existing = conn.execute(
            "SELECT id, brand_name, brand_confidence FROM awards WHERE announcement_id=? AND supplier_raw=?",
            (ann_id, supplier),
        ).fetchone()
    if existing:
        conn.execute(
            "UPDATE awards SET brand_name=COALESCE(NULLIF(?,''), brand_name), "
            "brand_confidence=CASE WHEN ?<>'' THEN 0.9 ELSE brand_confidence END, "
            "amount_yuan=COALESCE(?, amount_yuan), award_date=COALESCE(NULLIF(?,''), award_date) WHERE id=?",
            (brand, brand, amount, award_date, existing["id"]),
        )
        return "updated"
    if not supplier and not brand and amount is None:
        return "skipped"
    if not supplier:
        first = conn.execute("SELECT id FROM awards WHERE announcement_id=? ORDER BY id LIMIT 1", (ann_id,)).fetchone()
        if first:
            conn.execute(
                "UPDATE awards SET brand_name=COALESCE(NULLIF(?,''), brand_name), amount_yuan=COALESCE(?, amount_yuan) "
                "WHERE id=?",
                (brand, amount, first["id"]),
            )
            return "updated"
    conn.execute(
        "INSERT INTO awards(announcement_id, package_no, supplier_raw, supplier_norm, brand_name, brand_confidence, "
        "amount_yuan, amount_raw, award_date, is_winner, brand_basis) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
        (ann_id, "1", supplier, normalize_company(supplier), brand or "未识别",
         0.9 if brand and brand != "未识别" else 0.0, amount, "" if amount is None else str(amount),
         award_date, basis),
    )
    return "inserted"


def import_records(source, filename: str, actor: str = "system", ip: str = "") -> dict:
    """导入 CSV/XLSX。source 为文件流或路径；按公告 URL 合并，备注不会丢失。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in (".csv", ".xlsx", ".xlsm"):
        raise ValueError("仅支持 .csv / .xlsx / .xlsm 文件")
    raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else source.read()
    if not raw:
        raise ValueError("文件为空")
    rows = _read_xlsx(raw) if suffix in (".xlsx", ".xlsm") else _read_csv(raw)
    if len(rows) > 5000:
        raise ValueError("单次最多导入 5000 行")

    stats = {"rows": len(rows), "inserted": 0, "updated": 0, "skipped": 0, "notes": 0,
             "errors": [], "task_id": None}
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO crawl_tasks(source, params, status, started_at) VALUES (?,?,?,?)",
            ("manual_import", json.dumps({"file": filename, "rows": len(rows)}, ensure_ascii=False), "running", now()),
        )
        task_id = cur.lastrowid
        stats["task_id"] = task_id
        for index, row in enumerate(rows, start=2):
            try:
                url = (row.get("url") or "").strip()
                title = (row.get("title") or "").strip()
                if not url.startswith("http") or not title:
                    stats["skipped"] += 1
                    if len(stats["errors"]) < 20:
                        stats["errors"].append(f"第{index}行：缺公告链接或项目名称")
                    continue
                hospital_name = (row.get("hospital") or "").strip()
                if not hospital_name:
                    hospital_name = extract_purchaser("", title)
                if not hospital_name:
                    stats["skipped"] += 1
                    if len(stats["errors"]) < 20:
                        stats["errors"].append(f"第{index}行：缺医院名称且无法从标题识别")
                    continue

                publish = parse_date(row.get("publish_date") or "") or ""
                city_code = _city_code(row.get("city") or "", f"{hospital_name} {title}")
                hospital_id = upsert_hospital(conn, hospital_name, _city_short(city_code), "人工导入", url)
                if not hospital_id:
                    stats["skipped"] += 1
                    continue
                if city_code is None:
                    city_row = conn.execute("SELECT city_code FROM hospitals WHERE id=?", (hospital_id,)).fetchone()
                    city_code = city_row["city_code"] if city_row else None

                amount = _parse_amount(row.get("amount"))
                supplier = (row.get("supplier") or "").strip()
                brand = (row.get("brand") or "").strip()
                if brand == "未识别":
                    brand = ""
                if supplier and not brand:
                    matched, _conf, basis = brands_ops.match(supplier, title, conn=conn)
                    brand = matched or ""
                else:
                    basis = "人工导入"

                existing = conn.execute(
                    "SELECT id, title, publish_date FROM announcements WHERE url=?", (url,)
                ).fetchone()
                if existing:
                    ann_id = existing["id"]
                    conn.execute(
                        "UPDATE announcements SET title=COALESCE(NULLIF(?,''), title), "
                        "publish_date=COALESCE(NULLIF(?,''), publish_date), purchaser_raw=COALESCE(NULLIF(?,''), purchaser_raw), "
                        "purchaser_norm=COALESCE(NULLIF(?,''), purchaser_norm), hospital_id=?, "
                        "city_code=COALESCE(?, city_code), budget_yuan=COALESCE(?, budget_yuan), "
                        "last_seen_at=? WHERE id=?",
                        (title, publish, hospital_name, normalize_name(hospital_name), hospital_id,
                         city_code, amount, now(), ann_id),
                    )
                    stats["updated"] += 1
                else:
                    cur2 = conn.execute(
                        "INSERT INTO announcements(source_site, source_name, url, title, notice_type, publish_date, "
                        "purchaser_raw, purchaser_norm, hospital_id, city_code, budget_yuan, content_hash, raw_text, "
                        "matched_keywords, relevance, fetch_status, first_seen_at, last_seen_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ("manual_import", "人工导入", url, title[:300], "中标公告", publish,
                         hospital_name, normalize_name(hospital_name), hospital_id, city_code, amount,
                         content_hash(f"{title}\n{hospital_name}"), "", "[]", 3.0, "ok", now(), now()),
                    )
                    ann_id = cur2.lastrowid
                    conn.execute(
                        "INSERT OR IGNORE INTO attachments(announcement_id, name, url, kind, source, status, "
                        "is_valid, uploaded_at, last_checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (ann_id, title[:120], url, "中标公告", "announcement", "ok", 1, now(), now()),
                    )
                    stats["inserted"] += 1

                outcome = _ensure_award(conn, ann_id, supplier, brand, amount, publish, basis)
                if outcome == "updated":
                    stats["updated"] += 1

                if _note_upsert(conn, ann_id, url, row.get("note") or "", row.get("tags") or "",
                                row.get("status") or "", actor):
                    stats["notes"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["skipped"] += 1
                if len(stats["errors"]) < 20:
                    stats["errors"].append(f"第{index}行：{exc}")

        conn.execute(
            "UPDATE crawl_tasks SET status=?, finished_at=?, items=?, inserted=?, updated=?, error=? WHERE id=?",
            ("done", now(), stats["rows"], stats["inserted"], stats["updated"],
             "\n".join(stats["errors"])[:2000], task_id),
        )
        audit(conn, actor, "import", json.dumps({k: stats[k] for k in ("rows", "inserted", "updated", "notes")},
                                                ensure_ascii=False), ip)
    return stats


def template_csv() -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADERS)
    return buf.getvalue().encode("utf-8-sig")
