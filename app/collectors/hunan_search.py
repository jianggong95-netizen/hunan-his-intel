"""湖南省政府网站站群搜索（覆盖 14 市州 + 省本级）。

实测接口（2026-11-07）：
GET https://searching.hunan.gov.cn/hunan/{scope}/news?q=医院信息系统&searchfields=title&p=0
按站点群分组检索，返回 HTML 结果列表，含标题、链接、来源站点、发布时间。
scope 取值为各市州站群代码（见 regions.CITIES），比 govall 全量检索精确得多。
"""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import config
from ..normalize import clean_text, parse_date
from ..regions import CITIES, PROVINCE_SCOPE, scope_to_short
from .base import Fetcher, STOP_EVENT, text_of

BASE = "https://searching.hunan.gov.cn/hunan/{scope}/news"


class HunanGovSearchCollector:
    source_id = "hunan_gov_search"
    source_name = "湖南省政府网站站群搜索"

    def __init__(self, fetcher: Fetcher, logger):
        self.fetcher = fetcher
        self.logger = logger

    def search(self, keyword: str, scope: str = PROVINCE_SCOPE, max_pages: int | None = None,
               field: str = "title") -> list[dict]:
        items: list[dict] = []
        max_pages = max_pages or config.COLLECT_MAX_PAGES
        city_hint = scope_to_short().get(scope)
        for page in range(max_pages):
            if STOP_EVENT.is_set():
                break
            params = {"q": keyword, "searchfields": field, "sm": "", "columnCN": "", "p": page, "timetype": ""}
            resp = self.fetcher.get(
                BASE.format(scope=scope),
                params=params,
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": BASE.format(scope=scope)},
            )
            if resp is None or resp.status_code != 200:
                self.logger(f"[站群搜索] scope={scope} {keyword} 第{page + 1}页无响应", "warning")
                break
            page_items = self._parse(resp.text, resp.url)
            if not page_items:
                break
            for it in page_items:
                it["city_hint"] = city_hint
            items.extend(page_items)
            self.logger(f"[站群搜索] scope={scope} {keyword} 第{page + 1}页 {len(page_items)} 条")
        return items

    @staticmethod
    def _parse(html: str, base_url: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for li in soup.select("#hits li"):
            a = li.select_one(".title a") or li.find("a", href=True)
            if not a or not a.get("href"):
                continue
            title = clean_text(a.get("title") or a.get_text(strip=True))
            if not title:
                continue
            href = urljoin(base_url, a["href"])
            if "searching.hunan.gov.cn" in href:
                continue
            results.append(
                {
                    "url": href,
                    "title": title,
                    "notice_type": text_of(li.select_one(".com-title-name")),
                    "source_name": text_of(li.select_one(".source-name")) or "湖南省政府站群",
                    "publish_date": parse_date(text_of(li.select_one(".source-time"))),
                    "snippet": text_of(li.select_one(".right"))[:300],
                }
            )
        return results

    def collect(self, keywords: list[str] | None = None, scopes: list[str] | None = None,
                max_pages: int | None = None) -> list[dict]:
        keywords = (keywords or config.load_keywords())[:3]
        scopes = scopes or [c["scope"] for c in CITIES] + [PROVINCE_SCOPE]
        out: list[dict] = []
        seen = set()
        pages = max(1, min(max_pages or config.COLLECT_MAX_PAGES, 3))
        for scope in scopes:
            for kw in keywords:
                if STOP_EVENT.is_set():
                    break
                for item in self.search(kw, scope=scope, max_pages=pages):
                    if item["url"] in seen:
                        continue
                    seen.add(item["url"])
                    out.append(item)
        return out
