"""采集-解析-入库流水线：去重、变更留痕、附件留存。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    normalize_company,
    normalize_name,
    parse_date,
    guess_city,
)
from . import agent_reach, brands_ops, page_extract, search_plan, source_state
from .regions import CITY_BY_SHORT, detect_city
from .search_plan import TITLE_HINTS


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


def add_award(conn, ann_id: int, package_no: str, supplier: str, amount, amount_raw: str,
              award_date: str, full_text: str, amount_info: dict | None = None,
              missing_reason: str = "") -> None:
    brand, confidence, basis = brands_ops.match(supplier, full_text, conn=conn)
    status = "pending" if (not brand or confidence < 0.7) else "none"
    info = amount_info or {}
    cents = info.get("amount_cents")
    if cents is None and amount is not None:
        cents = int(round(float(amount) * 100))
    if not missing_reason:
        if not supplier:
            missing_reason = "未提取到中标供应商"
        elif amount is None:
            missing_reason = "未提取到中标金额"
    conn.execute(
        "INSERT INTO awards(announcement_id, package_no, supplier_raw, supplier_norm, brand_name, brand_confidence, "
        "amount_yuan, amount_raw, award_date, is_winner, brand_basis, review_status, "
        "amount_cents, amount_kind, tax_included, amount_basis, missing_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
        (ann_id, package_no, supplier, normalize_company(supplier), brand or "未识别", confidence,
         amount, amount_raw, award_date, basis, status, cents, info.get("amount_kind"),
         info.get("tax_included"), info.get("amount_basis"), missing_reason),
    )


def _save_attachments(conn, fetcher: Fetcher, ann_id: int, attachments: list[dict], logger,
                      download: bool = True) -> int:
    """登记附件并尝试留存直链文件，返回新增条数。

    正文里的交易中心包件页等非直链只登记（status=unverified, note 说明），不强行下载。
    """
    added = 0
    for i, att in enumerate(attachments[:6]):
        url = att.get("url")
        if not url:
            continue
        exists = conn.execute("SELECT id FROM attachments WHERE announcement_id=? AND url=?", (ann_id, url)).fetchone()
        if exists:
            continue
        kind = att.get("kind") or classify_attachment(att.get("name", ""), url)
        source = att.get("source") or "direct"
        direct = bool(att.get("direct", True))
        note = "" if direct else "正文链接（非直链文件），需人工打开"
        cur = conn.execute(
            "INSERT INTO attachments(announcement_id, name, url, kind, source, status, is_valid, "
            "uploaded_at, last_checked_at, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ann_id, att.get("name", "")[:120], url, kind, source,
             "unverified", 1, now(), now(), note),
        )
        att_id = cur.lastrowid
        added += 1
        if not direct:
            logger(f"[附件] 登记正文链接 {att.get('name') or url}（非直链）")
            continue
        if not download or not config.DOWNLOAD_ATTACHMENTS or i >= 3:
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
    return added


def _record_low_relevance(conn, item: dict, title: str, score: float, reason: str, publish: str = "") -> None:
    """低相关候选进入待核队列，不写主表，避免污染中标统计。"""
    source = item.get("source_id") or item.get("source_site") or ""
    conn.execute(
        "INSERT INTO low_relevance(url, title, source_site, source_name, keyword, score, reason, "
        "publish_date, created_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET title=excluded.title, source_site=excluded.source_site, "
        "source_name=excluded.source_name, keyword=excluded.keyword, score=excluded.score, "
        "reason=excluded.reason, publish_date=excluded.publish_date, last_seen_at=excluded.last_seen_at",
        (item["url"], title[:300], source, item.get("source_name", ""), item.get("query", ""),
         score, reason, publish, now(), now()),
    )


def _merge_ann_sources(conn, ann_id: int, source_site: str, alts: list[str]) -> None:
    """把同一公告的其他站点来源合并进 announcements，并维护来源数。"""
    if not alts:
        return
    row = conn.execute("SELECT source_site, alt_sources FROM announcements WHERE id=?", (ann_id,)).fetchone()
    if not row:
        return
    merged = []
    try:
        merged = [s for s in json.loads(row["alt_sources"] or "[]") if s]
    except (TypeError, ValueError):
        merged = []
    for src in alts:
        if src and src != (row["source_site"] or source_site) and src not in merged:
            merged.append(src)
    conn.execute(
        "UPDATE announcements SET alt_sources=?, source_count=? WHERE id=?",
        (json.dumps(merged, ensure_ascii=False), 1 + len(merged), ann_id),
    )


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
    text = ""
    if "detail_text" in item:
        # 采集器已取详情（省政采走 JSON 详情接口），不再重复请求页面
        text = clean_text(item.get("detail_text") or "")
        detail = {"attachments": item.get("detail_attachments") or [], "html": item.get("detail_html") or ""}
        if not text:
            fetch_status = "detail_unreachable"
    else:
        try:
            resp = fetcher.get(url, retries=1)
            if resp is None or resp.status_code != 200:
                fetch_status = "detail_unreachable"
            else:
                detail = parse_detail(resp.text, url)
                text = clean_text(detail.get("text") or "")
        except Exception as exc:  # noqa: BLE001
            fetch_status = "detail_unreachable"
            logger(f"[详情] 抓取异常 {url}: {exc}", "warning")
    via_agent_reach = False
    if fetch_status == "detail_unreachable" and agent_reach.is_available():
        # Agent-Reach 回退通道：JS 渲染/直连不可达页面的只读兜底（未安装时不进入此分支）
        ar_text, ar_err = agent_reach.read_page(url)
        ar_text = clean_text(ar_text or "")
        if ar_text:
            text = ar_text
            detail = {"attachments": agent_reach.file_links(ar_text), "html": ""}
            fetch_status = "ok"
            via_agent_reach = True
            logger(f"[Agent-Reach] 回退读取成功 {url[:80]}（{len(ar_text)} 字）")
        else:
            logger(f"[Agent-Reach] 回退读取失败 {url[:80]}: {ar_err}", "warning")
    full_text = f"{title}\n{text}"
    score, matched = his_relevance(title, full_text)
    if score < 3:
        _record_low_relevance(conn, item, title, score, f"HIS 相关度 {score:.1f} 低于阈值 3", publish)
        return "low_relevance", 0
    if is_void(title, text):
        return "skipped", 0

    info = page_extract.parse_award_html((detail or {}).get("html") or "", title, text)
    purchaser = info["purchaser"]
    if not purchaser:
        m = re.match(r"^(.{4,40}?(?:医院|卫生院|卫生服务中心|妇幼保健院))", title)
        purchaser = normalize_name(m.group(1)) if m else ""
    if not purchaser or not any(k in purchaser for k in ("医院", "卫生院", "妇幼", "卫生服务")):
        return "skipped", 0

    page_publish = parse_date(info.get("publish_date") or "") or ""
    notice_type = info.get("notice_type") or item.get("notice_type", "")
    project_no = info.get("project_no") or ""
    agency = info.get("agency") or ""
    extract_meta = json.dumps(
        {
            "missing_reasons": info.get("missing_reasons") or {},
            "notice_type_source": "page" if info.get("notice_type") else "list",
            "publish_date_source": "page" if page_publish else "list",
            "agent_reach_fallback": via_agent_reach,
        },
        ensure_ascii=False,
    )
    alt_sources = sorted({s for s in (item.get("_alt_sources") or []) if s})

    city_short = (
        detect_city(url, item.get("source_name", ""))
        or detect_city(purchaser, title, text[:200])
        or item.get("city_hint")
        or detect_city(title)
    )
    city_code = CITY_BY_SHORT[city_short]["code"] if city_short in CITY_BY_SHORT else None
    hospital_id = upsert_hospital(conn, purchaser, city_short, item.get("source_name", ""), url)

    amount, amount_raw = extract_amount(text)
    amount_info = {
        "amount_cents": int(round(amount * 100)) if amount is not None else None,
        "amount_kind": "总价" if amount is not None else "",
        "tax_included": None,
        "amount_basis": amount_raw,
    }
    if info["amount_yuan"]:
        amount, amount_raw = info["amount_yuan"], info["amount_raw"]
        amount_info = {
            "amount_cents": info.get("amount_cents"),
            "amount_kind": info.get("amount_kind"),
            "tax_included": info.get("tax_included"),
            "amount_basis": info.get("amount_basis", ""),
        }
    if item.get("amount_override") is not None:
        amount, amount_raw = item["amount_override"], str(item["amount_override"])
        amount_info = {
            "amount_cents": int(round(float(amount) * 100)),
            "amount_kind": "总价",
            "tax_included": None,
            "amount_basis": "人工覆盖金额",
        }
    packages = info["packages"]
    supplier_override = (item.get("supplier_override") or "").strip()
    if packages:
        suppliers = info["suppliers"]
    elif supplier_override:
        suppliers = [supplier_override]
    else:
        suppliers = info["suppliers"] or extract_suppliers(text)
    award_date = page_publish or parse_date(publish) or publish or ""
    chash = content_hash(full_text)

    if existing:
        _merge_ann_sources(conn, existing["id"], item.get("source_id", ""), alt_sources)
        conn.execute(
            "UPDATE announcements SET last_seen_at=?, fetch_status=?, "
            "notice_type=CASE WHEN COALESCE(notice_type,'')='' THEN ? ELSE notice_type END, "
            "project_no=CASE WHEN COALESCE(project_no,'')='' THEN ? ELSE project_no END, "
            "agency=CASE WHEN COALESCE(agency,'')='' THEN ? ELSE agency END, "
            "extract_meta=? WHERE id=?",
            (now(), fetch_status, notice_type, project_no, agency, extract_meta, existing["id"]),
        )
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
        "relevance, fetch_status, first_seen_at, last_seen_at, source_count, alt_sources, project_no, agency, "
        "extract_meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            item.get("source_id", ""), item.get("source_name", ""), url, item.get("notice_id", ""),
            title[:300], notice_type, award_date, purchaser, normalize_name(purchaser),
            hospital_id, city_code, amount, chash, text[:20000],
            json.dumps(matched, ensure_ascii=False), score, fetch_status, now(), now(),
            1 + len(alt_sources), json.dumps(alt_sources, ensure_ascii=False), project_no, agency, extract_meta,
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

    def _insert_award(package_no: str, supplier: str, pkg_amount, pkg_raw, extra=None) -> None:
        add_award(conn, ann_id, package_no, supplier, pkg_amount, pkg_raw, award_date, full_text,
                  amount_info=extra or amount_info)

    if packages:
        for idx, pkg in enumerate(packages):
            _insert_award(
                pkg["package_no"] or str(idx + 1), pkg["supplier"], pkg["amount_yuan"], pkg["amount_raw"],
                {"amount_cents": pkg.get("amount_cents"), "amount_kind": pkg.get("amount_kind"),
                 "tax_included": pkg.get("tax_included"), "amount_basis": pkg.get("amount_basis")},
            )
    elif suppliers:
        for idx, supplier in enumerate(suppliers):
            _insert_award("1" if idx == 0 else str(idx + 1), supplier,
                          amount if idx == 0 else None, amount_raw if idx == 0 else "")
    else:
        conn.execute(
            "INSERT INTO awards(announcement_id, package_no, supplier_raw, supplier_norm, brand_name, brand_confidence, "
            "amount_yuan, amount_raw, award_date, is_winner, brand_basis, review_status, "
            "amount_cents, amount_kind, tax_included, amount_basis, missing_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
            (ann_id, "1", "", "", "未识别", 0.0, amount, amount_raw, award_date,
             "未提取到中标供应商", "pending", amount_info.get("amount_cents"),
             amount_info.get("amount_kind"), amount_info.get("tax_included"),
             amount_info.get("amount_basis"), "未提取到中标供应商"),
        )

    if detail and detail.get("attachments"):
        _save_attachments(conn, fetcher, ann_id, detail["attachments"], logger)

    return "inserted", ann_id


def _record_source_failure(conn, sid: str, errors: list[dict], stats: dict, logger) -> dict:
    kind = source_state.pick_kind([e["kind"] for e in errors])
    info = source_state.record_failure(conn, sid, kind, errors[0]["message"] if errors else "")
    stats["failures"].append(info)
    logger(
        f"[失败] {sid} 归因 {source_state.kind_label(kind)}：{info['message'] or '无详情'}；"
        f"冷却至 {info['cooldown_until']}（连续失败 {info['fail_count']} 次）",
        "warning",
    )
    return info


def _trace_start(conn, task_id: int, source: str, keyword: str, group: str) -> int:
    cur = conn.execute(
        "INSERT INTO query_trace(task_id, keyword, source, status, created_at) VALUES (?,?,?,?,?)",
        (task_id, keyword or "", source, "running", now()),
    )
    return cur.lastrowid


def _trace_finish(conn, trace_id: int, status: str, returned: int = 0, elapsed_ms: int = 0,
                  error_kind: str = "", error: str = "") -> None:
    conn.execute(
        "UPDATE query_trace SET status=?, returned=?, elapsed_ms=?, error_kind=?, error=? WHERE id=?",
        (status, returned, elapsed_ms, error_kind, (error or "")[:300], trace_id),
    )


def _collect_one_source(conn, fetcher, collectors, sid: str, queries, since: str, resume: bool,
                        task_id: int, stats: dict, logger) -> tuple[list[dict], list[dict], bool]:
    """执行一个源的全部查询，返回 (候选, 查询追踪, 是否失败)。

    单个源内部的异常由调用方兜住并转成冷却，不影响同一查询计划里的其他源。
    """
    candidates: list[dict] = []
    traces: list[dict] = []
    failed = False

    def start(keyword: str, group: str) -> int:
        trace_id = _trace_start(conn, task_id, sid, keyword, group)
        traces.append({"id": trace_id, "source": sid, "keyword": keyword or ""})
        return trace_id

    if sid == "hunan_ccgp":
        for q in queries:
            if STOP_EVENT.is_set():
                break
            scope_key = f"kw:{q.keyword}"
            start_page = 1
            if resume:
                cursor = source_state.get_cursor(conn, sid, scope_key)
                start_page = int(cursor.get("page") or 1)
                if start_page > q.pages:
                    source_state.clear_cursor(conn, sid, scope_key)
                    stats["resumed_skipped"] += 1
                    continue
                if start_page > 1:
                    stats["resumed"] += 1
            trace_id = start(q.keyword, q.group)
            fetcher.begin_stage()
            t0 = time.perf_counter()
            try:
                items = collectors[sid].search(
                    q.keyword, max_pages=q.pages, since=since, start_page=start_page,
                    on_page=lambda page, key=scope_key: source_state.save_cursor(
                        conn, sid, key, {"page": page + 1}),
                )
            except Exception as exc:  # noqa: BLE001
                items = []
                fetcher.record_error(source_state.classify_exception(exc), str(exc))
            elapsed = int((time.perf_counter() - t0) * 1000)
            errors = fetcher.take_stage_errors()
            hard = any(e["kind"] in ("blocked", "robots", "rate_limited") for e in errors)
            for it in items:
                it["query"] = q.keyword
                it["query_group"] = q.group
                it.setdefault("source_id", sid)
            candidates.extend(items)
            stats["pages"] += q.pages
            if items:
                logger(f"[检索] {sid} · {q.keyword} · {len(items)} 条"
                       + (f"（自第 {start_page} 页续采）" if start_page > 1 else "")
                       + f" · {elapsed}ms")
            if errors and (not items or hard):
                if STOP_EVENT.is_set():
                    _trace_finish(conn, trace_id, "stopped", len(items), elapsed,
                                  source_state.pick_kind([e["kind"] for e in errors]),
                                  errors[0]["message"])
                    break
                _trace_finish(conn, trace_id, "failed", len(items), elapsed,
                              source_state.pick_kind([e["kind"] for e in errors]), errors[0]["message"])
                _record_source_failure(conn, sid, errors, stats, logger)
                failed = True
                break
            _trace_finish(
                conn, trace_id, "partial" if errors else "ok", len(items), elapsed,
                source_state.pick_kind([e["kind"] for e in errors]) if errors else "",
                errors[0]["message"] if errors else "",
            )
            if errors:
                logger(f"[警告] {sid} · {q.keyword} 部分请求失败：{errors[0]['message']}", "warning")
            if not STOP_EVENT.is_set():
                source_state.clear_cursor(conn, sid, scope_key)
        return candidates, traces, failed

    if sid == "hunan_gov_search":
        scope_key = "scopes:" + ",".join(q.keyword for q in queries)
        start_index = 0
        if resume:
            cursor = source_state.get_cursor(conn, sid, scope_key)
            start_index = int(cursor.get("scope_index") or 0)
            if start_index:
                stats["resumed"] += 1
        keyword = ",".join(q.keyword for q in queries)
        trace_id = start(keyword, "scope")
        fetcher.begin_stage()
        t0 = time.perf_counter()
        try:
            items = collectors[sid].collect(
                [q.keyword for q in queries], max_pages=queries[0].pages,
                start_scope_index=start_index,
                on_scope=lambda idx, scope, key=scope_key: source_state.save_cursor(
                    conn, sid, key, {"scope_index": idx + 1}),
            )
        except Exception as exc:  # noqa: BLE001
            items = []
            fetcher.record_error(source_state.classify_exception(exc), str(exc))
        elapsed = int((time.perf_counter() - t0) * 1000)
        errors = fetcher.take_stage_errors()
        hard = any(e["kind"] in ("blocked", "robots", "rate_limited") for e in errors)
        filtered = [
            it for it in items
            if any(h in it["title"] for h in ("医院", "卫生院", "妇幼"))
            and is_award_notice(it.get("notice_type", ""), it["title"])
        ]
        for it in filtered:
            it["source_id"] = sid
            it["query"] = keyword
        candidates.extend(filtered)
        stats["pages"] += len(queries)
        if errors and (not items or hard):
            if STOP_EVENT.is_set():
                _trace_finish(conn, trace_id, "stopped", len(items), elapsed,
                              source_state.pick_kind([e["kind"] for e in errors]), errors[0]["message"])
            else:
                _trace_finish(conn, trace_id, "failed", len(items), elapsed,
                              source_state.pick_kind([e["kind"] for e in errors]), errors[0]["message"])
                _record_source_failure(conn, sid, errors, stats, logger)
                failed = True
        elif STOP_EVENT.is_set():
            _trace_finish(conn, trace_id, "stopped", len(items), elapsed)
        else:
            if errors:
                logger(f"[警告] {sid} 部分请求失败：{errors[0]['message']}", "warning")
            source_state.clear_cursor(conn, sid, scope_key)
            _trace_finish(conn, trace_id, "partial" if errors else "ok", len(items), elapsed,
                          source_state.pick_kind([e["kind"] for e in errors]) if errors else "",
                          errors[0]["message"] if errors else "")
        return candidates, traces, failed

    if sid == "huaihua_ggzy":
        scope_key = "list"
        start_page = 1
        if resume:
            cursor = source_state.get_cursor(conn, sid, scope_key)
            start_page = int(cursor.get("page") or 1)
            if start_page > 1:
                stats["resumed"] += 1
        trace_id = start("", "list")
        fetcher.begin_stage()
        t0 = time.perf_counter()
        try:
            items = collectors[sid].collect(
                since=since, max_pages=queries[0].pages, start_page=start_page,
                on_page=lambda page, key=scope_key: source_state.save_cursor(
                    conn, sid, key, {"page": page + 1}),
            )
        except Exception as exc:  # noqa: BLE001
            items = []
            fetcher.record_error(source_state.classify_exception(exc), str(exc))
        elapsed = int((time.perf_counter() - t0) * 1000)
        errors = fetcher.take_stage_errors()
        hard = any(e["kind"] in ("blocked", "robots", "rate_limited") for e in errors)
        filtered = [
            it for it in items
            if any(h in it["title"] for h in ("医院", "卫生院", "妇幼"))
            and any(h.lower() in it["title"].lower() for h in TITLE_HINTS)
        ]
        for it in filtered:
            it["source_id"] = sid
            it["query"] = ""
        candidates.extend(filtered)
        stats["pages"] += len(queries)
        if errors and (not items or hard):
            if STOP_EVENT.is_set():
                _trace_finish(conn, trace_id, "stopped", len(items), elapsed,
                              source_state.pick_kind([e["kind"] for e in errors]), errors[0]["message"])
            else:
                _trace_finish(conn, trace_id, "failed", len(items), elapsed,
                              source_state.pick_kind([e["kind"] for e in errors]), errors[0]["message"])
                _record_source_failure(conn, sid, errors, stats, logger)
                failed = True
        elif STOP_EVENT.is_set():
            _trace_finish(conn, trace_id, "stopped", len(items), elapsed)
        else:
            if errors:
                logger(f"[警告] {sid} 部分请求失败：{errors[0]['message']}", "warning")
            source_state.clear_cursor(conn, sid, scope_key)
            _trace_finish(conn, trace_id, "partial" if errors else "ok", len(items), elapsed,
                          source_state.pick_kind([e["kind"] for e in errors]) if errors else "",
                          errors[0]["message"] if errors else "")
        return candidates, traces, failed

    return candidates, traces, failed


def _run_source_worker(task_id: int, sid: str, queries, since: str, resume: bool) -> dict:
    """独立线程执行一个源：独立连接与 Fetcher，避免共享 SQLite 连接与限速状态。"""
    with db() as conn:
        logger = lambda msg, level="info": log(conn, task_id, msg, level)
        fetcher = Fetcher()
        collectors = build_collectors(fetcher, logger)
        stats = {"pages": 0, "resumed": 0, "resumed_skipped": 0, "failures": []}
        items, traces, failed = _collect_one_source(
            conn, fetcher, collectors, sid, queries, since, resume, task_id, stats, logger)
        if not failed and not STOP_EVENT.is_set():
            source_state.record_success(conn, sid)
        conn.commit()
    return {"source": sid, "items": items, "traces": traces, "stats": stats, "failed": failed}


def _apply_drop_sources(conn, dropped: list[dict]) -> None:
    """库内已存在同一公告时，把本次命中来源补到来源列表。"""
    for d in dropped:
        if d.get("reason") != "db_cross_source" or not d.get("existing_id"):
            continue
        src = d.get("source") or search_plan.source_of(d["item"])
        if src:
            _merge_ann_sources(conn, int(d["existing_id"]), src, [src])


def _update_trace_counts(conn, traces: list[dict], candidates: list[dict], dropped: list[dict]) -> None:
    """去重后回填每个查询的 kept/dropped，诊断页可看到各源被去重掉多少。"""
    kept: dict[tuple[str, str], int] = {}
    drop: dict[tuple[str, str], int] = {}
    for it in candidates:
        key = (search_plan.source_of(it), it.get("query") or "")
        kept[key] = kept.get(key, 0) + 1
    for d in dropped:
        it = d["item"]
        key = (d.get("source") or search_plan.source_of(it), it.get("query") or "")
        drop[key] = drop.get(key, 0) + 1
    for tr in traces:
        key = (tr["source"], tr["keyword"])
        conn.execute(
            "UPDATE query_trace SET kept=?, dropped=? WHERE id=?",
            (kept.get(key, 0), drop.get(key, 0), tr["id"]),
        )


def _query_stats(conn, task_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT keyword, source, status, returned, kept, dropped, elapsed_ms, error_kind, error "
        "FROM query_trace WHERE task_id=? ORDER BY id", (task_id,)
    ).fetchall()
    groups: dict[str, dict] = {}
    for row in rows:
        key = row["keyword"] or "（栏目翻页）"
        group = groups.setdefault(key, {
            "keyword": key, "returned": 0, "kept": 0, "dropped": 0, "elapsed_ms": 0, "sources": [],
        })
        group["returned"] += row["returned"] or 0
        group["kept"] += row["kept"] or 0
        group["dropped"] += row["dropped"] or 0
        group["elapsed_ms"] += row["elapsed_ms"] or 0
        group["sources"].append(dict(row))
    return list(groups.values())


def run_collection(sources: list[str] | None = None, keywords: list[str] | None = None,
                   since: str | None = None, max_pages: int | None = None,
                   resume: bool = False, actor: str = "system") -> dict:
    """执行一次采集任务，返回统计。

    同一查询计划按源并行执行，每个源独立连接/限速/冷却；单源失败不影响其他源。
    resume=True 时按 crawl_cursor 从上次中断的页/站点群继续；默认仍从第一页开始。
    """
    since = since or config.COLLECT_SINCE
    sources = sources or ["hunan_ccgp", "hunan_gov_search", "huaihua_ggzy"]
    STOP_EVENT.clear()
    PAUSE_EVENT.clear()
    fetcher = Fetcher()
    stats = {"items": 0, "inserted": 0, "updated": 0, "skipped": 0, "low_relevance": 0, "pages": 0,
             "queries": 0, "duplicates": 0, "failures": [], "cooldown_skipped": [],
             "resumed": 0, "resumed_skipped": 0, "query_stats": []}
    plan = search_plan.build_plan(sources, keywords, max_pages)
    per_source = {
        sid: [q for q in plan if q.source == sid]
        for sid in dict.fromkeys(q.source for q in plan)
    }

    with db() as conn:
        task_id = _task_create(
            conn, "+".join(sources),
            {"keywords": keywords or "default", "since": since, "queries": len(plan), "resume": resume},
        )
        logger = lambda msg, level="info": log(conn, task_id, msg, level)
        collectors = build_collectors(fetcher, logger)

        try:
            candidates: list[dict] = []
            traces: list[dict] = []
            runnable: list[tuple[str, list]] = []
            for sid, queries in per_source.items():
                if STOP_EVENT.is_set():
                    break
                cooling, reason = source_state.is_cooling(conn, sid)
                if cooling:
                    stats["cooldown_skipped"].append({"source": sid, "reason": reason})
                    logger(f"[冷却] 跳过 {sid}：{reason}", "warning")
                    trace_id = _trace_start(conn, task_id, sid, "", "cooldown")
                    _trace_finish(conn, trace_id, "cooldown_skipped", 0, 0, "cooldown", reason)
                    continue
                runnable.append((sid, queries))
            conn.commit()

            if runnable:
                with ThreadPoolExecutor(max_workers=min(3, len(runnable))) as pool:
                    futures = {
                        pool.submit(_run_source_worker, task_id, sid, queries, since, resume): sid
                        for sid, queries in runnable
                    }
                    for fut in as_completed(futures):
                        sid = futures[fut]
                        try:
                            result = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            kind = source_state.classify_exception(exc)
                            info = source_state.record_failure(conn, sid, kind, str(exc))
                            stats["failures"].append(info)
                            logger(f"[失败] {sid} 线程异常：{exc}；其他源继续", "warning")
                            continue
                        candidates.extend(result["items"])
                        traces.extend(result["traces"])
                        for key in ("pages", "resumed", "resumed_skipped"):
                            stats[key] += result["stats"].get(key, 0)
                        stats["failures"].extend(result["stats"].get("failures", []))

            stats["queries"] = len(plan)
            stats["items"] = len(candidates)
            candidates, dropped = search_plan.dedupe(candidates, conn)
            stats["duplicates"] = len(dropped)
            _apply_drop_sources(conn, dropped)
            _update_trace_counts(conn, traces, candidates, dropped)
            logger(f"查询 {len(plan)} 个（{len(per_source)} 个源并行），候选 {stats['items']} 条；"
                   f"去重丢弃 {len(dropped)} 条，保留 {len(candidates)} 条按相关度解析")

            for item in candidates:
                if STOP_EVENT.is_set():
                    break
                try:
                    if item.get("source_id") == "hunan_ccgp" and "detail_text" not in item:
                        item = collectors["hunan_ccgp"].enrich(item)
                        if not item.get("hospital_in_text"):
                            stats["skipped"] += 1
                            continue
                    status, _ = ingest_item(conn, fetcher, item, logger, since)
                    stats[status] = stats.get(status, 0) + 1
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    logger(f"入库失败 {item.get('url')}: {exc}", "error")

            stats["query_stats"] = _query_stats(conn, task_id)
            _task_finish(conn, task_id, "stopped" if STOP_EVENT.is_set() else "done",
                         stats["pages"], stats["items"], stats["inserted"], stats["updated"])
            audit(conn, actor, "collect", json.dumps(stats, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger(f"任务异常终止: {exc}", "error")
            _task_finish(conn, task_id, "failed", error=str(exc))
            raise
    stats["task_id"] = task_id
    return stats


def reparse(source: str = "hunan_ccgp", limit: int = 0, logger=print) -> dict:
    """用结构化抽取回补已入库公告：只补空缺金额/包件，候选人多包只保留第一名。"""
    fetcher = Fetcher()
    collectors = build_collectors(fetcher, logger)
    collector = collectors.get(source)
    if not collector or not hasattr(collector, "_detail_data"):
        raise ValueError(f"源 {source} 不支持结构化回补")

    stats = {"checked": 0, "amount_filled": 0, "award_amount_filled": 0,
             "packages_added": 0, "stale_removed": 0, "unreachable": 0}
    sql = ("SELECT id, url, title, publish_date, budget_yuan FROM announcements "
           "WHERE source_site=? ORDER BY id")
    if limit:
        sql += f" LIMIT {int(limit)}"

    with db() as conn:
        for row in conn.execute(sql, (source,)).fetchall():
            stats["checked"] += 1
            m = re.search(r"articleId=([0-9a-zA-Z-]+)", row["url"] or "")
            if not m:
                continue
            data = collector._detail_data(m.group(1))
            if not data["html"]:
                stats["unreachable"] += 1
                continue
            info = page_extract.parse_award_html(data["html"], row["title"], data["text"])

            if info["amount_yuan"] and not row["budget_yuan"]:
                conn.execute("UPDATE announcements SET budget_yuan=? WHERE id=?",
                             (info["amount_yuan"], row["id"]))
                conn.execute(
                    "INSERT INTO change_history(entity, entity_id, field, old_value, new_value, changed_at, actor) "
                    "VALUES (?,?,?,?,?,?,?)",
                    ("announcement", row["id"], "budget_yuan", "", str(info["amount_yuan"]), now(), "reparse"),
                )
                stats["amount_filled"] += 1

            awards = conn.execute(
                "SELECT id, supplier_raw, amount_yuan, review_status FROM awards "
                "WHERE announcement_id=? AND is_winner=1 ORDER BY id", (row["id"],),
            ).fetchall()
            if not info["packages"]:
                conn.commit()
                continue

            wanted = {normalize_company(p["supplier"]) for p in info["packages"]}
            if len(awards) > len(info["packages"]):
                for a in awards:
                    if normalize_company(a["supplier_raw"]) in wanted or a["review_status"] == "confirmed":
                        continue
                    conn.execute("DELETE FROM awards WHERE id=?", (a["id"],))
                    conn.execute(
                        "INSERT INTO change_history(entity, entity_id, field, old_value, new_value, changed_at, actor) "
                        "VALUES (?,?,?,?,?,?,?)",
                        ("award", a["id"], "award_removed", a["supplier_raw"], "未进入第一中标候选人", now(), "reparse"),
                    )
                    stats["stale_removed"] += 1
                awards = [a for a in awards if normalize_company(a["supplier_raw"]) in wanted]

            if not awards:
                for idx, pkg in enumerate(info["packages"]):
                    add_award(conn, row["id"], pkg["package_no"] or str(idx + 1), pkg["supplier"],
                              pkg["amount_yuan"], pkg["amount_raw"], row["publish_date"] or "", data["text"])
                    stats["packages_added"] += 1
            else:
                for pkg in info["packages"]:
                    match = next((a for a in awards
                                  if normalize_company(a["supplier_raw"]) == normalize_company(pkg["supplier"])), None)
                    if match and not match["amount_yuan"] and pkg["amount_yuan"]:
                        conn.execute("UPDATE awards SET amount_yuan=?, amount_raw=? WHERE id=?",
                                     (pkg["amount_yuan"], pkg["amount_raw"], match["id"]))
                        conn.execute(
                            "INSERT INTO change_history(entity, entity_id, field, old_value, new_value, changed_at, actor) "
                            "VALUES (?,?,?,?,?,?,?)",
                            ("award", match["id"], "amount_yuan", "", str(pkg["amount_yuan"]), now(), "reparse"),
                        )
                        stats["award_amount_filled"] += 1
            conn.commit()

        audit(conn, "system", "reparse", json.dumps(stats, ensure_ascii=False))
    return stats


def fill_attachments(source: str = "hunan_ccgp", limit: int = 0, download: bool = True,
                     logger=print) -> dict:
    """回补附件：从公告正文链接、交易中心包件/附件页补齐，非直链只登记不下载。"""
    fetcher = Fetcher()
    collectors = build_collectors(fetcher, logger)
    collector = collectors.get(source)
    STOP_EVENT.clear()
    stats = {"checked": 0, "with_links": 0, "found": 0, "added": 0,
             "link_pages": 0, "unreachable": 0, "stopped": ""}
    sql = "SELECT id, url, title, source_site FROM announcements WHERE source_site=? ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"

    with db() as conn:
        cooling, reason = source_state.is_cooling(conn, source)
        if cooling:
            stats["stopped"] = reason
            logger(f"[冷却] {source} 暂不采附件：{reason}")
            return stats
        for row in conn.execute(sql, (source,)).fetchall():
            if STOP_EVENT.is_set():
                break
            stats["checked"] += 1
            fetcher.begin_stage()
            attachments: list[dict] = []
            if source == "hunan_ccgp":
                m = re.search(r"articleId=([0-9a-zA-Z-]+)", row["url"] or "")
                if not m:
                    continue
                data = collector._detail_data(m.group(1))
                attachments = data["attachments"]
                if not data["html"]:
                    stats["unreachable"] += 1
            else:
                resp = fetcher.get(row["url"], retries=1)
                if resp is None or resp.status_code != 200:
                    stats["unreachable"] += 1
                else:
                    attachments = parse_detail(resp.text, row["url"])["attachments"]

            errors = fetcher.take_stage_errors()
            hard = source_state.pick_kind([e["kind"] for e in errors]) if errors else ""
            if hard in ("blocked", "rate_limited"):
                source_state.record_failure(conn, source, hard, errors[0]["message"])
                stats["stopped"] = f"{source_state.kind_label(hard)}：{errors[0]['message']}"
                logger(f"[失败] {source} 附件回补中止：{stats['stopped']}", "warning")
                break

            if attachments:
                stats["with_links"] += 1
                stats["found"] += len(attachments)
                stats["link_pages"] += sum(1 for a in attachments if not a.get("direct", True))
            stats["added"] += _save_attachments(conn, fetcher, row["id"], attachments, logger, download)
            # 附件下载失败已逐条标记，不参与源级冷却判断
            fetcher.take_stage_errors()
            conn.commit()
        audit(conn, "system", "fill_attachments", json.dumps(stats, ensure_ascii=False))
    return stats


def refresh_health() -> list[dict]:
    fetcher = Fetcher()
    results = health_check(fetcher)
    results.append(agent_reach.status_row())
    with db() as conn:
        for r in results:
            conn.execute(
                "INSERT INTO source_health(source, last_check, status, http_code, note) VALUES (?,?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET last_check=excluded.last_check, status=excluded.status, "
                "http_code=excluded.http_code, note=excluded.note",
                (r["source"], now(), r["status"], r["http_code"], r["note"]),
            )
    return results
