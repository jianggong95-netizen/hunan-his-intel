"""中国政府采购网检索适配器（境内网络可用；本机实测会被“频繁访问”拦截）。

合规说明：仅按公开检索页限速采集，不绕过验证码/风控；被拦截时如实标记不可用。
"""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import config
from ..normalize import clean_text, parse_date
from .base import Fetcher, STOP_EVENT, text_of

SEARCH_URL = "https://search.ccgp.gov.cn/bxsearch"
DETAIL_HOST = "https://www.ccgp.gov.cn"


class Blocked(Exception):
    pass


class CcgpNationalCollector:
    source_id = "ccgp_national"
    source_name = "中国政府采购网"

    def __init__(self, fetcher: Fetcher, logger):
        self.fetcher = fetcher
        self.logger = logger

    def search(self, keyword: str, page: int = 1, since: str | None = None,
               until: str | None = None, zone: str = "湖南") -> list[dict]:
        since = since or config.COLLECT_SINCE
        until = until or date.today().isoformat()
        params = {
            "searchtype": 1, "page_index": page, "bidSort": 0, "buyerName": "", "projectId": "",
            "pinMu": 0, "bidType": 0, "dbselect": "bidx", "kw": keyword,
            "start_time": since.replace("-", ":"), "end_time": until.replace("-", ":"),
            "timeType": 6, "displayZone": zone, "zoneId": "", "pppStatus": 0, "agentName": "",
        }
        resp = self.fetcher.get(SEARCH_URL, params=params, headers={"Referer": "https://www.ccgp.gov.cn/"})
        if resp is None:
            raise Blocked("无响应（可能被网络策略或风控拦截）")
        if resp.status_code != 200:
            raise Blocked(f"HTTP {resp.status_code}")
        if "频繁访问" in resp.text or len(resp.text) < 3500:
            raise Blocked("命中“访问过于频繁”拦截页")
        return self._parse(resp.text)

    @staticmethod
    def _parse(html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        items = []
        for li in soup.select(".vT-srch-result-list-bid li"):
            a = li.find("a", href=True)
            if not a:
                continue
            items.append(
                {
                    "url": urljoin(DETAIL_HOST, a["href"]),
                    "title": clean_text(a.get_text(strip=True)),
                    "notice_type": "",
                    "source_name": "中国政府采购网",
                    "publish_date": parse_date(text_of(li.find("span"))),
                    "snippet": text_of(li.find("p")),
                }
            )
        return items

    def collect(self, keywords: list[str]) -> list[dict]:
        out: list[dict] = []
        seen = set()
        for kw in keywords:
            for page in range(1, config.COLLECT_MAX_PAGES + 1):
                if STOP_EVENT.is_set():
                    return out
                items = self.search(kw, page)
                if not items:
                    break
                for it in items:
                    if it["url"] not in seen:
                        seen.add(it["url"])
                        out.append(it)
        return out


def check_ccgp_national(fetcher: Fetcher) -> tuple[str, int | None, str]:
    try:
        CcgpNationalCollector(fetcher, lambda *a, **k: None).search("HIS", page=1)
        return "available", 200, "检索页正常"
    except Blocked as exc:
        return "blocked", None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return "error", None, str(exc)


def check_ccgp_hunan(fetcher: Fetcher) -> tuple[str, int | None, str]:
    try:
        resp = fetcher.get("https://www.ccgp-hunan.gov.cn/", retries=0)
        if resp is None:
            return "unreachable", None, "连接超时/被网络拦截"
        return "available", resp.status_code, f"HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return "error", None, str(exc)
