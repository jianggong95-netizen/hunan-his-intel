"""怀化市公共资源交易中心：政府采购-中标公告列表 + 详情。

实测（2026-11-07）：
列表 https://ggzy.huaihua.gov.cn/ggzyjyzx/c116113/list.shtml
分页 list_2.shtml ... list_34.shtml；详情页 .shtml，正文容器 .article_box。
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import config
from ..normalize import clean_text, parse_date
from .base import Fetcher, STOP_EVENT, text_of
from .detail import parse_detail

LIST_BASE = "https://ggzy.huaihua.gov.cn/ggzyjyzx/c116113/"
CHANNELS = {
    "中标公告": "https://ggzy.huaihua.gov.cn/ggzyjyzx/c116113/list.shtml",
    "采购公告": "https://ggzy.huaihua.gov.cn/ggzyjyzx/c116112/list.shtml",
    "流标公告": "https://ggzy.huaihua.gov.cn/ggzyjyzx/c116115/list.shtml",
}


class HuaihuaCollector:
    source_id = "huaihua_ggzy"
    source_name = "怀化市公共资源交易中心"

    def __init__(self, fetcher: Fetcher, logger):
        self.fetcher = fetcher
        self.logger = logger

    def _page_url(self, base: str, page: int) -> str:
        return base if page == 1 else base.replace("/list.shtml", f"/list_{page}.shtml")

    def total_pages(self, html: str) -> int:
        m = re.search(r"createPageHTML\('paging',\s*(\d+)", html)
        return int(m.group(1)) if m else 1

    def list_items(self, page: int = 1) -> tuple[list[dict], int]:
        url = self._page_url(CHANNELS["中标公告"], page)
        resp = self.fetcher.get(url)
        if resp is None or resp.status_code != 200:
            self.logger(f"[怀化] 列表第{page}页无响应", "warning")
            return [], 1
        soup = BeautifulSoup(resp.text, "html.parser")
        items = []
        for li in soup.select(".ul_list1 li"):
            a = li.find("a", href=True)
            if not a:
                continue
            items.append(
                {
                    "url": urljoin(url, a["href"]),
                    "title": clean_text(a.get_text(strip=True)),
                    "notice_type": "中标公告",
                    "source_name": self.source_name,
                    "publish_date": parse_date(text_of(li.find("span"))),
                    "snippet": "",
                }
            )
        return items, self.total_pages(resp.text)

    def fetch_detail(self, url: str) -> dict | None:
        resp = self.fetcher.get(url)
        if resp is None or resp.status_code != 200:
            return None
        return parse_detail(resp.text, url)

    def collect(self, since: str | None = None, max_pages: int | None = None) -> list[dict]:
        since = since or config.COLLECT_SINCE
        max_pages = max_pages or 40
        out: list[dict] = []
        page = 1
        total = None
        fails = 0
        while page <= (total or max_pages):
            if STOP_EVENT.is_set():
                break
            if page > max_pages:
                break
            items, page_total = self.list_items(page)
            if total is None:
                total = page_total
            if not items:
                fails += 1
                self.logger(f"[怀化] 第{page}页无有效条目（第{fails}次）", "warning")
                if fails >= 2:
                    break
                page += 1
                continue
            fails = 0
            out.extend(items)
            oldest = min((i["publish_date"] or "9999-99-99") for i in items)
            if oldest <= since:
                break
            page += 1
        return out
