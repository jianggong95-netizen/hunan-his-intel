"""品牌识别：包件拆分、集成商背后厂家推断、待核队列与规则维护。"""
from __future__ import annotations

import json
import re

from .db import audit, db, now
from .normalize import match_brand as _catalog_match
from . import config

# 集成商/运营商特征：其中标结果通常不是最终 HIS 厂家
INTEGRATOR_WORDS = (
    "中国电信", "电信", "中国联通", "联通", "中国移动", "移动通信", "联合网络通信", "铁塔", "广电",
    "数智", "系统集成", "信息产业", "信息技术", "网络科技", "智慧城市", "大数据",
)
# 包件/标段分隔
_PACKAGE_RE = re.compile(r"(?:合同包|采购包|标段|包件|分包|包)\s*([0-9一二三四五六七八九十]+)")
# 代理机构/咨询公司，不是中标单位
AGENCY_WORDS = (
    "招标有限责任公司", "招标有限公司", "招标代理", "招标咨询", "管理咨询",
    "采购中心", "咨询有限公司", "项目管理有限公司",
)

CONFIRMED = "confirmed"
PENDING = "pending"
NONE = "none"


def split_packages(text: str) -> list[dict]:
    """按「包1/第一包/标段二」等标记拆分正文，返回 [{package_no, text}]。"""
    if not text:
        return []
    marks = list(_PACKAGE_RE.finditer(text))
    if len(marks) < 2:
        return []
    out = []
    for idx, m in enumerate(marks):
        start = m.start()
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(text)
        segment = text[start:end].strip()
        if len(segment) < 6:
            continue
        out.append({"package_no": m.group(0), "text": segment})
    return out


def is_integrator(supplier: str) -> bool:
    return any(w in (supplier or "") for w in INTEGRATOR_WORDS)


def is_agency(supplier: str) -> bool:
    return any(w in (supplier or "") for w in AGENCY_WORDS)


def _rules(conn=None) -> list[dict]:
    if conn is not None:
        rows = conn.execute("SELECT * FROM supplier_brand_rules ORDER BY priority, id").fetchall()
        return [dict(r) for r in rows]
    with db() as own:
        rows = own.execute("SELECT * FROM supplier_brand_rules ORDER BY priority, id").fetchall()
        return [dict(r) for r in rows]


def _brand_aliases() -> list[tuple[str, str]]:
    pairs = []
    for brand in config.load_brands():
        for alias in brand.get("aliases", []):
            if alias and len(alias) >= 2:
                pairs.append((alias, brand["name"]))
    return pairs


def _hit(text: str, pattern: str, match_type: str) -> bool:
    if not text or not pattern:
        return False
    if match_type == "regex":
        try:
            return re.search(pattern, text) is not None
        except re.error:
            return False
    return pattern in text


def match(supplier: str, text: str, package_text: str = "", conn=None) -> tuple[str | None, float, str]:
    """返回 (品牌, 置信度, 依据)。规则库优先；集成商需正文佐证，否则留待人工核。"""
    supplier = supplier or ""
    evidence = f"{package_text or ''}\n{text or ''}"
    rules = _rules(conn)
    for rule in rules:
        if _hit(supplier, rule["pattern"], rule["match_type"] or "contains"):
            return rule["brand_name"], 0.9, f"规则库：供应商名称命中「{rule['pattern']}」"
    for alias, brand in _brand_aliases():
        if alias in supplier:
            return brand, 0.85, f"供应商名称命中别名「{alias}」"
    if is_integrator(supplier):
        for rule in rules:
            if _hit(evidence, rule["pattern"], rule["match_type"] or "contains"):
                return rule["brand_name"], 0.6, f"集成商（{supplier}），正文/包件佐证「{rule['pattern']}」"
        for alias, brand in _brand_aliases():
            if _hit(evidence, alias, "contains"):
                return brand, 0.55, f"集成商（{supplier}），正文提到「{alias}」"
        return None, 0.0, "集成商，正文未见明确厂家"
    brand, confidence, basis = _catalog_match(supplier, text, allow_text=False)
    return brand, confidence, basis


def _review_status(brand: str | None, confidence: float) -> str:
    if not brand or brand == "未识别" or confidence < 0.7:
        return PENDING
    return NONE


def recompute_awards(ann_id: int | None = None, actor: str = "system") -> dict:
    """重算中标记录的品牌：包件拆分 + 集成商识别；人工已确认的不覆盖。"""
    updated = split = pending = confirmed_kept = 0
    with db() as conn:
        sql = "SELECT id, title, raw_text FROM announcements"
        params: list = []
        if ann_id:
            sql += " WHERE id=?"
            params.append(ann_id)
        rows = conn.execute(sql, params).fetchall()
        for ann in rows:
            text = ann["raw_text"] or ""
            packages = split_packages(text)
            awards = conn.execute(
                "SELECT id, package_no, supplier_raw, brand_name, brand_confidence, review_status "
                "FROM awards WHERE announcement_id=? ORDER BY id",
                (ann["id"],),
            ).fetchall()
            if not awards:
                continue
            for award in awards:
                if award["review_status"] == CONFIRMED:
                    confirmed_kept += 1
                    continue
                if is_agency(award["supplier_raw"] or ""):
                    conn.execute(
                        "UPDATE awards SET brand_name='未识别', brand_confidence=0, "
                        "brand_basis='疑似代理机构，不计入中标单位', review_status='pending', "
                        "is_winner=0 WHERE id=?",
                        (award["id"],),
                    )
                    updated += 1
                    pending += 1
                    continue
                pkg = ""
                if packages and award["package_no"]:
                    for p in packages:
                        if award["package_no"] in p["package_no"] or p["package_no"].endswith(award["package_no"]):
                            pkg = p["text"]
                            break
                brand, confidence, basis = match(award["supplier_raw"] or "", text, pkg, conn=conn)
                status = _review_status(brand, confidence)
                conn.execute(
                    "UPDATE awards SET brand_name=?, brand_confidence=?, brand_basis=?, review_status=? WHERE id=?",
                    (brand or "未识别", confidence, basis, status, award["id"]),
                )
                updated += 1
                if status == PENDING:
                    pending += 1
                if pkg:
                    split += 1
        conn.execute(
            "INSERT INTO audit_logs(username, action, detail, ip, ts) VALUES (?,?,?,?,?)",
            (actor, "brand_recompute",
             json.dumps({"ann_id": ann_id, "updated": updated, "split": split, "pending": pending},
                        ensure_ascii=False), "", now()),
        )
    return {"updated": updated, "package_matched": split, "pending": pending,
            "confirmed_kept": confirmed_kept}


def confirm_award(award_id: int, brand: str, actor: str = "system", ip: str = "") -> dict:
    brand = (brand or "").strip()
    if not brand:
        raise ValueError("请填写确认后的厂家名称")
    with db() as conn:
        row = conn.execute("SELECT id FROM awards WHERE id=?", (award_id,)).fetchone()
        if not row:
            raise ValueError("中标记录不存在")
        conn.execute(
            "UPDATE awards SET brand_name=?, brand_confidence=1.0, brand_basis=?, review_status=?, "
            "reviewed_by=?, reviewed_at=? WHERE id=?",
            (brand, "人工确认", CONFIRMED, actor, now(), award_id),
        )
        audit(conn, actor, "brand_confirm", f"award={award_id} brand={brand}", ip)
    return {"ok": True, "award_id": award_id, "brand": brand}


def mark_pending(award_id: int, actor: str = "system", ip: str = "") -> dict:
    with db() as conn:
        conn.execute(
            "UPDATE awards SET review_status=?, reviewed_by=?, reviewed_at=? WHERE id=?",
            (PENDING, actor, now(), award_id),
        )
        audit(conn, actor, "brand_mark_pending", f"award={award_id}", ip)
    return {"ok": True}


def rules_list() -> list[dict]:
    return _rules()


def add_rule(pattern: str, brand_name: str, match_type: str = "contains",
             priority: int = 1, note: str = "", actor: str = "system", ip: str = "") -> dict:
    pattern, brand_name = (pattern or "").strip(), (brand_name or "").strip()
    if not pattern or not brand_name:
        raise ValueError("规则与厂家名称不能为空")
    if match_type == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"正则无效：{exc}") from exc
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO supplier_brand_rules(pattern, brand_name, match_type, priority, note) VALUES (?,?,?,?,?)",
            (pattern, brand_name, match_type, int(priority or 1), note),
        )
        audit(conn, actor, "brand_rule_add", f"{pattern}->{brand_name}", ip)
        return dict(conn.execute("SELECT * FROM supplier_brand_rules WHERE id=?", (cur.lastrowid,)).fetchone())


def update_rule(rule_id: int, **fields) -> dict:
    allowed = {"pattern", "brand_name", "match_type", "priority", "note"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        raise ValueError("没有需要更新的字段")
    with db() as conn:
        if not conn.execute("SELECT id FROM supplier_brand_rules WHERE id=?", (rule_id,)).fetchone():
            raise ValueError("规则不存在")
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE supplier_brand_rules SET {sets} WHERE id=?", (*updates.values(), rule_id))
        return dict(conn.execute("SELECT * FROM supplier_brand_rules WHERE id=?", (rule_id,)).fetchone())


def delete_rule(rule_id: int, actor: str = "system", ip: str = "") -> dict:
    with db() as conn:
        conn.execute("DELETE FROM supplier_brand_rules WHERE id=?", (rule_id,))
        audit(conn, actor, "brand_rule_delete", str(rule_id), ip)
    return {"ok": True}


def unknown_ratio() -> dict:
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM awards WHERE is_winner=1").fetchone()["c"]
        unknown = conn.execute(
            "SELECT COUNT(*) c FROM awards WHERE is_winner=1 "
            "AND (brand_name='未识别' OR brand_name IS NULL OR brand_name='')"
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) c FROM awards WHERE review_status='pending' AND is_winner=1"
        ).fetchone()["c"]
        excluded = conn.execute("SELECT COUNT(*) c FROM awards WHERE is_winner=0").fetchone()["c"]
    return {"awards": total, "unknown": unknown,
            "ratio": round(unknown / total, 3) if total else 0, "pending": pending,
            "excluded": excluded}
