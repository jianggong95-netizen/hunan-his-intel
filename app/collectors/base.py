"""采集基类：限速、重试、robots、可暂停、日志。"""
from __future__ import annotations

import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

from .. import config
from ..normalize import clean_text

PAUSE_EVENT = threading.Event()  # set 表示暂停
STOP_EVENT = threading.Event()


class Fetcher:
    def __init__(self, logger=None):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self.logger = logger or (lambda msg, level="info": None)
        self._last_request = 0.0
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _wait(self):
        elapsed = time.time() - self._last_request
        if elapsed < config.REQUEST_INTERVAL_SEC:
            time.sleep(config.REQUEST_INTERVAL_SEC - elapsed)
        while PAUSE_EVENT.is_set() and not STOP_EVENT.is_set():
            time.sleep(1)
        self._last_request = time.time()

    def robots_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"{host}/robots.txt")
            try:
                rp.read()
            except Exception:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        if rp is None:
            return True
        try:
            return rp.can_fetch(config.USER_AGENT, url)
        except Exception:
            return True

    def get(self, url: str, params: dict | None = None, method: str = "GET",
            timeout: int | None = None, retries: int = 2, **kwargs) -> requests.Response | None:
        if STOP_EVENT.is_set():
            return None
        if not self.robots_allowed(url):
            self.logger(f"robots 禁止抓取: {url}", "warning")
            return None
        for attempt in range(retries + 1):
            self._wait()
            if STOP_EVENT.is_set():
                return None
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    timeout=timeout or config.REQUEST_TIMEOUT_SEC,
                    **kwargs,
                )
                resp.encoding = resp.apparent_encoding if not resp.encoding or resp.encoding == "ISO-8859-1" else resp.encoding
                return resp
            except Exception as exc:  # noqa: BLE001
                self.logger(f"请求失败({attempt + 1}/{retries + 1}) {url}: {exc}", "warning")
                time.sleep(2 * (attempt + 1))
        return None

    def download(self, url: str, dest) -> tuple[bool, str, int]:
        """下载附件，返回 (成功, sha256, 大小)。"""
        resp = self.get(url, retries=1, stream=True)
        if resp is None or resp.status_code != 200:
            return False, "", 0
        import hashlib
        from pathlib import Path

        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        sha = hashlib.sha256()
        size = 0
        try:
            with path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
                        sha.update(chunk)
                        size += len(chunk)
            return True, sha.hexdigest(), size
        except Exception as exc:  # noqa: BLE001
            self.logger(f"附件下载失败 {url}: {exc}", "warning")
            return False, "", 0


def text_of(node) -> str:
    return clean_text(node.get_text(" ", strip=True)) if node else ""
