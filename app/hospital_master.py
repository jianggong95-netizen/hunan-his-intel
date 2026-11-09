"""医院主数据：公开名录导入、别名归并、未匹配清单与手工合并。

公开渠道拿不到完整名录时，用本模块的 CSV 模板导入；
导入行标记 is_master=1，作为别名归并的目标主数据。
"""
from __future__ import annotations

import csv
import io
import json
import re

from openpyxl import load_workbook

from . import aggregate
from .db import audit, db, now
from .normalize import normalize_name
from .regions import CITY_BY_SHORT, detect_city

TEMPLATE_HEADERS = ["医院名称", "市州", "等级", "别名", "来源链接"]

_HEADER_ALIASES = {
    "name": ("医院名称", "医院", "名称", "医疗机构名称"),
    "city": ("市州", "城市", "地区", "所属市州"),
    "level": ("等级", "医院等级", "级别"),
    "aliases": ("别名", "曾用名", "简称"),
    "source_url": ("来源链接", "来源", "链接", "source"),
}
_STRIP = re.compile(r"[\s（）()：:]+")


def _norm_header(value) -> str:
    return _STRIP.sub("", str(value or "").replace("\ufeff", "")).lower()


_HEADER_MAP = {_norm_header(a): f for f, al in _HEADER_ALIASES.items() for a in al}


def template_csv() -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow(["示例：XX县人民医院", "邵阳市", "二级甲等", "XX人民医院|XX县医院", "https://wjw.hunan.gov.cn/…"])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _read_rows(raw: bytes, filename: str) -> list[dict]:
    lower = (filename or "").lower()
    if lower.endswith((".xlsx", ".xlsm")):
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        cells = list(ws.iter_rows(values_only=True))
        if not cells:
            return []
        headers = [_norm_header(h) for h in cells[0]]
        out = []
        for row in cells[1:]:
            record = {}
            for idx, header in enumerate(headers):
                field = _HEADER_MAP.get(header)
                if field and idx < len(row):
                    record[field] = str(row[idx] or "").strip()
            if record.get("name"):
                out.append(record)
        return out
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
            if field and idx < len(cells):
                record[field] = cells[idx].strip()
        if record.get("name"):
            out.append(record)
    return out


def _split_aliases(value: str) -> list[str]:
    return [p.strip() for p in re.split(r"[|,，;；、]", value or "") if p.strip()]


def import_master(stream, filename: str, actor: str = "system", ip: str = "") -> dict:
    """导入医院名录，标记 is_master=1；同名医院只补全字段，不覆盖已采集信息。"""
    raw = stream.read() if hasattr(stream, "read") else stream
    rows = _read_rows(raw, filename)
    added = updated = skipped = 0
    errors: list[str] = []
    with db() as conn:
        for idx, row in enumerate(rows, start=2):
            name = (row.get("name") or "").strip()
            if not name or name.startswith("示例"):
                skipped += 1
                continue
            norm = normalize_name(name)
            if len(norm) < 4:
                errors.append(f"第{idx}行：医院名称过短（{name}）")
                skipped += 1
                continue
            city_raw = (row.get("city") or "").strip()
            short = city_raw.replace("市", "").replace("土家族苗族自治州", "").strip()
            if short not in CITY_BY_SHORT:
                short = detect_city(name) or ""
            city_code = CITY_BY_SHORT[short]["code"] if short in CITY_BY_SHORT else None
            aliases = _split_aliases(row.get("aliases") or "")
            existing = conn.execute("SELECT id FROM hospitals WHERE norm_name=?", (norm,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE hospitals SET is_master=1, city_code=COALESCE(city_code, ?), "
                    "level=COALESCE(NULLIF(level,''), ?), source=COALESCE(source,'名录导入'), "
                    "source_url=COALESCE(source_url, ?) WHERE id=?",
                    (city_code, row.get("level") or "", row.get("source_url") or "", existing["id"]),
                )
                if aliases:
                    old = conn.execute("SELECT aliases FROM hospitals WHERE id=?", (existing["id"],)).fetchone()["aliases"]
                    merged = _merge_aliases(old, aliases)
                    conn.execute("UPDATE hospitals SET aliases=? WHERE id=?", (merged, existing["id"]))
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO hospitals(name, norm_name, city_code, level, ownership, aliases, source, "
                    "source_url, first_seen_at, is_master) VALUES (?,?,?,?,?,?,?,?,?,1)",
                    (name, norm, city_code, row.get("level") or "", "公立",
                     json.dumps(aliases, ensure_ascii=False), "名录导入", row.get("source_url") or "", now()),
                )
                added += 1
        audit(conn, actor, "hospital_master_import",
              json.dumps({"file": filename, "added": added, "updated": updated, "skipped": skipped}, ensure_ascii=False), ip)
    return {"rows": len(rows), "added": added, "updated": updated, "skipped": skipped, "errors": errors[:20]}


def _merge_aliases(old_json: str, new_items: list[str]) -> str:
    try:
        items = json.loads(old_json or "[]")
    except Exception:  # noqa: BLE001
        items = []
    for name in new_items:
        if name and name not in items:
            items.append(name)
    return json.dumps(items, ensure_ascii=False)


def _loose(name: str) -> str:
    """去掉行政区级别字，便于「洞口县人民医院」≈「洞口人民医院」。"""
    return re.sub(r"(自治县|县|市|区|州)", "", name or "")


def suggestions(limit: int = 200) -> list[dict]:
    """为未匹配的采集医院给出可能的卫健委名录目标（同名/别名/包含关系）。"""
    with db() as conn:
        masters = conn.execute(
            "SELECT id, name, norm_name, city_code, aliases FROM hospitals "
            "WHERE is_master=1 AND merged_into IS NULL"
        ).fetchall()
        candidates = conn.execute(
            "SELECT h.id, h.name, h.norm_name, h.city_code, "
            "(SELECT COUNT(*) FROM announcements a WHERE a.hospital_id=h.id) ann_count "
            "FROM hospitals h WHERE h.merged_into IS NULL AND h.is_master=0 ORDER BY ann_count DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for cand in candidates:
        best = None
        best_score = 0.0
        for m in masters:
            score = 0.0
            aliases = []
            try:
                aliases = json.loads(m["aliases"] or "[]")
            except Exception:  # noqa: BLE001
                pass
            if m["norm_name"] == cand["norm_name"]:
                score = 1.0
            elif _loose(m["norm_name"]) and _loose(m["norm_name"]) == _loose(cand["norm_name"]):
                score = 0.9
            elif cand["norm_name"] in aliases or m["norm_name"] in aliases:
                score = 0.95
            elif m["norm_name"] and (m["norm_name"] in cand["norm_name"] or cand["norm_name"] in m["norm_name"]):
                score = 0.75
            if score > best_score and (m["city_code"] == cand["city_code"] or not cand["city_code"]):
                best, best_score = m, score
        d = dict(cand)
        d["suggestion"] = (
            {"id": best["id"], "name": best["name"], "score": best_score} if best else None
        )
        out.append(d)
    return out


def merge(source_id: int, target_id: int, actor: str = "system", ip: str = "") -> dict:
    result = aggregate.merge_hospitals(source_id, target_id, actor)
    with db() as conn:
        audit(conn, actor, "hospital_merge_manual", f"{source_id}->{target_id}", ip)
    return result
