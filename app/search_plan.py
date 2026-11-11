"""多源检索编排：查询计划、跨源去重、相关性排序。

思路借鉴 Agent-Reach（MIT）的「动作由有序后端承担、失败分类后冷却」，不复制其代码；
这里面向政府采购公告重写：按站点能力把关键词展开成查询计划，候选先做跨源去重与
相关性排序，再逐条解析入库，避免同一公告被多个检索入口重复抓取。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import config
from .normalize import his_relevance, is_award_notice, is_void, normalize_name, parse_date

HOSPITAL_HINTS = ("医院", "卫生院", "妇幼", "卫生服务", "医疗集团", "疾控")

# 站点能力：keyword=关键词检索、scope=站群分站检索、list=栏目翻页
SOURCE_CAPS = {
    "hunan_ccgp": {"mode": "keyword", "max_queries": 12, "max_pages": 3},
    "hunan_gov_search": {"mode": "scope", "max_queries": 3, "max_pages": 3},
    "huaihua_ggzy": {"mode": "list", "max_queries": 0, "max_pages": 40},
}

# 默认查询组：核心 HIS 词 + 院内业务系统词（PACS/LIS/互联网医院等平时搜不到）
CORE_QUERIES = ("医院信息", "信息系统", "电子病历", "HIS")
DOMAIN_QUERIES = ("PACS", "LIS", "互联网医院", "智慧医院", "医保移动支付", "银医")

# 标题命中即视为 HIS 相关（入库仍以正文相关度 >= 3 为准）
TITLE_HINTS = (
    "HIS", "医院信息系统", "医院信息管理系统", "医院信息", "信息系统", "信息平台", "信息化",
    "智慧医院", "智慧医疗", "电子病历", "互联互通", "数据中心", "云平台", "系统建设",
    "系统升级", "系统维保", "PACS", "LIS", "互联网医院", "医保移动支付", "银医",
    "预约挂号", "排队叫号", "自助服务", "集成平台", "软件", "信息管理", "网络安全", "服务器",
)

# 标题里的空白与标点不参与跨源比对
_TITLE_NOISE = re.compile(r"[\s\u3000（）()【】\[\]“”\"'、,，。.;：:\-—_/]+")


@dataclass(frozen=True)
class Query:
    source: str
    keyword: str
    group: str
    pages: int


def build_plan(sources: list[str] | None = None, keywords: list[str] | None = None,
               max_pages: int | None = None) -> list[Query]:
    """按站点能力把关键词展开为查询计划；自定义关键词时不追加默认扩展词。"""
    sources = sources or ["hunan_ccgp", "hunan_gov_search", "huaihua_ggzy"]
    max_pages = max_pages or config.COLLECT_MAX_PAGES
    plan: list[Query] = []

    if keywords:
        queries = [(k, "custom") for k in keywords if k]
    else:
        queries = [(k, "core") for k in CORE_QUERIES] + [(k, "domain") for k in DOMAIN_QUERIES]

    for sid in sources:
        caps = SOURCE_CAPS.get(sid)
        if not caps:
            continue
        if caps["mode"] == "keyword":
            seen = set()
            picked = 0
            for kw, group in queries:
                key = kw.lower()
                if key in seen or picked >= caps["max_queries"]:
                    continue
                seen.add(key)
                picked += 1
                plan.append(Query(sid, kw, group, max(1, min(max_pages, caps["max_pages"]))))
        elif caps["mode"] == "scope":
            for kw, group in queries[: caps["max_queries"]]:
                plan.append(Query(sid, kw, group, max(1, min(max_pages, caps["max_pages"]))))
        else:
            pages = max(1, min(max_pages * 6, caps["max_pages"]))
            plan.append(Query(sid, "", "list", pages))
    return plan


def title_key(title: str) -> str:
    return _TITLE_NOISE.sub("", re.sub(r"<[^>]+>", "", title or "")).lower()


def fingerprint(item: dict) -> str:
    """跨源同一公告的指纹：标题归一 + 发布日期 + 采购单位。"""
    title = title_key(item.get("title"))
    date = parse_date(item.get("publish_date") or "") or ""
    purchaser = normalize_name(item.get("purchaser") or "")
    return f"{title}|{date}|{purchaser}"


def is_hospital_title(title: str) -> bool:
    return any(h in (title or "") for h in HOSPITAL_HINTS)


def is_his_title(title: str) -> bool:
    return any(k.lower() in (title or "").lower() for k in TITLE_HINTS)


def score_breakdown(item: dict) -> dict:
    """相关性打分拆解：HIS/信息系统词 + 医院属性 + 中标语义，标题权重高于正文。

    标题命中 HIS 词 3 分/个、正文 1 分/个（沿用 his_relevance）；医院词标题 1.5、
    正文 0.5；中标语义标题 1、正文 0.5；作废公告扣 5 分。
    """
    title = item.get("title", "")
    body = item.get("snippet") or item.get("detail_text") or ""
    score, matched = his_relevance(title, body)
    title_hits = [kw for kw in matched if _hit_text(kw, title)]
    body_hits = [kw for kw in matched if kw not in title_hits]
    hospital = (1.5 if is_hospital_title(title) else 0.0) + (
        0.5 if (not is_hospital_title(title) and any(h in body for h in HOSPITAL_HINTS)) else 0.0
    )
    award = 0.0
    if is_award_notice(item.get("notice_type", ""), title):
        award = 1.0
    elif any(w in body for w in ("中标", "成交", "结果公告")):
        award = 0.5
    penalty = -5.0 if is_void(title) else 0.0
    return {
        "title_hits": title_hits,
        "body_hits": body_hits,
        "his_title": 3.0 * len(title_hits),
        "his_body": 1.0 * len(body_hits),
        "hospital": hospital,
        "award": award,
        "penalty": penalty,
        "score": score + hospital + award + penalty,
    }


def _hit_text(keyword: str, text: str) -> bool:
    if keyword.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])", (text or "").lower()) is not None
    return keyword in (text or "")


def score_candidate(item: dict) -> float:
    """HIS 相关度 + 医院/中标属性加成，用于决定解析顺序与重复项取舍。"""
    return score_breakdown(item)["score"]


def rank(candidates: list[dict]) -> list[dict]:
    for item in candidates:
        item["_score"] = score_candidate(item)
    return sorted(candidates, key=lambda it: it["_score"], reverse=True)


def known_duplicate(conn, item: dict) -> int | None:
    """库内跨源同一公告：标题 + 日期一致但来源不同，视为重复。"""
    date = parse_date(item.get("publish_date") or "")
    title = (item.get("title") or "")[:300]
    if not date or not title:
        return None
    row = conn.execute(
        "SELECT id FROM announcements WHERE title=? AND publish_date=? AND source_site<>? LIMIT 1",
        (title, date, item.get("source_id") or item.get("source_site") or ""),
    ).fetchone()
    return row["id"] if row else None


def source_of(item: dict) -> str:
    return (item.get("source_id") or item.get("source_site") or item.get("source_name") or "").strip()


def merge_alt_sources(target: dict, *items: dict) -> None:
    """把重复公告的来源挂到保留项上，供入库记录「还出现在哪些站点」。"""
    alts = list(target.get("_alt_sources") or [])
    own = source_of(target)
    for it in items:
        src = source_of(it)
        if src and src != own and src not in alts:
            alts.append(src)
    target["_alt_sources"] = alts


def dedupe(candidates: list[dict], conn=None) -> tuple[list[dict], list[dict]]:
    """按 URL 与跨源指纹去重，保留相关度更高的一条。返回 (保留, 丢弃)。

    同一公告被多个站点命中时只保留一条，并把其他来源记到保留项的 ``_alt_sources``；
    被丢弃项带上 ``source``（库内重复另带 ``existing_id``），便于更新来源数与查询统计。
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    by_url: dict[str, dict] = {}
    by_fp: dict[str, dict] = {}
    for item in rank(candidates):
        url = (item.get("url") or "").strip()
        if not url:
            dropped.append({"item": item, "reason": "no_url", "source": source_of(item)})
            continue
        if url in by_url:
            merge_alt_sources(by_url[url], item)
            dropped.append({"item": item, "reason": "same_url", "source": source_of(item)})
            continue
        fp = fingerprint(item)
        if fp in by_fp and item["_score"] <= by_fp[fp]["_score"]:
            merge_alt_sources(by_fp[fp], item)
            dropped.append({"item": item, "reason": "cross_source", "source": source_of(item)})
            continue
        existing = known_duplicate(conn, item) if conn is not None else None
        if existing:
            dropped.append({"item": item, "reason": "db_cross_source",
                            "source": source_of(item), "existing_id": existing})
            continue
        if fp in by_fp:
            previous = by_fp[fp]
            merge_alt_sources(item, previous)
            dropped.append({"item": previous, "reason": "cross_source", "source": source_of(previous)})
            kept.remove(previous)
            if by_url.get(previous.get("url", "")) is previous:
                by_url.pop(previous.get("url", ""), None)
        by_url[url] = item
        by_fp[fp] = item
        kept.append(item)
    return kept, dropped
