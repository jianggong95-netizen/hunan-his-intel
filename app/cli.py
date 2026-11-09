"""命令行入口：初始化、采集、导出、健康检查、启动 Web。"""
from __future__ import annotations

import argparse
import csv
import json
import sys

from . import aggregate, attachments, brands_ops, config, export, hospital_master, importer, pipeline, web
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
    """导入医院名录 CSV/XLSX：中文或英文表头均可；--master 标记为主数据。"""
    init_db()
    if args.master:
        with open(args.path, "rb") as fh:
            result = hospital_master.import_master(fh, args.path, actor="cli")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    added = 0
    with open(args.path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        with db() as conn:
            for row in reader:
                name = (row.get("name") or row.get("医院名称") or "").strip()
                if not name:
                    continue
                norm = normalize_name(name)
                city = row.get("city") or row.get("市州") or detect_city(name) or ""
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


def cmd_hospital_unmatched(_args):
    init_db()
    print(json.dumps({"unmatched": aggregate.unmatched_hospitals(),
                      "suggestions": hospital_master.suggestions()}, ensure_ascii=False, indent=2))


def cmd_brand_recompute(args):
    init_db()
    result = brands_ops.recompute_awards(args.announcement_id, actor="cli")
    result["stats"] = brands_ops.unknown_ratio()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_import_records(args):
    """导入中标数据 CSV/XLSX：按公告链接合并，保留已有备注。"""
    init_db()
    result = importer.import_records(args.path, args.path, actor="cli")
    print(json.dumps(result, ensure_ascii=False, indent=2))


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

    p2 = sub.add_parser("import-hospitals", help="导入医院名录 CSV/XLSX")
    p2.add_argument("path")
    p2.add_argument("--master", action="store_true", help="标记为主数据（卫健委名录），用于别名归并")
    p2.set_defaults(func=cmd_import_hospitals)

    sub.add_parser("hospital-unmatched", help="列出未匹配医院与归并建议").set_defaults(
        func=cmd_hospital_unmatched)

    p5 = sub.add_parser("brand-recompute", help="按包件与集成商规则重算品牌")
    p5.add_argument("--announcement-id", type=int, default=None)
    p5.set_defaults(func=cmd_brand_recompute)

    p4 = sub.add_parser("import-records", help="导入/更新中标数据 CSV/XLSX（按公告链接合并，备注保留）")
    p4.add_argument("path")
    p4.set_defaults(func=cmd_import_records)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
