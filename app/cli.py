"""命令行入口：初始化、采集、导出、健康检查、启动 Web。"""
from __future__ import annotations

import argparse
import csv
import json
import sys

from . import aggregate, attachments, brands_ops, config, export, hospital_master, importer, pipeline, source_state, version, web
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
        resume=args.resume,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def cmd_crawl_state(args):
    """查看各源冷却/失败归因与断点游标；可手动清零冷却。"""
    init_db()
    with db() as conn:
        if args.reset:
            sources = [s for s in args.reset.split(",") if s]
            reset = source_state.reset(conn, sources)
        else:
            reset = 0
        if args.clear_cursors:
            targets = [s for s in args.source.split(",") if s] if args.source else [
                row["source"] for row in source_state.list_cursors(conn)
            ]
            cleared = sum(source_state.clear_source_cursors(conn, s) for s in set(targets))
        else:
            cleared = 0
        payload = {
            "runtime": source_state.list_runtime(conn),
            "cursors": source_state.list_cursors(conn),
            "reset": reset,
            "cleared_cursors": cleared,
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


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


def cmd_reparse(args):
    init_db()
    result = pipeline.reparse(source=args.source, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_check_attachments(args):
    init_db()
    result = attachments.recheck_all(limit=args.limit, actor="cli")
    print(json.dumps(result, ensure_ascii=False))


def cmd_version(args):
    info = version.update_info()
    if args.no_check:
        print(f"{version.APP_NAME} v{version.__version__}")
        return
    print(json.dumps(info, ensure_ascii=False, indent=2))
    if info.get("error"):
        print(f"检查更新失败：{info['error']}（离线不影响使用）")
    elif info.get("has_update"):
        print(f"发现新版本 v{info['version']}：下载 zip 解压覆盖 his-intel 目录即可升级（data/ 不受影响）\n{info.get('url','')}")
    else:
        print("已是最新版本。")


def cmd_agent_reach(_args):
    from . import agent_reach

    print(json.dumps(agent_reach.status_row(), ensure_ascii=False, indent=2))


def cmd_fill_attachments(args):
    """从公告正文链接、交易中心包件/附件页回补附件。"""
    init_db()
    result = pipeline.fill_attachments(
        source=args.source, limit=args.limit, download=not args.no_download)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="his-intel", description="湖南公立医院 HIS 中标情报系统")
    parser.add_argument("--version", action="version", version=f"{version.APP_NAME} {version.__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化数据库").set_defaults(func=cmd_init)
    sub.add_parser("serve", help="启动 Web 服务").set_defaults(func=cmd_serve)
    sub.add_parser("export", help="导出 Excel").set_defaults(func=cmd_export)
    sub.add_parser("health", help="检测数据源可用性").set_defaults(func=cmd_health)
    sub.add_parser("stats", help="输出统计").set_defaults(func=cmd_stats)
    pv = sub.add_parser("version", help="显示版本并检查 GitHub Releases 更新")
    pv.add_argument("--no-check", action="store_true", help="只显示本地版本，不联网检查")
    pv.set_defaults(func=cmd_version)
    sub.add_parser("agent-reach", help="查看 Agent-Reach 回退通道状态").set_defaults(func=cmd_agent_reach)
    p3 = sub.add_parser("check-attachments", help="复检远端附件链接有效性")
    p3.add_argument("--limit", type=int, default=0)
    p3.set_defaults(func=cmd_check_attachments)

    p8 = sub.add_parser("fill-attachments", help="从公告正文/交易中心页回补附件链接")
    p8.add_argument("--source", default="hunan_ccgp")
    p8.add_argument("--limit", type=int, default=0)
    p8.add_argument("--no-download", action="store_true", help="只登记链接，不下载直链文件")
    p8.set_defaults(func=cmd_fill_attachments)

    p = sub.add_parser("collect", help="执行采集")
    p.add_argument("--sources", default="", help="逗号分隔，默认 hunan_gov_search,huaihua_ggzy")
    p.add_argument("--keywords", default="", help="逗号分隔，默认读取 config/his_keywords.txt")
    p.add_argument("--since", default=config.COLLECT_SINCE)
    p.add_argument("--pages", type=int, default=config.COLLECT_MAX_PAGES)
    p.add_argument("--resume", action="store_true", help="按断点游标继续上次中断的页/站点群")
    p.set_defaults(func=cmd_collect)

    p7 = sub.add_parser("crawl-state", help="查看/重置采集源冷却与断点游标")
    p7.add_argument("--source", default="", help="逗号分隔的源；配合 --clear-cursors 使用")
    p7.add_argument("--reset", default="", help="逗号分隔的源，清除冷却与失败计数")
    p7.add_argument("--clear-cursors", action="store_true", help="清除断点游标（默认全部源）")
    p7.set_defaults(func=cmd_crawl_state)

    p2 = sub.add_parser("import-hospitals", help="导入医院名录 CSV/XLSX")
    p2.add_argument("path")
    p2.add_argument("--master", action="store_true", help="标记为主数据（卫健委名录），用于别名归并")
    p2.set_defaults(func=cmd_import_hospitals)

    sub.add_parser("hospital-unmatched", help="列出未匹配医院与归并建议").set_defaults(
        func=cmd_hospital_unmatched)

    p6 = sub.add_parser("reparse", help="结构化回补已入库公告的金额/包件/第一候选人")
    p6.add_argument("--source", default="hunan_ccgp")
    p6.add_argument("--limit", type=int, default=0)
    p6.set_defaults(func=cmd_reparse)

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
