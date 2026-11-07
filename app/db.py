"""SQLite 数据访问层：建表、初始化、连接与通用查询。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from werkzeug.security import generate_password_hash

from . import config
from .regions import CITIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS regions (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    short TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hospitals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    norm_name TEXT NOT NULL UNIQUE,
    city_code TEXT,
    level TEXT,
    ownership TEXT DEFAULT '公立',
    aliases TEXT DEFAULT '[]',
    source TEXT,
    source_url TEXT,
    first_seen_at TEXT,
    is_master INTEGER DEFAULT 0,
    merged_into INTEGER
);

CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    aliases TEXT DEFAULT '[]',
    category TEXT DEFAULT 'HIS',
    is_unknown INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS supplier_brand_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    brand_name TEXT NOT NULL,
    match_type TEXT DEFAULT 'contains',
    priority INTEGER DEFAULT 1,
    note TEXT
);

CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_site TEXT,
    source_name TEXT,
    url TEXT NOT NULL UNIQUE,
    notice_id TEXT,
    title TEXT NOT NULL,
    notice_type TEXT,
    publish_date TEXT,
    purchaser_raw TEXT,
    purchaser_norm TEXT,
    hospital_id INTEGER,
    city_code TEXT,
    budget_yuan REAL,
    content_hash TEXT,
    raw_text TEXT,
    matched_keywords TEXT,
    relevance REAL DEFAULT 0,
    fetch_status TEXT DEFAULT 'ok',
    is_void INTEGER DEFAULT 0,
    first_seen_at TEXT,
    last_seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ann_hospital ON announcements(hospital_id);
CREATE INDEX IF NOT EXISTS idx_ann_city ON announcements(city_code);
CREATE INDEX IF NOT EXISTS idx_ann_date ON announcements(publish_date);
CREATE INDEX IF NOT EXISTS idx_ann_hash ON announcements(content_hash);

CREATE TABLE IF NOT EXISTS awards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id INTEGER NOT NULL,
    package_no TEXT,
    supplier_raw TEXT,
    supplier_norm TEXT,
    brand_name TEXT,
    brand_confidence REAL DEFAULT 0,
    amount_yuan REAL,
    amount_raw TEXT,
    award_date TEXT,
    is_winner INTEGER DEFAULT 1,
    brand_basis TEXT,
    review_status TEXT DEFAULT 'none',
    reviewed_by TEXT,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_award_ann ON awards(announcement_id);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id INTEGER NOT NULL,
    name TEXT,
    url TEXT NOT NULL,
    local_path TEXT,
    sha256 TEXT,
    size INTEGER,
    status TEXT DEFAULT 'unverified',
    checked_at TEXT,
    kind TEXT DEFAULT '其他',
    source TEXT DEFAULT 'direct',
    uploader TEXT,
    uploaded_at TEXT,
    is_valid INTEGER DEFAULT 1,
    last_checked_at TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_att_ann ON attachments(announcement_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_att_ann_url ON attachments(announcement_id, url);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id INTEGER NOT NULL,
    kind TEXT DEFAULT 'new_award',
    created_at TEXT,
    is_read INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notif_read ON notifications(is_read);

CREATE TABLE IF NOT EXISTS crawl_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    params TEXT,
    status TEXT DEFAULT 'running',
    started_at TEXT,
    finished_at TEXT,
    pages INTEGER DEFAULT 0,
    items INTEGER DEFAULT 0,
    inserted INTEGER DEFAULT 0,
    updated INTEGER DEFAULT 0,
    error TEXT
);

CREATE TABLE IF NOT EXISTS crawl_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    ts TEXT,
    level TEXT DEFAULT 'info',
    message TEXT
);

CREATE TABLE IF NOT EXISTS change_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT,
    entity_id INTEGER,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    changed_at TEXT,
    actor TEXT DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    action TEXT,
    detail TEXT,
    ip TEXT,
    ts TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL UNIQUE,
    last_check TEXT,
    status TEXT,
    http_code INTEGER,
    note TEXT
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


MIGRATIONS = {
    "hospitals": {
        "is_master": "INTEGER DEFAULT 0",
        "merged_into": "INTEGER",
    },
    "awards": {
        "brand_basis": "TEXT",
        "review_status": "TEXT DEFAULT 'none'",
        "reviewed_by": "TEXT",
        "reviewed_at": "TEXT",
    },
    "attachments": {
        "kind": "TEXT DEFAULT '其他'",
        "source": "TEXT DEFAULT 'direct'",
        "uploader": "TEXT",
        "uploaded_at": "TEXT",
        "is_valid": "INTEGER DEFAULT 1",
        "last_checked_at": "TEXT",
        "note": "TEXT",
    },
}


def migrate_db(conn: sqlite3.Connection) -> None:
    """对已存在的旧库补齐新增字段，并回填公告原文附件与新中标通知。"""
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_att_ann_url ON attachments(announcement_id, url)"
    )
    # 每条公告的原文页面作为「中标公告」类型附件登记，保证三来源齐全
    conn.execute(
        "INSERT INTO attachments(announcement_id, name, url, kind, source, status, is_valid, "
        "uploaded_at, last_checked_at) "
        "SELECT a.id, substr(a.title,1,120), a.url, '中标公告', 'announcement', 'ok', 1, "
        "COALESCE(a.first_seen_at, ?), COALESCE(a.first_seen_at, ?) "
        "FROM announcements a WHERE NOT EXISTS ("
        "SELECT 1 FROM attachments t WHERE t.announcement_id=a.id AND t.url=a.url)",
        (now(), now()),
    )
    # 历史公告视为已读，避免升级后一次性弹出全部红点
    conn.execute(
        "INSERT INTO notifications(announcement_id, kind, created_at, is_read) "
        "SELECT a.id, 'new_award', COALESCE(a.first_seen_at, ?), 1 FROM announcements a "
        "WHERE NOT EXISTS (SELECT 1 FROM notifications n WHERE n.announcement_id=a.id)",
        (now(),),
    )
    # 低置信/未识别进入待核队列
    conn.execute(
        "UPDATE awards SET review_status='pending' WHERE review_status='none' "
        "AND (brand_name IS NULL OR brand_name='未识别' OR brand_name='' OR brand_confidence<0.9)"
    )



def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        migrate_db(conn)
        for city in CITIES:
            conn.execute(
                "INSERT OR IGNORE INTO regions(code, name, short) VALUES (?,?,?)",
                (city["code"], city["name"], city["short"]),
            )
        for brand in config.load_brands():
            conn.execute(
                "INSERT OR IGNORE INTO brands(name, aliases, category) VALUES (?,?,?)",
                (brand["name"], json.dumps(brand.get("aliases", []), ensure_ascii=False), brand.get("category", "HIS")),
            )
        if conn.execute("SELECT COUNT(*) c FROM supplier_brand_rules").fetchone()["c"] == 0:
            for rule in config.load_supplier_rules():
                conn.execute(
                    "INSERT INTO supplier_brand_rules(pattern, brand_name, match_type, priority, note) VALUES (?,?,?,?,?)",
                    (rule["pattern"], rule["brand_name"], rule["match_type"], rule["priority"], rule["note"]),
                )
        if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES (?,?,?,?)",
                (config.ADMIN_USER, generate_password_hash(config.ADMIN_PASSWORD), "admin", now()),
            )


def log(conn: sqlite3.Connection, task_id: int | None, message: str, level: str = "info") -> None:
    conn.execute(
        "INSERT INTO crawl_logs(task_id, ts, level, message) VALUES (?,?,?,?)",
        (task_id, now(), level, message),
    )


def audit(conn: sqlite3.Connection, username: str, action: str, detail: str, ip: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_logs(username, action, detail, ip, ts) VALUES (?,?,?,?,?)",
        (username, action, detail, ip, now()),
    )
