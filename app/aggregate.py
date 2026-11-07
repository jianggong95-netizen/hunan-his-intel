"""查询与聚合：每家医院最近 2 次中标、市州分布、品牌份额、数据质量。"""
from __future__ import annotations

import json
import sqlite3

from .db import db, now
from .regions import CITIES


def _city_map(conn) -> dict[str, str]:
    rows = conn.execute("SELECT code, short FROM regions").fetchall()
    return {r["code"]: r["short"] for r in rows}


def latest_two(city_code: str | None = None, brand: str | None = None, year: str | None = None) -> list[dict]:
    sql = """
    WITH ranked AS (
        SELECT a.id, a.hospital_id, a.publish_date,
               ROW_NUMBER() OVER (PARTITION BY a.hospital_id
                                  ORDER BY COALESCE(a.publish_date,'') DESC, a.id DESC) AS rn
        FROM announcements a
        WHERE a.hospital_id IS NOT NULL AND a.is_void = 0
    )
    SELECT a.*, h.name AS hospital_name, r.rn
    FROM ranked r
    JOIN announcements a ON a.id = r.id
    JOIN hospitals h ON h.id = a.hospital_id
    WHERE r.rn <= 2
    """
    params: list = []
    if city_code:
        sql += " AND a.city_code = ?"
        params.append(city_code)
    if year:
        sql += " AND substr(COALESCE(a.publish_date,''),1,4) = ?"
        params.append(year)
    sql += " ORDER BY h.name, a.publish_date DESC"

    with db() as conn:
        cities = _city_map(conn)
        rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            awards = conn.execute(
                "SELECT supplier_raw, brand_name, brand_confidence, amount_yuan FROM awards WHERE announcement_id=?",
                (row["id"],),
            ).fetchall()
            if brand and not any((a["brand_name"] or "") == brand for a in awards):
                continue
            amounts = [a["amount_yuan"] for a in awards if a["amount_yuan"]]
            brands = []
            for a in awards:
                if a["brand_name"] and a["brand_name"] not in brands:
                    brands.append(a["brand_name"])
            suppliers = [a["supplier_raw"] for a in awards if a["supplier_raw"]]
            atts = conn.execute(
                "SELECT id, name, url, kind, source, status, is_valid, uploader, local_path, sha256, size "
                "FROM attachments WHERE announcement_id=? ORDER BY "
                "CASE kind WHEN '中标公告' THEN 3 WHEN '参数文件' THEN 0 WHEN '招标文件' THEN 1 ELSE 2 END, id",
                (row["id"],),
            ).fetchall()
            result.append(
                {
                    "announcement_id": row["id"],
                    "hospital_id": row["hospital_id"],
                    "hospital_name": row["hospital_name"],
                    "city_code": row["city_code"],
                    "city": cities.get(row["city_code"] or "", "未归属"),
                    "title": row["title"],
                    "url": row["url"],
                    "publish_date": row["publish_date"] or "",
                    "supplier": "、".join(dict.fromkeys(suppliers)) or "未获取",
                    "brand": "、".join(brands) or "未识别",
                    "brand_confidence": max([a["brand_confidence"] or 0 for a in awards], default=0),
                    "amount": max(amounts) if amounts else None,
                    "attachments": [dict(a) for a in atts],
                    "fetch_status": row["fetch_status"],
                    "rank": row["rn"],
                }
            )
        return result


def city_summary() -> list[dict]:
    with db() as conn:
        hospitals = conn.execute(
            "SELECT city_code, COUNT(*) c FROM hospitals WHERE city_code IS NOT NULL GROUP BY city_code"
        ).fetchall()
        anns = conn.execute(
            "SELECT city_code, COUNT(*) c, COUNT(DISTINCT hospital_id) h FROM announcements "
            "WHERE city_code IS NOT NULL AND is_void=0 GROUP BY city_code"
        ).fetchall()
        hmap = {r["city_code"]: r["c"] for r in hospitals}
        amap = {r["city_code"]: r for r in anns}
        out = []
        for c in CITIES:
            a = amap.get(c["code"])
            out.append(
                {
                    "code": c["code"],
                    "name": c["name"],
                    "short": c["short"],
                    "hospitals": hmap.get(c["code"], 0),
                    "announcements": a["c"] if a else 0,
                    "covered_hospitals": a["h"] if a else 0,
                }
            )
        return out


def brand_share() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(brand_name,''),'未识别') b, COUNT(DISTINCT announcement_id) c, "
            "SUM(COALESCE(amount_yuan,0)) amount FROM awards GROUP BY b ORDER BY c DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def quality_stats() -> dict:
    with db() as conn:
        total_ann = conn.execute("SELECT COUNT(*) c FROM announcements").fetchone()["c"]
        total_h = conn.execute("SELECT COUNT(*) c FROM hospitals WHERE merged_into IS NULL").fetchone()["c"]
        unreachable = conn.execute(
            "SELECT COUNT(*) c FROM announcements WHERE fetch_status != 'ok'"
        ).fetchone()["c"]
        missing_amount = conn.execute(
            "SELECT COUNT(*) c FROM announcements a WHERE NOT EXISTS "
            "(SELECT 1 FROM awards w WHERE w.announcement_id=a.id AND w.amount_yuan IS NOT NULL)"
        ).fetchone()["c"]
        unknown_brand = conn.execute(
            "SELECT COUNT(*) c FROM awards WHERE brand_name='未识别' OR brand_name IS NULL OR brand_name=''"
        ).fetchone()["c"]
        total_awards = conn.execute("SELECT COUNT(*) c FROM awards").fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) c FROM awards WHERE review_status='pending'"
        ).fetchone()["c"]
        missing_att = conn.execute(
            "SELECT COUNT(*) c FROM announcements a WHERE NOT EXISTS "
            "(SELECT 1 FROM attachments t WHERE t.announcement_id=a.id AND t.kind IN ('参数文件','招标文件','其他'))"
        ).fetchone()["c"]
        missing_city = conn.execute(
            "SELECT COUNT(*) c FROM hospitals WHERE city_code IS NULL AND merged_into IS NULL"
        ).fetchone()["c"]
        no_hospital = conn.execute(
            "SELECT COUNT(*) c FROM announcements WHERE hospital_id IS NULL"
        ).fetchone()["c"]
        atts = conn.execute("SELECT status, COUNT(*) c FROM attachments GROUP BY status").fetchall()
        kinds = conn.execute(
            "SELECT kind, COUNT(*) c, SUM(CASE WHEN is_valid=1 THEN 1 ELSE 0 END) v FROM attachments GROUP BY kind"
        ).fetchall()
        sources = conn.execute("SELECT source, COUNT(*) c FROM attachments GROUP BY source").fetchall()
        unread = conn.execute("SELECT COUNT(*) c FROM notifications WHERE is_read=0").fetchone()["c"]
        return {
            "announcements": total_ann,
            "hospitals": total_h,
            "detail_unreachable": unreachable,
            "missing_amount": missing_amount,
            "unknown_brand_awards": unknown_brand,
            "awards": total_awards,
            "unknown_brand_ratio": round(unknown_brand / total_awards, 3) if total_awards else 0,
            "pending_review": pending,
            "missing_attachment": missing_att,
            "missing_city": missing_city,
            "no_hospital": no_hospital,
            "unread_notifications": unread,
            "attachments": {r["status"]: r["c"] for r in atts},
            "attachments_by_kind": {r["kind"]: {"count": r["c"], "valid": r["v"] or 0} for r in kinds},
            "attachments_by_source": {r["source"]: r["c"] for r in sources},
        }


def hospitals_list(city_code: str | None = None, q: str | None = None) -> list[dict]:
    sql = "SELECT * FROM hospitals WHERE merged_into IS NULL"
    params: list = []
    if city_code:
        sql += " AND city_code=?"
        params.append(city_code)
    if q:
        sql += " AND (name LIKE ? OR norm_name LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    sql += " ORDER BY city_code, name"
    with db() as conn:
        cities = _city_map(conn)
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            cnt = conn.execute(
                "SELECT COUNT(*) c FROM announcements WHERE hospital_id=?", (r["id"],)
            ).fetchone()["c"]
            d = dict(r)
            d["city"] = cities.get(r["city_code"] or "", "未归属")
            d["announcement_count"] = cnt
            out.append(d)
        return out


def hospital_timeline(hospital_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT a.*, (SELECT GROUP_CONCAT(DISTINCT brand_name) FROM awards w WHERE w.announcement_id=a.id) brands, "
            "(SELECT MAX(amount_yuan) FROM awards w WHERE w.announcement_id=a.id) amount, "
            "(SELECT COUNT(*) FROM attachments t WHERE t.announcement_id=a.id) att_count "
            "FROM announcements a WHERE a.hospital_id=? AND a.is_void=0 ORDER BY a.publish_date DESC",
            (hospital_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def tasks(limit: int = 20) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM crawl_tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def task_logs(task_id: int, limit: int = 200) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM crawl_logs WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def source_health() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM source_health ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def review_queue(limit: int = 200) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT w.id AS award_id, w.announcement_id, w.package_no, w.supplier_raw, w.brand_name, "
            "w.brand_confidence, w.brand_basis, w.amount_yuan, w.award_date, a.title, a.url, "
            "h.name AS hospital_name, h.city_code "
            "FROM awards w JOIN announcements a ON a.id=w.announcement_id "
            "LEFT JOIN hospitals h ON h.id=a.hospital_id "
            "WHERE w.review_status='pending' ORDER BY w.brand_confidence ASC, w.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        cities = _city_map(conn)
    out = []
    for r in rows:
        d = dict(r)
        d["city"] = cities.get(r["city_code"] or "", "未归属")
        out.append(d)
    return out


def rules_list() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM supplier_brand_rules ORDER BY priority, id").fetchall()
        return [dict(r) for r in rows]


def notifications(limit: int = 50) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT n.id, n.announcement_id, n.kind, n.created_at, n.is_read, a.title, a.url, "
            "h.name AS hospital_name, w.brand_name, w.amount_yuan "
            "FROM notifications n JOIN announcements a ON a.id=n.announcement_id "
            "LEFT JOIN hospitals h ON h.id=a.hospital_id "
            "LEFT JOIN awards w ON w.announcement_id=a.id "
            "ORDER BY n.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_notifications_read(ids: list[int] | None = None) -> int:
    with db() as conn:
        if ids:
            cur = conn.execute(
                "UPDATE notifications SET is_read=1 WHERE id IN (%s)" % ",".join("?" * len(ids)), ids
            )
        else:
            cur = conn.execute("UPDATE notifications SET is_read=1 WHERE is_read=0")
        return cur.rowcount


def unmatched_hospitals() -> list[dict]:
    """未匹配到卫健委名录的医院（含无市州归属）。"""
    with db() as conn:
        rows = conn.execute(
            "SELECT h.*, (SELECT COUNT(*) FROM announcements a WHERE a.hospital_id=h.id) ann_count "
            "FROM hospitals h WHERE h.merged_into IS NULL AND h.is_master=0 AND NOT EXISTS ("
            "SELECT 1 FROM hospitals m WHERE m.is_master=1 AND m.merged_into IS NULL AND ("
            "m.norm_name=h.norm_name OR m.aliases LIKE '%'||h.norm_name||'%')) "
            "ORDER BY ann_count DESC, h.name"
        ).fetchall()
        cities = _city_map(conn)
    out = []
    for r in rows:
        d = dict(r)
        d["city"] = cities.get(r["city_code"] or "", "未归属")
        out.append(d)
    return out


def merge_hospitals(source_id: int, target_id: int, actor: str = "system") -> dict:
    """把重复医院并入目标医院：迁移公告、合并别名、保留原行标记 merged_into。"""
    if source_id == target_id:
        raise ValueError("不能合并到自身")
    with db() as conn:
        src = conn.execute("SELECT * FROM hospitals WHERE id=?", (source_id,)).fetchone()
        dst = conn.execute("SELECT * FROM hospitals WHERE id=?", (target_id,)).fetchone()
        if not src or not dst:
            raise ValueError("医院不存在")
        if src["merged_into"]:
            raise ValueError("源医院已被合并")
        aliases = []
        for raw in (dst["aliases"], src["aliases"]):
            try:
                aliases.extend(json.loads(raw or "[]"))
            except Exception:  # noqa: BLE001
                pass
        for name in (src["name"], src["norm_name"]):
            if name and name not in aliases and name != dst["norm_name"]:
                aliases.append(name)
        conn.execute(
            "UPDATE hospitals SET aliases=? WHERE id=?",
            (json.dumps(aliases, ensure_ascii=False), target_id),
        )
        conn.execute("UPDATE announcements SET hospital_id=? WHERE hospital_id=?", (target_id, source_id))
        conn.execute("UPDATE hospitals SET merged_into=? WHERE id=?", (target_id, source_id))
        conn.execute(
            "INSERT INTO change_history(entity, entity_id, field, old_value, new_value, changed_at, actor) "
            "VALUES (?,?,?,?,?,?,?)",
            ("hospital", source_id, "merged_into", "", str(target_id), now(), actor),
        )
        conn.execute(
            "INSERT INTO audit_logs(username, action, detail, ip, ts) VALUES (?,?,?,?,?)",
            (actor, "hospital_merge", f"{source_id}->{target_id}", "", now()),
        )
    return {"ok": True, "source": source_id, "target": target_id}
