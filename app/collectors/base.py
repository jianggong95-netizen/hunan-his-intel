"""采集基类：限速、重试、robots、可暂停、日志。"""
from __future__ import annotations

import threading
import time
import urllib.robotparser
from urllib.parse import urlparse

import requests

from .. import config, source_state
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
        self.stage_errors: list[dict] = []

    def begin_stage(self) -> None:
        """开始一个采集阶段（一个源的一次检索/翻页），清空上一阶段的错误。"""
        self.stage_errors = []

    def take_stage_errors(self) -> list[dict]:
        errors, self.stage_errors = self.stage_errors, []
        return errors

    def record_error(self, kind: str, message: str) -> None:
        self.stage_errors.append({"kind": kind, "message": (message or "")[:300]})


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
            self.record_error("robots", url)
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
                self._note_response(resp, url, kwargs.get("stream", False))
                return resp
            except Exception as exc:  # noqa: BLE001
                kind = source_state.classify_exception(exc)
                self.logger(f"请求失败({attempt + 1}/{retries + 1}) {url}: {exc}", "warning")
                self.record_error(kind, f"{url} {exc}")
                time.sleep(2 * (attempt + 1))
        return None

    def _note_response(self, resp: requests.Response, url: str, stream: bool) -> None:
        """把 HTTP 错误与疑似风控页登记为阶段错误；不改变返回行为，由编排层决定冷却。"""
        if resp.status_code >= 400:
            self.record_error(source_state.classify_status(resp.status_code),
                              f"{url} HTTP {resp.status_code}")
            return
        if stream or not resp.headers.get("Content-Type", "").lower().startswith(("text/html", "application/xhtml")):
            return
        try:
            kind = source_state.classify_body(resp.text[:4000])
        except Exception:  # noqa: BLE001
            return
        if kind:
            self.record_error(kind, f"{url} 疑似拦截页")


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
