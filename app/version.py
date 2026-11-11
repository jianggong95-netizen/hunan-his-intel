"""版本管理与更新检查：GitHub Releases 作为升级通道。

升级方式（onedir 包）：下载最新 his-intel-vX.Y.Z-win64.zip，解压覆盖旧的 his-intel 目录即可，
data/（数据库、附件、导出）不在 zip 内、不会被覆盖，配置 config/ 与 .env 会保留本地版本。
"""
from __future__ import annotations

import re
import time

import requests

from . import config

__version__ = "1.0.0"
APP_NAME = "hunan-his-intel"
REPO = "jianggong95-netizen/hunan-his-intel"
RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"

_CACHE_TTL_SEC = 600
_cache: dict = {"ts": 0.0, "data": None}


def parse_version(v: str) -> tuple[int, int, int]:
    parts = [int(x) for x in re.findall(r"\d+", v or "")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)  # type: ignore[return-value]


def latest_release(timeout: int = 10) -> dict:
    """查询 GitHub 最新 Release；失败返回 {"error": ...}，不抛异常。结果缓存 10 分钟。"""
    if _cache["data"] is not None and time.time() - _cache["ts"] < _CACHE_TTL_SEC:
        return _cache["data"]
    try:
        resp = requests.get(
            RELEASE_API, timeout=timeout,
            headers={"Accept": "application/vnd.github+json", "User-Agent": config.USER_AGENT},
        )
        if resp.status_code != 200:
            return {"error": f"GitHub API HTTP {resp.status_code}"}
        d = resp.json()
        tag = str(d.get("tag_name") or "").lstrip("vV")
        asset = next((a for a in d.get("assets", []) if str(a.get("name", "")).endswith(".zip")), None)
        data = {
            "version": tag,
            "url": (asset or d).get("browser_download_url") or d.get("html_url", ""),
            "page": d.get("html_url", ""),
            "name": d.get("name") or tag,
            "published": d.get("published_at") or "",
            "notes": (d.get("body") or "")[:600],
        }
        _cache.update({"ts": time.time(), "data": data})
        return data
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def update_info() -> dict:
    """当前版本 + 最新版本 + 是否有更新（离线时仅 current/has_update=False/error）。"""
    cur = __version__
    latest = latest_release()
    has_update = False
    if latest.get("version"):
        try:
            has_update = parse_version(latest["version"]) > parse_version(cur)
        except ValueError:
            pass
    return {"app": APP_NAME, "repo": REPO, "current": cur, "has_update": has_update, **latest}
