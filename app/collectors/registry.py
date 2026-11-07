"""采集源注册表与健康检查。"""
from __future__ import annotations

from .base import Fetcher
from .ccgp import check_ccgp_hunan, check_ccgp_national
from .huaihua import HuaihuaCollector
from .hunan_search import HunanGovSearchCollector

SOURCES = [
    {
        "id": "hunan_gov_search",
        "name": "湖南省政府网站站群搜索",
        "scope": "全省（省市县三级政府/交易中心站点）",
        "enabled": True,
        "note": "实测可用；按关键词只发现公告链接，详情是否可达取决于目标站点网络",
    },
    {
        "id": "huaihua_ggzy",
        "name": "怀化市公共资源交易中心",
        "scope": "怀化市",
        "enabled": True,
        "note": "实测可用（列表+详情）",
    },
    {
        "id": "ccgp_national",
        "name": "中国政府采购网",
        "scope": "全国（按湖南筛选）",
        "enabled": True,
        "note": "参数可用；本机出口 IP 被风控拦截，需境内网络运行",
    },
    {
        "id": "ccgp_hunan",
        "name": "湖南省政府采购网",
        "scope": "湖南省",
        "enabled": False,
        "note": "本机不可达；保留适配器，待境内网络验证后启用",
    },
]


def build_collectors(fetcher: Fetcher, logger):
    return {
        "hunan_gov_search": HunanGovSearchCollector(fetcher, logger),
        "huaihua_ggzy": HuaihuaCollector(fetcher, logger),
    }


def health_check(fetcher: Fetcher, logger=None) -> list[dict]:
    results = []
    for src in SOURCES:
        if src["id"] == "ccgp_national":
            status, code, note = check_ccgp_national(fetcher)
        elif src["id"] == "ccgp_hunan":
            status, code, note = check_ccgp_hunan(fetcher)
        elif src["id"] == "hunan_gov_search":
            try:
                resp = fetcher.get(
                    "https://searching.hunan.gov.cn/hunan/govall/news",
                    params={"q": "HIS", "p": 0, "searchfields": "", "sm": "", "columnCN": "", "timetype": ""},
                    retries=0,
                )
                status, code, note = (
                    ("available", resp.status_code, f"HTTP {resp.status_code}") if resp else ("unreachable", None, "无响应")
                )
            except Exception as exc:  # noqa: BLE001
                status, code, note = "error", None, str(exc)
        elif src["id"] == "huaihua_ggzy":
            try:
                resp = fetcher.get("https://ggzy.huaihua.gov.cn/ggzyjyzx/c116113/list.shtml", retries=0)
                status, code, note = (
                    ("available", resp.status_code, f"HTTP {resp.status_code}") if resp else ("unreachable", None, "无响应")
                )
            except Exception as exc:  # noqa: BLE001
                status, code, note = "error", None, str(exc)
        else:
            continue
        results.append({"source": src["id"], "name": src["name"], "status": status, "http_code": code, "note": note})
    return results
