"""湖南省政府采购网（政采云）采集器：全局检索 JSON + 详情 JSON。"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..normalize import clean_text, is_void, parse_date
from ..regions import detect_city
from ..search_plan import HOSPITAL_HINTS, TITLE_HINTS
from .base import Fetcher
from .detail import attachment_links

SEARCH_API = "http://www.ccgp-hunan.gov.cn/portal/all"
DETAIL_API = "http://www.ccgp-hunan.gov.cn/portal/detail"
FRONT_DETAIL = "http://www.ccgp-hunan.gov.cn/luban/front/detail"
SITE_BASE = "http://www.ccgp-hunan.gov.cn"


class HunanCcgpCollector:
    id = "hunan_ccgp"
    name = "湖南省政府采购网"

    def __init__(self, fetcher: Fetcher, logger):
        self.fetcher = fetcher
        self.logger = logger

    def _search_page(self, keyword: str, page_no: int) -> list[dict]:
        resp = self.fetcher.get(
            SEARCH_API, method="POST", json={"keyword": keyword, "pageNo": page_no, "pageSize": 30}, retries=1
        )
        if resp is None or resp.status_code != 200:
            self.logger(f"[省政采] 检索失败 {keyword} 第{page_no}页", "warning")
            return []
        try:
            data = resp.json()
            return data["result"]["data"]["data"] or []
        except Exception as exc:  # noqa: BLE001
            self.logger(f"[省政采] 检索返回解析失败 {keyword}: {exc}", "warning")
            self.fetcher.record_error("parse", f"检索 {keyword} 返回解析失败: {exc}")
            return []

    def search(self, keyword: str, max_pages: int = 3, since: str | None = None,
               start_page: int = 1, on_page=None) -> list[dict]:
        out, seen = [], set()
        for page_no in range(max(1, start_page), max_pages + 1):
            raw_items = self._search_page(keyword, page_no)
            for raw in raw_items:
                title = re.sub(r"<[^>]+>", "", raw.get("title") or "")
                path = raw.get("pathName") or ""
                if "中标" not in path or "公告" not in path:
                    continue
                if raw.get("invalid"):
                    continue
                if not any(h in title for h in HOSPITAL_HINTS):
                    continue
                if not any(k.lower() in title.lower() for k in TITLE_HINTS):
                    continue
                if is_void(title):
                    continue
                ts = raw.get("publishDate")
                publish = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else ""
                if since and publish and publish < since:
                    continue
                aid = raw.get("articleId")
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                url = (
                    f"{FRONT_DETAIL}?categoryCode={quote(str(raw.get('firstCode') or ''))}"
                    f"&parentId={raw.get('parentId') or ''}&articleId={quote(aid)}"
                )
                out.append(
                    {
                        "title": title,
                        "url": url,
                        "notice_type": "中标公告",
                        "publish_date": publish,
                        "city_hint": detect_city(raw.get("districtName") or "") or "",
                        "district_code": raw.get("districtCode") or "",
                        "article_id": aid,
                        "source_name": self.name,
                        "source_id": self.id,
                    }
                )
            if on_page:
                on_page(page_no)
            if not raw_items:
                break
        return out

    def _detail_data(self, article_id: str) -> dict:
        resp = self.fetcher.get(DETAIL_API, params={"articleId": article_id}, retries=1)
        if resp is None or resp.status_code != 200:
            return {"text": "", "attachments": [], "html": ""}
        try:
            data = resp.json()["result"]["data"]
        except Exception:  # noqa: BLE001
            return {"text": "", "attachments": [], "html": ""}
        if not data:
            return {"text": "", "attachments": [], "html": ""}
        html = data.get("content") or ""
        soup = BeautifulSoup(html, "html.parser")
        text = "\n".join(
            line.strip() for line in soup.get_text("\n", strip=True).splitlines() if line.strip()
        )
        atts = []
        seen = set()
        for att in data.get("attachmentList") or []:
            url = att.get("url") or att.get("fileUrl") or att.get("downloadUrl") or ""
            name = att.get("fileName") or att.get("name") or att.get("originalName") or "附件"
            if url and url not in seen:
                seen.add(url)
                atts.append({"name": name[:120], "url": url, "direct": True, "source": "api"})
        # 部分公告的附件只出现在正文链接里（OSS 直链、交易中心包件/附件页）
        for link in attachment_links(html, SITE_BASE):
            if link["url"] in seen:
                continue
            seen.add(link["url"])
            link["source"] = "body_link"
            atts.append(link)
        return {"text": text, "attachments": atts, "html": html}

    def _detail(self, article_id: str) -> tuple[str, list[dict]]:
        data = self._detail_data(article_id)
        return data["text"], data["attachments"]

    def enrich(self, item: dict) -> dict:
        """补齐详情正文、附件与中标供应商；正文没有医院字样时标记供编排层过滤。"""
        data = self._detail_data(item["article_id"])
        text, atts = data["text"], data["attachments"]
        supplier = ""
        m = re.search(r"供应商名称[:：]\s*([^\s，,；;（(]{4,60})", text)
        if m:
            supplier = m.group(1)
        item.update(
            {
                "detail_text": text,
                "detail_html": data["html"],
                "detail_attachments": atts,
                "supplier_override": supplier,
                "city_hint": item.get("city_hint") or "",
                "publish_date": item.get("publish_date") or parse_date(text) or "",
                "hospital_in_text": (not text) or any(h in text for h in HOSPITAL_HINTS),
            }
        )
        self.logger(f"[省政采] {item['title'][:40]} 详情 {len(text)} 字 / 附件 {len(atts)}")
        return item

    def collect(self, keywords: list[str] | None = None, max_pages: int | None = None,
                since: str | None = None) -> list[dict]:
        from .. import config

        keywords = keywords or config.load_keywords()[:4]
        max_pages = max(1, min(max_pages or config.COLLECT_MAX_PAGES, 4))
        since = since or config.COLLECT_SINCE
        items = []
        for kw in keywords:
            for it in self.search(kw, max_pages, since):
                it = self.enrich(it)
                if not it["hospital_in_text"]:
                    continue
                items.append(it)
        return items
