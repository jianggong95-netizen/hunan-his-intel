"""通用详情页解析：正文、标题、日期、附件。"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

ATTACH_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".ofd", ".txt", ".jpg", ".png")
PAGE_HINTS = ("pkg_detail", "/pkg/", "packagedetail", "package_detail", "attachment")
LINK_TEXT_HINTS = ("附件", "下载", "参数", "需求", "招标文件", "采购文件", "磋商文件", "清单", "明细表")
URL_HINTS = ("download", "attach", "fileid=", "uploadfile", "oss-", "/files/")


def _is_attachment(href: str, url: str, name: str) -> bool:
    path = urlparse(url).path.lower()
    if path.endswith(ATTACH_EXT):
        return True
    lower_href = href.lower()
    if any(k in lower_href for k in URL_HINTS):
        return True
    if any(k in (name or "") for k in LINK_TEXT_HINTS):
        return True
    return any(k in lower_href for k in PAGE_HINTS)


def attachment_links(html: str, base_url: str) -> list[dict]:
    """从详情页正文提取附件候选：直链文件、下载接口、交易中心包件/附件页。

    返回的 `direct=False` 表示是非直链页面，只登记不强行下载。
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        url = urljoin(base_url, href)
        if url.rstrip("/#") == base_url.rstrip("/#"):
            continue
        name = (a.get_text(strip=True) or "").strip()
        if not _is_attachment(href, url, name):
            continue
        if url in seen:
            continue
        seen.add(url)
        lower_href = href.lower()
        direct = (
            urlparse(url).path.lower().endswith(ATTACH_EXT)
            or any(k in lower_href for k in ("download", "fileid=", "uploadfile", "oss-"))
        )
        is_page = any(k in lower_href for k in PAGE_HINTS)
        if name in ("详细", "详情", "查看", "查看详细", "附件"):
            name = "交易中心包件页" if is_page else ""
        if not name:
            name = urlparse(url).path.rsplit("/", 1)[-1]
            if is_page:
                name = "交易中心包件页"
        out.append({"name": name[:120], "url": url, "direct": direct})
    return out



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

    attachments = attachment_links(html, base_url)

    host = urlparse(base_url).netloc
    return {"title": title, "text": text, "publish_date": publish, "attachments": attachments,
            "host": host, "html": html}
