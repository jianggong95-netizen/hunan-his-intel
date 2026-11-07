"""Excel 导出：明细、市州汇总、品牌份额、数据质量。"""
from __future__ import annotations

from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from . import config
from .aggregate import brand_share, city_summary, latest_two, quality_stats

HEAD_FILL = PatternFill("solid", fgColor="1F6F5C")
HEAD_FONT = Font(bold=True, color="FFFFFF", size=10)
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _write_sheet(ws, headers: list[str], rows: list[list], widths: list[int]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in rows:
        ws.append(row)
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.border = BORDER
            cell.alignment = Alignment(vertical="center", wrap_text=False)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def export_excel(path=None) -> str:
    path = path or (config.EXPORT_DIR / f"湖南HIS中标情报_{datetime.now():%Y%m%d_%H%M}.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "每院最近2次"

    records = latest_two()
    rows = []
    for r in records:
        direct = [a for a in r["attachments"] if a["source"] != "announcement"]
        links = []
        for a in direct:
            target = a["url"]
            if a["local_path"]:
                target = str(config.BASE_DIR / a["local_path"])
            links.append(f"{a['kind']}：{target}")
        types = "、".join(f"{a['kind']}({'有效' if a['is_valid'] else '失效'})" for a in direct)
        rows.append(
            [
                r["city"], r["hospital_name"], f"最近{r['rank']}次", r["title"], r["publish_date"],
                r["supplier"], r["brand"], r["amount"] if r["amount"] is not None else "未获取",
                r["url"], types or "无参数/招标附件", "\n".join(links) or "未获取",
                "详情不可达" if r["fetch_status"] != "ok" else "正常",
            ]
        )
    _write_sheet(
        ws,
        ["市州", "医院", "次序", "招标项目名称", "中标日期", "中标单位", "HIS厂家品牌",
         "中标金额(元)", "公告原文", "附件类型", "附件链接（直链/本地）", "数据状态"],
        rows,
        [10, 28, 10, 46, 12, 30, 16, 14, 40, 26, 52, 12],
    )
    for i, row in enumerate(rows, start=2):
        cell = ws.cell(i, 9)
        cell.hyperlink = row[8]
        cell.style = "Hyperlink"
        first = (row[10] or "").split("\n")[0]
        if "http" in first:
            link_cell = ws.cell(i, 11)
            link_cell.hyperlink = first.split("：", 1)[-1]
            link_cell.style = "Hyperlink"

    ws2 = wb.create_sheet("市州汇总")
    _write_sheet(
        ws2,
        ["市州", "医院数", "HIS中标公告数", "有记录医院数"],
        [[c["name"], c["hospitals"], c["announcements"], c["covered_hospitals"]] for c in city_summary()],
        [16, 12, 18, 16],
    )

    ws3 = wb.create_sheet("品牌份额")
    _write_sheet(
        ws3,
        ["HIS厂家品牌", "涉及公告数", "金额合计(元)"],
        [[b["b"], b["c"], b["amount"] or 0] for b in brand_share()],
        [18, 14, 16],
    )

    ws4 = wb.create_sheet("数据质量")
    q = quality_stats()
    _write_sheet(
        ws4,
        ["指标", "数值"],
        [
            ["公告总数", q["announcements"]],
            ["医院总数", q["hospitals"]],
            ["详情不可达", q["detail_unreachable"]],
            ["缺金额公告", q["missing_amount"]],
            ["缺参数/招标附件公告", q["missing_attachment"]],
            ["未识别品牌/中标记录", f"{q['unknown_brand_awards']}/{q['awards']}"],
            ["未识别品牌占比", q["unknown_brand_ratio"]],
            ["待人工复核", q["pending_review"]],
            ["附件状态", str(q["attachments"])],
            ["附件按类型", str(q.get("attachments_by_kind", {}))],
            ["附件按来源", str(q.get("attachments_by_source", {}))],
        ],
        [28, 24],
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return str(path)
