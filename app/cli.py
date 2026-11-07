"""命令行入口：初始化、采集、导出、健康检查、启动 Web。"""
from __future__ import annotations

import argparse
import csv
import json
import sys

from . import aggregate, attachments, config, export, pipeline, web
from .db import db, init_db, now
from .normalize import normalize_name
from .regions import CITY_BY_SHORT, detect_city


def cmd_init(_args):
    init_db()
    print(f"数据库已初始化: {config.DB_PATH}")


def cmd_collect(args):
    init_db()
    stats = pipeline.run_collection(
        sources=args.sources.split(",") if args.sources else None,
        keywords=args.keywords.split(",") if args.keywords else None,
        since=args.since,
        max_pages=args.pages,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def cmd_health(_args):
    init_db()
    rows = pipeline.refresh_health()
    for r in rows:
        print(f"{r['name']:<24} {r['status']:<12} {r['note']}")


def cmd_export(_args):
    init_db()
    path = export.export_excel()
    print(f"已导出: {path}")


def cmd_serve(_args):
    init_db()
    web.run()


def cmd_import_hospitals(args):
    """导入医院名录 CSV：列名 name,city,level,source。"""
    init_db()
    added = 0
    with open(args.path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        with db() as conn:
            for row in reader:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                norm = normalize_name(name)
                city = row.get("city") or detect_city(name) or ""
                short = city.replace("市", "") if city else ""
                code = CITY_BY_SHORT.get(short, {}).get("code")
                exists = conn.execute("SELECT id FROM hospitals WHERE norm_name=?", (norm,)).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO hospitals(name, norm_name, city_code, level, ownership, aliases, source, first_seen_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (name, norm, code, row.get("level", ""), "公立", "[]", row.get("source", "名录导入"), now()),
                )
                added += 1
    print(f"导入完成，新增 {added} 家医院")


def cmd_stats(_args):
    init_db()
    print(json.dumps({"quality": aggregate.quality_stats(), "cities": aggregate.city_summary(),
                      "brands": aggregate.brand_share()}, ensure_ascii=False, indent=2))


def cmd_check_attachments(args):
    init_db()
    result = attachments.recheck_all(limit=args.limit, actor="cli")
    print(json.dumps(result, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="his-intel", description="湖南公立医院 HIS 中标情报系统")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化数据库").set_defaults(func=cmd_init)
    sub.add_parser("serve", help="启动 Web 服务").set_defaults(func=cmd_serve)
    sub.add_parser("export", help="导出 Excel").set_defaults(func=cmd_export)
    sub.add_parser("health", help="检测数据源可用性").set_defaults(func=cmd_health)
    sub.add_parser("stats", help="输出统计").set_defaults(func=cmd_stats)
    p3 = sub.add_parser("check-attachments", help="复检远端附件链接有效性")
    p3.add_argument("--limit", type=int, default=0)
    p3.set_defaults(func=cmd_check_attachments)

    p = sub.add_parser("collect", help="执行采集")
    p.add_argument("--sources", default="", help="逗号分隔，默认 hunan_gov_search,huaihua_ggzy")
    p.add_argument("--keywords", default="", help="逗号分隔，默认读取 config/his_keywords.txt")
    p.add_argument("--since", default=config.COLLECT_SINCE)
    p.add_argument("--pages", type=int, default=config.COLLECT_MAX_PAGES)
    p.set_defaults(func=cmd_collect)

    p2 = sub.add_parser("import-hospitals", help="导入医院名录 CSV")
    p2.add_argument("path")
    p2.set_defaults(func=cmd_import_hospitals)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
