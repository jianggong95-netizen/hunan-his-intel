"""通用详情页解析：正文、标题、日期、附件。"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

ATTACH_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".ofd", ".txt", ".jpg", ".png")


def parse_detail(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    meta = soup.find("meta", attrs={"name": "ArticleTitle"})
    if meta and meta.get("content"):
        title = meta["content"].strip()
    if not title:
        h = soup.find(["h1", "h2"])
        title = h.get_text(strip=True) if h else (soup.title.get_text(strip=True) if soup.title else "")

    body = None
    for selector in (".article_box", "#zoom", ".TRS_Editor", ".article-content", ".content", "#content"):
        node = soup.select_one(selector)
        if node and len(node.get_text(strip=True)) > 80:
            body = node
            break
    if body is None:
        candidates = [d for d in soup.find_all("div") if len(d.get_text(strip=True)) > 150]
        body = max(candidates, key=lambda d: len(d.get_text(strip=True))) if candidates else soup.body

    text = ""
    if body:
        text = "\n".join(
            line.strip() for line in body.get_text("\n", strip=True).splitlines() if line.strip()
        )

    publish = ""
    for pat in (
        r"(?:发布时间|公告时间|发布日期|公告日期|信息发布时间)[:：]?\s*(20\d{2}[-年/.]\d{1,2}[-月/.]\d{1,2})",
        r"(20\d{2}年\d{1,2}月\d{1,2}日)",
    ):
        m = re.search(pat, text)
        if m:
            publish = m.group(1)
            break
    if not publish:
        m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", base_url)
        if m:
            publish = m.group(0)

    attachments = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        name = a.get_text(strip=True) or href.split("/")[-1]
        lower = href.lower()
        if lower.endswith(ATTACH_EXT) or "download" in lower or "oss/download" in lower:
            attachments.append({"name": name[:120], "url": urljoin(base_url, href)})

    host = urlparse(base_url).netloc
    return {"title": title, "text": text, "publish_date": publish, "attachments": attachments, "host": host}
