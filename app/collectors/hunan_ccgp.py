"""湖南省政府采购网（政采云）采集器：全局检索 JSON + 详情 JSON。"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..normalize import clean_text, is_void, parse_date
from ..regions import detect_city
from .base import Fetcher

SEARCH_API = "http://www.ccgp-hunan.gov.cn/portal/all"
DETAIL_API = "http://www.ccgp-hunan.gov.cn/portal/detail"
FRONT_DETAIL = "http://www.ccgp-hunan.gov.cn/luban/front/detail"

HIS_TITLE_HINTS = (
    "HIS", "医院信息系统", "医院信息管理系统", "信息系统", "信息平台", "信息化",
    "智慧医院", "电子病历", "互联互通", "数据中心", "云平台", "系统建设", "系统升级",
)
HOSPITAL_HINTS = ("医院", "卫生院", "妇幼", "卫生服务", "医疗集团", "疾控")


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
            return []

    def search(self, keyword: str, max_pages: int = 3, since: str | None = None) -> list[dict]:
        out, seen = [], set()
        for page_no in range(1, max_pages + 1):
            for raw in self._search_page(keyword, page_no):
                title = re.sub(r"<[^>]+>", "", raw.get("title") or "")
                path = raw.get("pathName") or ""
                if "中标" not in path or "公告" not in path:
                    continue
                if raw.get("invalid"):
                    continue
                if not any(h in title for h in HOSPITAL_HINTS):
                    continue
                if not any(k in title for k in HIS_TITLE_HINTS):
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
        return out

    def _detail(self, article_id: str) -> tuple[str, list[dict]]:
        resp = self.fetcher.get(DETAIL_API, params={"articleId": article_id}, retries=1)
        if resp is None or resp.status_code != 200:
            return "", []
        try:
            data = resp.json()["result"]["data"]
        except Exception:  # noqa: BLE001
            return "", []
        html = data.get("content") or ""
        soup = BeautifulSoup(html, "html.parser")
        text = "\n".join(
            line.strip() for line in soup.get_text("\n", strip=True).splitlines() if line.strip()
        )
        atts = []
        for att in data.get("attachmentList") or []:
            url = att.get("url") or att.get("fileUrl") or att.get("downloadUrl") or ""
            name = att.get("fileName") or att.get("name") or att.get("originalName") or "附件"
            if url:
                atts.append({"name": name[:120], "url": url})
        return text, atts

    def collect(self, keywords: list[str] | None = None, max_pages: int | None = None,
                since: str | None = None) -> list[dict]:
        from .. import config

        keywords = keywords or config.load_keywords()[:4]
        max_pages = max(1, min(max_pages or config.COLLECT_MAX_PAGES, 4))
        since = since or config.COLLECT_SINCE
        items = []
        for kw in keywords:
            for it in self.search(kw, max_pages, since):
                text, atts = self._detail(it["article_id"])
                if text and not any(h in text for h in HOSPITAL_HINTS):
                    continue
                supplier = ""
                m = re.search(r"供应商名称[:：]\s*([^\s，,；;（(]{4,60})", text)
                if m:
                    supplier = m.group(1)
                it.update(
                    {
                        "detail_text": text,
                        "detail_attachments": atts,
                        "supplier_override": supplier,
                        "city_hint": it.get("city_hint") or "",
                        "publish_date": it.get("publish_date") or parse_date(text),
                    }
                )
                items.append(it)
                self.logger(f"[省政采] {it['title'][:40]} 详情 {len(text)} 字 / 附件 {len(atts)}")
        return items
