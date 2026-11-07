"""天眼查开放平台接入位（合规：仅官方 API，不做网页抓取/不绕验证码）。

未配置 TYC_API_KEY 时模块禁用，调用返回明确的禁用提示。
"""
from __future__ import annotations

import requests

from . import config


class TycDisabled(RuntimeError):
    pass


def is_enabled() -> bool:
    return bool(config.TYC_API_KEY)


def _request(path: str, params: dict) -> dict:
    if not is_enabled():
        raise TycDisabled("天眼查模块未启用：请在 .env 配置 TYC_API_KEY（官方开放平台）。")
    resp = requests.get(
        f"{config.TYC_BASE_URL}{path}",
        params=params,
        headers={"Authorization": config.TYC_API_KEY},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"天眼查 API 返回 HTTP {resp.status_code}")
    return resp.json()


def search_company(keyword: str) -> dict:
    """企业检索（需官方 key）。"""
    return _request("/services/open/search/2.0", {"word": keyword, "pageSize": 5, "pageNum": 1})


def company_profile(name: str) -> dict:
    return _request("/services/open/ic/baseinfo/normal", {"keyword": name})
