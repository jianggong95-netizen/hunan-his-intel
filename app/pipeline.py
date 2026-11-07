"""采集-解析-入库流水线：去重、变更留痕、附件留存。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from . import config
from .collectors.base import Fetcher, STOP_EVENT, PAUSE_EVENT
from .collectors.detail import parse_detail
from .collectors.huaihua import HuaihuaCollector
from .collectors.hunan_search import HunanGovSearchCollector
from .collectors.registry import build_collectors, health_check
from .db import audit, db, log, now
from .normalize import (
    clean_text,
    classify_attachment,
    extract_amount,
    extract_purchaser,
    extract_suppliers,
    his_relevance,
    is_award_notice,
    is_void,
    match_brand,
    normalize_company,
    normalize_name,
    parse_date,
    guess_city,
)
from .regions import CITY_BY_SHORT, detect_city

TITLE_HINTS = ("信息系统", "信息化", "智慧医院", "电子病历", "HIS", "信息平台", "信息管理", "软件",
               "服务器", "数据中心", "互联互通", "网络安全", "系统升级", "系统建设", "云平台")


def _task_create(conn, source: str, params: dict) -> int:
    cur = conn.execute(
        "INSERT INTO crawl_tasks(source, params, status, started_at) VALUES (?,?,?,?)",
        (source, json.dumps(params, ensure_ascii=False), "running", now()),
    )
    return cur.lastrowid


def _task_finish(conn, task_id: int, status: str, pages=0, items=0, inserted=0, updated=0, error="") -> None:
    conn.execute(
        "UPDATE crawl_tasks SET status=?, finished_at=?, pages=?, items=?, inserted=?, updated=?, error=? WHERE id=?",
        (status, now(), pages, items, inserted, updated, error, task_id),
    )


def content_hash(text: str) -> str:
    norm = re.sub(r"\s+", "", text or "")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def upsert_hospital(conn, name: str, city_short: str | None, source_name: str, url: str) -> int | None:
    norm = normalize_name(name)
    if not norm or len(norm) < 4:
        return None
    row = conn.execute("SELECT id, city_code, aliases FROM hospitals WHERE norm_name=?", (norm,)).fetchone()
    city_code = CITY_BY_SHORT[city_short]["code"] if city_short in CITY_BY_SHORT else None
    if row:
        if city_code and not row["city_code"]:
            conn.execute("UPDATE hospitals SET city_code=? WHERE id=?", (city_code, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO hospitals(name, norm_name, city_code, ownership, aliases, source, source_url, first_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (name, norm, city_code, "公立", json.dumps([], ensure_ascii=False), source_name, url, now()),
    )
    return cur.lastrowid


def _save_attachments(conn, fetcher: Fetcher, ann_id: int, attachments: list[dict], logger) -> None:
    for i, att in enumerate(attachments[:6]):
        url = att.get("url")
        if not url:
            continue
        exists = conn.execute("SELECT id FROM attachments WHERE announcement_id=? AND url=?", (ann_id, url)).fetchone()
        if exists:
            continue
        kind = att.get("kind") or classify_attachment(att.get("name", ""), url)
        cur = conn.execute(
            "INSERT INTO attachments(announcement_id, name, url, kind, source, status, is_valid, uploaded_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ann_id, att.get("name", "")[:120], url, kind, "direct", "unverified", 1, now()),
        )
        att_id = cur.lastrowid
        if not config.DOWNLOAD_ATTACHMENTS or i >= 3:
            continue
        ext = Path(urlparse(url).path).suffix or ".bin"
        dest = config.ATTACHMENT_DIR / str(ann_id) / f"{att_id}{ext}"
        ok, sha, size = fetcher.download(url, dest)
        if ok:
            conn.execute(
                "UPDATE attachments SET local_path=?, sha256=?, size=?, status=?, is_valid=1, "
                "checked_at=?, last_checked_at=? WHERE id=?",
                (str(dest.relative_to(config.BASE_DIR)), sha, size, "ok", now(), now(), att_id),
            )
            logger(f"[附件] 已留存 {att.get('name') or url}")
        else:
            conn.execute(
                "UPDATE attachments SET status=?, is_valid=0, checked_at=?, last_checked_at=? WHERE id=?",
                ("download_failed", now(), now(), att_id),
            )
            logger(f"[附件] 下载失败 {url}", "warning")


def ingest_item(conn, fetcher: Fetcher, item: dict, logger, since: str | None = None) -> tuple[str, int]:
    """入库一条候选公告，返回 ('inserted'|'updated'|'skipped', ann_id)。"""
    since = since or config.COLLECT_SINCE
    url = item["url"]
    title = item.get("title", "")
    publish = parse_date(item.get("publish_date", "")) or item.get("publish_date") or ""
    if publish and publish < since:
        return "skipped", 0
    if is_void(title):
        return "skipped", 0
    if not is_award_notice(item.get("notice_type", ""), title):
        return "skipped", 0

    existing = conn.execute("SELECT id, content_hash FROM announcements WHERE url=?", (url,)).fetchone()

    detail = None
    fetch_status = "ok"
    try:
        resp = fetcher.get(url, retries=1)
        if resp is None or resp.status_code != 200:
            fetch_status = "detail_unreachable"
        else:
            detail = parse_detail(resp.text, url)
    except Exception as exc:  # noqa: BLE001
        fetch_status = "detail_unreachable"
        logger(f"[详情] 抓取异常 {url}: {exc}", "warning")

    text = clean_text(detail["text"]) if detail else ""
    full_text = f"{title}\n{text}"
    score, matched = his_relevance(title, full_text)
    if score < 3:
        return "skipped", 0
    if is_void(title, text):
        return "skipped", 0

    purchaser = extract_purchaser(text, title)
    if not purchaser:
        m = re.match(r"^(.{4,40}?(?:医院|卫生院|卫生服务中心|妇幼保健院))", title)
        purchaser = normalize_name(m.group(1)) if m else ""
    if not purchaser or not any(k in purchaser for k in ("医院", "卫生院", "妇幼", "卫生服务")):
        return "skipped", 0

    city_short = (
        detect_city(url, item.get("source_name", ""))
        or detect_city(purchaser, title, text[:200])
        or item.get("city_hint")
        or detect_city(title)
    )
    city_code = CITY_BY_SHORT[city_short]["code"] if city_short in CITY_BY_SHORT else None
    hospital_id = upsert_hospital(conn, purchaser, city_short, item.get("source_name", ""), url)

    amount, amount_raw = extract_amount(text)
    suppliers = extract_suppliers(text)
    award_date = parse_date(publish) or publish or ""
    chash = content_hash(full_text)

    if existing:
        conn.execute("UPDATE announcements SET last_seen_at=?, fetch_status=? WHERE id=?", (now(), fetch_status, existing["id"]))
        if existing["content_hash"] and existing["content_hash"] != chash:
            conn.execute(
                "UPDATE announcements SET content_hash=?, raw_text=?, title=? WHERE id=?",
                (chash, text[:20000], title, existing["id"]),
            )
            conn.execute(
                "INSERT INTO change_history(entity, entity_id, field, old_value, new_value, changed_at, actor) "
                "VALUES (?,?,?,?,?,?,?)",
                ("announcement", existing["id"], "content_hash", existing["content_hash"], chash, now(), "collector"),
            )
            return "updated", existing["id"]
        return "skipped", existing["id"]

    cur = conn.execute(
        "INSERT INTO announcements(source_site, source_name, url, notice_id, title, notice_type, publish_date, "
        "purchaser_raw, purchaser_norm, hospital_id, city_code, budget_yuan, content_hash, raw_text, matched_keywords, "
        "relevance, fetch_status, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            item.get("source_id", ""), item.get("source_name", ""), url, item.get("notice_id", ""),
            title[:300], item.get("notice_type", ""), award_date, purchaser, normalize_name(purchaser),
            hospital_id, city_code, amount, chash, text[:20000],
            json.dumps(matched, ensure_ascii=False), score, fetch_status, now(), now(),
        ),
    )
    ann_id = cur.lastrowid

    # 三来源之一：公告原文页面本身登记为「中标公告」附件
    conn.execute(
        "INSERT OR IGNORE INTO attachments(announcement_id, name, url, kind, source, status, is_valid, "
        "uploaded_at, last_checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (ann_id, title[:120], url, "中标公告", "announcement", "ok", 1, now(), now()),
    )
    # 站内新中标提醒（增量新增才产生未读）
    conn.execute(
        "INSERT INTO notifications(announcement_id, kind, created_at, is_read) VALUES (?,?,?,0)",
        (ann_id, "new_award", now()),
    )

    if suppliers:
        for idx, supplier in enumerate(suppliers):
            brand, confidence, basis = match_brand(supplier, full_text)
            conn.execute(
                "INSERT INTO awards(announcement_id, package_no, supplier_raw, supplier_norm, brand_name, brand_confidence, "
                "amount_yuan, amount_raw, award_date, is_winner) VALUES (?,?,?,?,?,?,?,?,?,1)",
                (
                    ann_id, "1" if idx == 0 else str(idx + 1), supplier, normalize_company(supplier),
                    brand or "未识别", confidence, amount if idx == 0 else None,
                    amount_raw if idx == 0 else "", award_date,
                ),
            )
    else:
        conn.execute(
            "INSERT INTO awards(announcement_id, package_no, supplier_raw, supplier_norm, brand_name, brand_confidence, "
            "amount_yuan, amount_raw, award_date, is_winner) VALUES (?,?,?,?,?,?,?,?,?,1)",
            (ann_id, "1", "", "", "未识别", 0.0, amount, amount_raw, award_date),
        )

    if detail and detail.get("attachments"):
        _save_attachments(conn, fetcher, ann_id, detail["attachments"], logger)

    return "inserted", ann_id


def run_collection(sources: list[str] | None = None, keywords: list[str] | None = None,
                   since: str | None = None, max_pages: int | None = None,
                   actor: str = "system") -> dict:
    """执行一次采集任务，返回统计。"""
    since = since or config.COLLECT_SINCE
    keywords = keywords or config.load_keywords()
    sources = sources or ["hunan_gov_search", "huaihua_ggzy"]
    STOP_EVENT.clear()
    PAUSE_EVENT.clear()
    fetcher = Fetcher()
    stats = {"items": 0, "inserted": 0, "updated": 0, "skipped": 0, "pages": 0}

    with db() as conn:
        task_id = _task_create(conn, "+".join(sources), {"keywords": keywords, "since": since})
        logger = lambda msg, level="info": log(conn, task_id, msg, level)
        collectors = build_collectors(fetcher, logger)

        try:
            candidates: list[dict] = []
            for sid in sources:
                if STOP_EVENT.is_set():
                    break
                if sid == "hunan_gov_search":
                    items = collectors[sid].collect(keywords[:3], max_pages=max_pages)
                    filtered = [
                        it for it in items
                        if any(h in it["title"] for h in ("医院", "卫生院", "妇幼"))
                        and is_award_notice(it.get("notice_type", ""), it["title"])
                    ]
                    for it in filtered:
                        it["source_id"] = sid
                    candidates.extend(filtered)
                    stats["pages"] += 1
                elif sid == "huaihua_ggzy":
                    items = collectors[sid].collect(since=since, max_pages=max_pages * 6)
                    filtered = [
                        it for it in items
                        if any(h in it["title"] for h in ("医院", "卫生院", "妇幼"))
                        and any(h in it["title"] for h in TITLE_HINTS)
                    ]
                    for it in filtered:
                        it["source_id"] = sid
                    candidates.extend(filtered)
                    stats["pages"] += 1

            stats["items"] = len(candidates)
            logger(f"候选公告 {len(candidates)} 条，开始逐条解析")

            for item in candidates:
                if STOP_EVENT.is_set():
                    break
                try:
                    status, _ = ingest_item(conn, fetcher, item, logger, since)
                    stats[status] = stats.get(status, 0) + 1
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    logger(f"入库失败 {item.get('url')}: {exc}", "error")

            _task_finish(conn, task_id, "stopped" if STOP_EVENT.is_set() else "done",
                         stats["pages"], stats["items"], stats["inserted"], stats["updated"])
            audit(conn, actor, "collect", json.dumps(stats, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger(f"任务异常终止: {exc}", "error")
            _task_finish(conn, task_id, "failed", error=str(exc))
            raise
    stats["task_id"] = task_id
    return stats


def refresh_health() -> list[dict]:
    fetcher = Fetcher()
    results = health_check(fetcher)
    with db() as conn:
        for r in results:
            conn.execute(
                "INSERT INTO source_health(source, last_check, status, http_code, note) VALUES (?,?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET last_check=excluded.last_check, status=excluded.status, "
                "http_code=excluded.http_code, note=excluded.note",
                (r["source"], now(), r["status"], r["http_code"], r["note"]),
            )
    return results
