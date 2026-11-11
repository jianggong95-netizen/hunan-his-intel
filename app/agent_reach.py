"""Agent-Reach 可选通道适配层。

对接 GitHub 项目 Panniantong/Agent-Reach（MIT，Python CLI）：它把「网页读取 / 搜索」
封装成有序回退的通道（Jina Reader → Exa…），能读到 JS 渲染或本机直连不可达的公开页面。

本项目用法（全部只读、失败静默降级，不影响主流程）：
- read_page(url)：详情页直连失败（detail_unreachable）时回退读取正文；
- status_row()：数据源体检里报告通道安装与健康状态；
- 未安装 CLI 时所有函数返回不可用，采集行为与原来完全一致。

⚠️ PyPI 上的 `agent-reach` 是冒名包，禁止 pip 安装；请按官方仓库 docs/install.md 安装。
"""
from __future__ import annotations

import re
import shutil
import subprocess

from . import config

# 直链文件后缀（从 agent-reach 返回的 markdown 正文里提取附件链接）
_FILE_EXT = r"(?:pdf|doc|docx|xls|xlsx|zip|rar|ofd)"


def cli_path() -> str | None:
    """定位 agent-reach 可执行文件；禁用或未安装时返回 None。"""
    if not config.AGENT_REACH_ENABLED:
        return None
    return shutil.which("agent-reach")


def is_available() -> bool:
    return cli_path() is not None


def _run(args: list[str], timeout: int | None = None) -> tuple[bool, str]:
    exe = cli_path()
    if not exe:
        return False, "agent-reach 未安装"
    try:
        proc = subprocess.run(
            [exe, *args], capture_output=True, timeout=timeout or config.AGENT_REACH_TIMEOUT_SEC,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"agent-reach 超时（>{timeout or config.AGENT_REACH_TIMEOUT_SEC}s）"
    except Exception as exc:  # noqa: BLE001
        return False, f"agent-reach 调用失败: {exc}"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, ((proc.stderr or out) or f"exit {proc.returncode}")[:300]
    if not out:
        return False, "agent-reach 无输出"
    return True, out


def _strip_reader_meta(text: str) -> str:
    """去掉 Jina Reader 式输出的头部元信息行（Title:/URL Source:/Markdown Content:）。"""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and re.match(r"^(Title|URL Source|Markdown Source|Markdown Content)[:：]", lines[i].strip()):
        i += 1
    return "\n".join(lines[i:]).strip()


def file_links(text: str) -> list[dict]:
    """从正文中提取可下载直链（供附件登记）。"""
    out, seen = [], set()
    for m in re.finditer(r"https?://[^\s)\]\"'，。；]+?\." + _FILE_EXT + r"\b", text or "", re.I):
        url = m.group(0)
        if url in seen:
            continue
        seen.add(url)
        name = url.rsplit("/", 1)[-1]
        out.append({"name": name[:120], "url": url, "direct": True, "source": "agent_reach"})
    return out


def read_page(url: str) -> tuple[str, str]:
    """读取网页正文（markdown 式文本）。返回 (text, error)，失败时 text 为空。"""
    ok, out = _run(["read", url])
    if not ok:
        return "", out
    return _strip_reader_meta(out), ""


def status_row() -> dict:
    """供数据源体检使用的一行状态（不抛异常）。"""
    name = "Agent-Reach 通道"
    if not config.AGENT_REACH_ENABLED:
        return {"source": "agent_reach", "name": name, "status": "disabled", "http_code": None,
                "note": "AGENT_REACH_ENABLED=0，已手动禁用"}
    if not shutil.which("agent-reach"):
        return {"source": "agent_reach", "name": name, "status": "not_installed", "http_code": None,
                "note": "未安装 CLI；按 github.com/Panniantong/Agent-Reach 的 docs/install.md 安装（勿装 PyPI 冒名包）"}
    ok, out = _run(["channels"], timeout=30)
    if not ok:
        return {"source": "agent_reach", "name": name, "status": "error", "http_code": None, "note": out[:200]}
    usable = len(re.findall(r"\b(ok|available|working|✓)\b", out, re.I))
    return {"source": "agent_reach", "name": name, "status": "available", "http_code": None,
            "note": f"channels 输出 {len(out)} 字符，健康通道约 {usable} 个（read 回退通道可用）"}
