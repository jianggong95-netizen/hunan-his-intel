"""全局配置：读取 .env、路径、关键词与品牌规则。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 兼容 PyInstaller 打包后的路径
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    # utf-8-sig：兼容带 BOM 的 .env（Windows 记事本/PowerShell 常见），否则首个键名会混入 \\ufeff 而静默失效
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _abspath(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


APP_HOST = _env("APP_HOST", "127.0.0.1")
APP_PORT = int(_env("APP_PORT", "8765"))
APP_SECRET = _env("APP_SECRET", "dev-secret-change-me")

DB_PATH = _abspath(_env("DB_PATH", "data/his_intel.db"))
ATTACHMENT_DIR = _abspath(_env("ATTACHMENT_DIR", "data/attachments"))
EXPORT_DIR = _abspath(_env("EXPORT_DIR", "data/exports"))
CONFIG_DIR = BASE_DIR / "config"

COLLECT_SINCE = _env("COLLECT_SINCE", "2023-01-01")
COLLECT_MAX_PAGES = int(_env("COLLECT_MAX_PAGES", "6"))
REQUEST_INTERVAL_SEC = float(_env("REQUEST_INTERVAL_SEC", "4.5"))
REQUEST_TIMEOUT_SEC = int(_env("REQUEST_TIMEOUT_SEC", "20"))
DOWNLOAD_ATTACHMENTS = _env("DOWNLOAD_ATTACHMENTS", "1") == "1"
ATTACHMENT_CHECK_HOURS = int(_env("ATTACHMENT_CHECK_HOURS", "24"))
MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", "50"))
USER_AGENT = _env(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)

ADMIN_USER = _env("ADMIN_USER", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "admin123")

TYC_API_KEY = _env("TYC_API_KEY", "")
TYC_BASE_URL = _env("TYC_BASE_URL", "https://open.api.tianyancha.com")

# Agent-Reach 可选通道（github.com/Panniantong/Agent-Reach，只读回退；未安装自动降级）
AGENT_REACH_ENABLED = _env("AGENT_REACH_ENABLED", "1") == "1"
AGENT_REACH_TIMEOUT_SEC = int(_env("AGENT_REACH_TIMEOUT_SEC", "60"))

for _d in (DB_PATH.parent, ATTACHMENT_DIR, EXPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def load_keywords() -> list[str]:
    path = CONFIG_DIR / "his_keywords.txt"
    if not path.exists():
        return ["HIS", "医院信息系统"]
    items = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(line)
    return items


def load_brands() -> list[dict]:
    path = CONFIG_DIR / "brands.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_supplier_rules() -> list[dict]:
    path = CONFIG_DIR / "supplier_brand_rules.csv"
    if not path.exists():
        return []
    rows = []
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        rows.append(
            {
                "pattern": parts[0].strip(),
                "brand_name": parts[1].strip(),
                "match_type": parts[2].strip() or "contains",
                "priority": int(parts[3] or 1),
                "note": parts[4].strip() if len(parts) > 4 else "",
            }
        )
    return rows
