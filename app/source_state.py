"""采集源运行状态：失败归因、分站点冷却退避、断点续采游标。

思路借鉴 Agent-Reach（MIT，Panniantong/Agent-Reach）的「后端动作有序 + 失败分类后冷却」，
不复制其代码；这里按政府采购站点的实际错误重写：把超时/限流/风控/上游异常分开，
按类型和连续失败次数设置冷却时间，被封的站点只标记、不在冷却期内重试。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_runtime (
    source TEXT PRIMARY KEY,
    fail_count INTEGER DEFAULT 0,
    cooldown_until TEXT,
    last_error_kind TEXT,
    last_error TEXT,
    last_failure_at TEXT,
    last_success_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS crawl_cursor (
    source TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    cursor TEXT DEFAULT '{}',
    updated_at TEXT,
    PRIMARY KEY (source, scope_key)
);
"""

# 错误类型 → (中文名, 冷却阶梯秒数，按连续失败次数取)
ERROR_KINDS = {
    "blocked": ("风控/封禁", (3600, 7200)),
    "robots": ("robots 禁止", (3600,)),
    "rate_limited": ("限流", (300, 900, 1800)),
    "upstream_broken": ("上游异常", (120, 300, 900)),
    "network": ("网络不可达", (60, 120, 300)),
    "timeout": ("请求超时", (60, 120, 300)),
    "parse": ("解析失败", (60, 120)),
    "http": ("HTTP 错误", (60, 120)),
}

# 同一阶段出现多种错误时，按危害从高到低归因
KIND_PRIORITY = ("blocked", "robots", "rate_limited", "upstream_broken", "network", "timeout", "parse", "http")

BLOCK_MARKERS = (
    "访问过于频繁", "访问太频繁", "请求过于频繁", "请输入验证码", "访问被拒绝",
    "拒绝访问", "access denied", "forbidden", "请求被拦截", "安全拦截", "人机验证",
)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def classify_status(code: int | None) -> str:
    if code == 429:
        return "rate_limited"
    if code in (401, 403, 412, 418):
        return "blocked"
    if code and code >= 500:
        return "upstream_broken"
    return "http"


def classify_exception(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timed out" in text or "超时" in text:
        return "timeout"
    if any(w in name for w in ("connection", "proxy", "ssl", "dns")) or any(
        w in text for w in ("connection", "max retries", "name resolution", "ssl", "proxy")
    ):
        return "network"
    if "json" in name or "decode" in name:
        return "parse"
    return "http"


def classify_body(text: str) -> str | None:
    """页面内容像风控/验证码拦截页时返回 blocked。"""
    sample = (text or "")[:4000].lower()
    if not sample:
        return None
    if any(marker.lower() in sample for marker in BLOCK_MARKERS):
        return "blocked"
    return None


def pick_kind(kinds: list[str]) -> str:
    for kind in KIND_PRIORITY:
        if kind in kinds:
            return kind
    return "http"


def kind_label(kind: str) -> str:
    return ERROR_KINDS.get(kind, (kind, ()))[0]


def _cooldown_seconds(kind: str, fail_count: int) -> int:
    ladder = ERROR_KINDS.get(kind, ERROR_KINDS["http"])[1]
    index = max(0, min(fail_count - 1, len(ladder) - 1))
    return ladder[index]


def get_runtime(conn: sqlite3.Connection, source: str) -> dict:
    row = conn.execute("SELECT * FROM source_runtime WHERE source=?", (source,)).fetchone()
    return dict(row) if row else {}


def is_cooling(conn: sqlite3.Connection, source: str, at: str | None = None) -> tuple[bool, str]:
    at = at or now()
    row = conn.execute(
        "SELECT cooldown_until, last_error_kind FROM source_runtime WHERE source=?", (source,)
    ).fetchone()
    if not row or not row["cooldown_until"]:
        return False, ""
    if row["cooldown_until"] > at:
        kind = row["last_error_kind"] or "http"
        return True, f"{kind_label(kind)}冷却至 {row['cooldown_until']}"
    return False, ""


def record_failure(conn: sqlite3.Connection, source: str, kind: str, message: str = "",
                   at: str | None = None) -> dict:
    """记录一次失败并按类型/连续次数设置冷却；返回最新状态。"""
    at = at or now()
    row = conn.execute("SELECT fail_count FROM source_runtime WHERE source=?", (source,)).fetchone()
    fail_count = (row["fail_count"] if row else 0) + 1
    until = (datetime.strptime(at, "%Y-%m-%d %H:%M:%S")
             + timedelta(seconds=_cooldown_seconds(kind, fail_count))).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO source_runtime(source, fail_count, cooldown_until, last_error_kind, last_error, "
        "last_failure_at, updated_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET fail_count=excluded.fail_count, "
        "cooldown_until=excluded.cooldown_until, last_error_kind=excluded.last_error_kind, "
        "last_error=excluded.last_error, last_failure_at=excluded.last_failure_at, updated_at=excluded.updated_at",
        (source, fail_count, until, kind, (message or "")[:300], at, at),
    )
    conn.commit()
    return {"source": source, "kind": kind, "fail_count": fail_count,
            "cooldown_until": until, "message": (message or "")[:300]}


def record_success(conn: sqlite3.Connection, source: str, at: str | None = None) -> None:
    at = at or now()
    conn.execute(
        "INSERT INTO source_runtime(source, fail_count, cooldown_until, last_success_at, updated_at) "
        "VALUES (?,0,NULL,?,?) ON CONFLICT(source) DO UPDATE SET fail_count=0, cooldown_until=NULL, "
        "last_error_kind=NULL, last_error=NULL, last_success_at=excluded.last_success_at, "
        "updated_at=excluded.updated_at",
        (source, at, at),
    )


def reset(conn: sqlite3.Connection, sources: list[str]) -> int:
    """人工清除冷却与失败计数（用于确认站点恢复后手动重试）。"""
    count = 0
    for source in sources:
        cur = conn.execute(
            "UPDATE source_runtime SET fail_count=0, cooldown_until=NULL, last_error_kind=NULL, "
            "last_error=NULL, updated_at=? WHERE source=?",
            (now(), source),
        )
        count += cur.rowcount
    return count


def list_runtime(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM source_runtime ORDER BY source").fetchall()
    return [dict(r) for r in rows]


def get_cursor(conn: sqlite3.Connection, source: str, scope_key: str) -> dict:
    row = conn.execute(
        "SELECT cursor FROM crawl_cursor WHERE source=? AND scope_key=?", (source, scope_key)
    ).fetchone()
    if not row:
        return {}
    try:
        data = json.loads(row["cursor"] or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cursor(conn: sqlite3.Connection, source: str, scope_key: str, cursor: dict) -> None:
    conn.execute(
        "INSERT INTO crawl_cursor(source, scope_key, cursor, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(source, scope_key) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at",
        (source, scope_key, json.dumps(cursor, ensure_ascii=False), now()),
    )


def clear_cursor(conn: sqlite3.Connection, source: str, scope_key: str) -> None:
    conn.execute("DELETE FROM crawl_cursor WHERE source=? AND scope_key=?", (source, scope_key))


def clear_source_cursors(conn: sqlite3.Connection, source: str) -> int:
    return conn.execute("DELETE FROM crawl_cursor WHERE source=?", (source,)).rowcount


def list_cursors(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT source, scope_key, cursor, updated_at FROM crawl_cursor ORDER BY source, scope_key"
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["cursor"] = json.loads(item["cursor"] or "{}")
        except (TypeError, ValueError):
            item["cursor"] = {}
        out.append(item)
    return out
