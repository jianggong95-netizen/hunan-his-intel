"""公告详情页结构化抽取：优先解析表格与标签值，正则/文本抽取兜底。

采购公告的中标信息通常以两种形式出现：
1. 表头式：| 包号 | 中标供应商 | 中标金额 |，一行一个包件；
2. 标签式：| 中标供应商 | 湖南某某有限公司 |。
先按表头/标签定位字段，拿不到再退回 normalize 中的文本正则，避免整页正则误抓。
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .normalize import extract_amount, extract_purchaser, extract_suppliers, parse_cn_number, parse_date

PACKAGE_ALIASES = ("包号", "包件", "标段", "分包", "合同包", "包组", "标包")
SUPPLIER_STRONG = ("中标供应商", "成交供应商", "中标人", "成交人", "中标单位", "成交单位",
                   "中标候选人", "成交候选人", "第一中标候选人", "第一成交候选人")
CANDIDATE_ALIASES = ("中标（成交）候选人名称", "中标候选人名称", "成交候选人名称", "候选人名称",
                     "投标人名称")
RANK_ALIASES = ("推荐排名", "排名", "排序")
QUOTE_ALIASES = ("投标报价", "报价", "投标价格")
SUPPLIER_ALIASES = SUPPLIER_STRONG + ("供应商名称", "中标企业")
AMOUNT_ALIASES = ("中标金额", "成交金额", "中标价", "成交价", "中标价格", "成交价格",
                  "中标（成交）金额", "中标(成交)金额", "金额")
PURCHASER_ALIASES = ("采购人名称", "采购人", "采购单位", "招标人名称", "招标人")
PROJECT_ALIASES = ("采购项目名称", "项目名称", "标项名称")
PROJECT_NO_ALIASES = ("采购项目编号", "项目编号", "项目编码", "招标编号", "采购编号", "标项编号",
                      "采购计划编号", "计划编号", "项目代码")
AGENCY_ALIASES = ("采购代理机构名称", "采购代理机构", "代理机构名称", "代理机构", "招标代理机构",
                  "集中采购机构")
NOTICE_TYPE_ALIASES = ("公告类型", "公告种类", "公告类别", "信息类型")
DATE_ALIASES = ("发布日期", "公告日期", "公告发布时间", "发布时间", "公告时间", "信息发布时间", "公示日期")

FIELD_LABELS = {
    "supplier": "中标单位",
    "amount": "中标金额",
    "packages": "包件划分",
    "purchaser": "采购人",
    "project_no": "项目编号",
    "agency": "代理机构",
    "notice_type": "公告类型",
    "publish_date": "发布日期",
}

TAX_EXCLUDED = ("不含税", "未税", "税前")
TAX_INCLUDED = ("含税", "税后")
AMOUNT_KIND_WORDS = {"单价": "单价", "总价": "总价", "合计": "总价", "总金额": "总价", "总中标金额": "总价"}

AGENCY_WORDS = ("招标有限责任公司", "招标有限公司", "招标代理", "招标咨询", "采购中心",
                "管理咨询", "项目管理有限公司", "咨询有限公司", "代理有限公司")
ORG_SUFFIX = ("公司", "集团", "厂", "中心", "研究院", "研究所", "事务所", "联合体")

_NUM = re.compile(r"([0-9][0-9,，]*(?:\.[0-9]+)?)\s*(亿元|万元|万|元)?")
_CN_AMOUNT = re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]{3,}元")


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _hit(text: str, aliases) -> bool:
    target = _squash(text)
    return any(_squash(a) in target for a in aliases)


def _cell_text(cell) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()


def table_rows(html: str) -> list[list[list[str]]]:
    """把页面里的每个 table 解析成若干行单元格文本。"""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [_cell_text(c) for c in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def clean_supplier(value: str) -> str:
    value = (value or "").strip(" ：:；;，,。()（）")
    value = re.sub(r"^(名称|中标人|成交人)\s*[:：]\s*", "", value)
    return value.strip()


def is_supplier(value: str) -> bool:
    value = clean_supplier(value)
    if not (4 <= len(value) <= 60):
        return False
    if not any(s in value for s in ORG_SUFFIX):
        return False
    if any(w in value for w in AGENCY_WORDS):
        return False
    return True


def _empty_amount(raw: str, reason: str) -> dict:
    return {"yuan": None, "cents": None, "raw": raw, "matched": "", "unit": "",
            "unit_factor": 0, "tax_included": None, "kind": "", "note": reason}


def normalize_amount(value: str) -> dict:
    """金额归一：万元/亿元/元、大小写金额统一换算为元与分，并保留换算依据。

    返回 {yuan, cents, raw, matched, unit, unit_factor, tax_included, kind, note}；
    无单位且数值过小（如包号 1、序号 2）不采用，绝不按上下文猜单位。
    """
    raw = re.sub(r"\s+", " ", value or "").strip()
    compact = re.sub(r"(?<=\d)[\s\u00a0](?=\d)", "", raw)
    number = None
    matched = ""
    unit = ""
    factor = 1
    m = _NUM.search(compact)
    if m:
        number = float(m.group(1).replace(",", "").replace("，", ""))
        unit = m.group(2) or ""
        matched = m.group(0)
        if unit == "亿元":
            factor = 100000000
        elif unit in ("万元", "万"):
            factor = 10000
        elif unit == "" and number < 1000:
            return _empty_amount(raw, "数值过小且无单位，疑似包号/序号")
    else:
        cn = _CN_AMOUNT.search(compact)
        if cn:
            number = parse_cn_number(cn.group(0))
            matched = cn.group(0)
            unit = "元（大写）"
            factor = 1
    if number is None:
        return _empty_amount(raw, "未找到可核对的金额")
    yuan = number * factor
    if not (1000 <= yuan <= 5_000_000_000):
        return _empty_amount(raw, f"换算后 {yuan:.2f} 元超出合理区间")
    tax_included = None
    if any(w in compact for w in TAX_EXCLUDED):
        tax_included = False
    elif any(w in compact for w in TAX_INCLUDED):
        tax_included = True
    kind = next((v for k, v in AMOUNT_KIND_WORDS.items() if k in compact), "")
    return {
        "yuan": round(yuan, 2),
        "cents": int(round(yuan * 100)),
        "raw": raw,
        "matched": matched,
        "unit": unit or "元",
        "unit_factor": factor,
        "tax_included": tax_included,
        "kind": kind,
        "note": f"{matched} × {factor} = {yuan:.2f} 元",
    }


def parse_amount_cell(value: str) -> tuple[float | None, str]:
    """从单元格取金额（元）；无单位且数值过小视为包号/序号，不采用。"""
    info = normalize_amount(value)
    return info["yuan"], info["raw"] if info["yuan"] is not None else ""


AMOUNT_STRONG = ("中标金额", "成交金额", "中标价", "成交价", "中标价格", "成交价格",
                 "中标（成交）金额", "中标(成交)金额")


def _header_mapping(row: list[str]) -> dict:
    mapping = {}
    for idx, cell in enumerate(row):
        if _hit(cell, PACKAGE_ALIASES) and "package" not in mapping:
            mapping["package"] = idx
        elif _hit(cell, SUPPLIER_ALIASES) and "supplier" not in mapping:
            mapping["supplier"] = idx
            mapping["supplier_strong"] = _hit(cell, SUPPLIER_STRONG)
        elif _hit(cell, AMOUNT_ALIASES) and "amount" not in mapping:
            mapping["amount"] = idx
            mapping["amount_strong"] = _hit(cell, AMOUNT_STRONG)
    return mapping


def extract_table_packages(tables: list[list[list[str]]]) -> list[dict]:
    """表头式中标明细：表头需同时有供应商列与金额/包号列，避免把投标人清单当结果。"""
    packages: list[dict] = []
    for rows in tables:
        header_idx = None
        columns: dict = {}
        for i, row in enumerate(rows[:3]):
            mapping = _header_mapping(row)
            # 只有“中标/成交”字样或包号+中标金额才算结果表，普通报价/投标人清单不采
            if "supplier" in mapping and (mapping.get("supplier_strong") or mapping.get("amount_strong")):
                header_idx, columns = i, mapping
                break
        if header_idx is None:
            continue
        for row in rows[header_idx + 1:]:
            supplier = clean_supplier(row[columns["supplier"]]) if columns["supplier"] < len(row) else ""
            if not is_supplier(supplier):
                continue
            amount_info = _empty_amount("", "无金额列")
            if "amount" in columns and columns["amount"] < len(row):
                amount_info = normalize_amount(row[columns["amount"]])
            package_no = row[columns["package"]] if "package" in columns and columns["package"] < len(row) else ""
            packages.append(
                {
                    "package_no": re.sub(r"[^\d一二三四五六七八九十]", "", package_no)[:6] or "",
                    "supplier": supplier,
                    "amount_yuan": amount_info["yuan"],
                    "amount_raw": amount_info["raw"] if amount_info["yuan"] is not None else "",
                    "amount_cents": amount_info["cents"],
                    "amount_basis": amount_info["note"],
                    "tax_included": amount_info["tax_included"],
                    "amount_kind": "",
                }
            )
    for pkg in packages:
        pkg["amount_kind"] = pkg["amount_kind"] or (
            "分包价" if len(packages) > 1 else (pkg.get("amount_kind") or "总价"))
    return packages


def extract_candidate_packages(tables: list[list[list[str]]]) -> list[dict]:
    """中标候选人公示：取排名第一的候选人，金额标注为候选人报价。"""
    for rows in tables:
        header_idx = None
        columns: dict = {}
        for i, row in enumerate(rows[:3]):
            mapping = {}
            for idx, cell in enumerate(row):
                if _hit(cell, CANDIDATE_ALIASES) and "supplier" not in mapping:
                    mapping["supplier"] = idx
                    mapping["candidate"] = _hit(cell, ("候选人",))
                elif _hit(cell, QUOTE_ALIASES) and "amount" not in mapping:
                    mapping["amount"] = idx
                elif _hit(cell, RANK_ALIASES) and "rank" not in mapping:
                    mapping["rank"] = idx
            if "supplier" in mapping and ("amount" in mapping or "rank" in mapping) and (
                mapping.get("candidate") or "rank" in mapping
            ):
                header_idx, columns = i, mapping
                break
        if header_idx is None:
            continue
        picks = []
        for row in rows[header_idx + 1:]:
            supplier = clean_supplier(row[columns["supplier"]]) if columns["supplier"] < len(row) else ""
            if not is_supplier(supplier):
                continue
            rank = row[columns["rank"]] if "rank" in columns and columns["rank"] < len(row) else ""
            amount_info = _empty_amount("", "无金额列")
            if "amount" in columns and columns["amount"] < len(row):
                amount_info = normalize_amount(row[columns["amount"]])
            picks.append((supplier, rank.strip(), amount_info))
        if not picks:
            continue
        winner = next((p for p in picks if "第一" in p[1] or p[1] in ("1", "1.0")), picks[0])
        supplier, _, amount_info = winner
        return [{
            "package_no": "1",
            "supplier": supplier,
            "amount_yuan": amount_info["yuan"],
            "amount_raw": f"{amount_info['raw']}（第一中标候选人报价）" if amount_info["yuan"] else "",
            "amount_cents": amount_info["cents"],
            "amount_basis": amount_info["note"] + "（第一中标候选人报价）" if amount_info["yuan"] else "",
            "tax_included": amount_info["tax_included"],
            "amount_kind": "分包价",
        }]
    return []


def extract_kv(tables: list[list[list[str]]]) -> dict:
    """标签式字段：| 中标供应商 | 湖南某某有限公司 |。

    同时抓取项目编号、代理机构、公告类型、发布日期等结构化字段；金额额外保留分、
    含税标记与换算依据，供页面核对。
    """
    found = {
        "supplier": "", "amount_yuan": None, "amount_raw": "", "amount_cents": None,
        "amount_basis": "", "tax_included": None, "amount_kind": "",
        "purchaser": "", "project_name": "", "project_no": "", "agency": "",
        "notice_type": "", "publish_date": "",
    }
    for rows in tables:
        for row in rows:
            if len(row) < 2:
                continue
            label, value = row[0], " ".join(row[1:]).strip()
            if not value:
                continue
            if _hit(label, SUPPLIER_STRONG) and not found["supplier"] and is_supplier(value):
                found["supplier"] = clean_supplier(value)
            elif _hit(label, AMOUNT_ALIASES) and found["amount_yuan"] is None:
                info = normalize_amount(value)
                if info["yuan"] is not None:
                    found.update({
                        "amount_yuan": info["yuan"], "amount_raw": info["raw"],
                        "amount_cents": info["cents"], "amount_basis": info["note"],
                        "tax_included": info["tax_included"],
                        "amount_kind": info["kind"] or "总价",
                    })
            elif _hit(label, PURCHASER_ALIASES) and not found["purchaser"]:
                found["purchaser"] = value
            elif _hit(label, PROJECT_ALIASES) and not found["project_name"]:
                found["project_name"] = value
            elif _hit(label, PROJECT_NO_ALIASES) and not found["project_no"]:
                found["project_no"] = _clean_value(value)
            elif _hit(label, AGENCY_ALIASES) and not found["agency"]:
                found["agency"] = _clean_value(value)
            elif _hit(label, NOTICE_TYPE_ALIASES) and not found["notice_type"]:
                found["notice_type"] = _clean_value(value)
            elif _hit(label, DATE_ALIASES) and not found["publish_date"]:
                found["publish_date"] = parse_date(value) or ""
    return found


def _clean_value(value: str) -> str:
    return re.sub(r"^[\s:：]+|[\s，。；;]+$", "", value or "")[:120]


def extract_labeled(text: str, aliases) -> str:
    """正文标签兜底：项目编号：XXX / 代理机构：XXX，仅取紧邻标签的值，不猜。"""
    for alias in aliases:
        m = re.search(rf"{re.escape(alias)}\s*[:：]\s*([^\n，。；]{{2,80}})", text or "")
        if m:
            return _clean_value(m.group(1))
    return ""


def _missing_reasons(info: dict) -> dict:
    """字段抽取不到时记录可核对的缺失原因，不猜测、不填充默认值。"""
    reasons: dict[str, str] = {}
    if not info.get("suppliers"):
        reasons["supplier"] = "页面未出现中标/成交供应商表格或标签，正文也未匹配到单位名称"
    if info.get("amount_yuan") is None:
        reasons["amount"] = "未找到中标/成交金额标签，或金额无单位/数值不可核对"
    if not info.get("packages"):
        reasons["packages"] = "页面未出现包号/分包表格（可能为单一合同包）"
    if not info.get("purchaser"):
        reasons["purchaser"] = "页面未出现采购人标签，标题也未解析出医院名称"
    if not info.get("project_no"):
        reasons["project_no"] = "页面未出现项目编号/采购编号标签"
    if not info.get("agency"):
        reasons["agency"] = "页面未出现代理机构标签"
    if not info.get("notice_type"):
        reasons["notice_type"] = "页面未出现公告类型标签"
    if not info.get("publish_date"):
        reasons["publish_date"] = "页面未出现发布日期标签，且列表页未提供日期"
    return reasons


def parse_award_html(html: str, title: str = "", text: str = "") -> dict:
    """返回结构化中标字段 + 缺失原因。

    字段：purchaser / project_name / project_no / agency / notice_type / publish_date /
    packages / suppliers / amount_yuan / amount_cents / amount_kind / tax_included /
    missing_reasons。packages 非空表示表格里拿到了逐包件明细；否则走标签/正文兜底。
    """
    tables = table_rows(html)
    packages = extract_table_packages(tables) if tables else []
    if not packages and tables:
        packages = extract_candidate_packages(tables)
    kv = extract_kv(tables) if tables else {}

    purchaser = kv.get("purchaser", "") or extract_purchaser(text, title)
    project_name = kv.get("project_name", "")
    project_no = kv.get("project_no", "") or extract_labeled(text, PROJECT_NO_ALIASES)
    agency = kv.get("agency", "") or extract_labeled(text, AGENCY_ALIASES)
    notice_type = kv.get("notice_type", "") or extract_labeled(text, NOTICE_TYPE_ALIASES)
    publish_date = kv.get("publish_date", "")

    if packages:
        amounts = [p["amount_yuan"] for p in packages if p.get("amount_yuan")]
        suppliers = []
        for p in packages:
            if p["supplier"] not in suppliers:
                suppliers.append(p["supplier"])
        first_amount = next((p for p in packages if p.get("amount_yuan")), {})
        amount = max(amounts) if amounts else None
        info = {
            "purchaser": purchaser,
            "project_name": project_name,
            "project_no": project_no,
            "agency": agency,
            "notice_type": notice_type,
            "publish_date": publish_date,
            "packages": packages,
            "suppliers": suppliers,
            "amount_yuan": amount,
            "amount_cents": int(round(amount * 100)) if amount is not None else None,
            "amount_raw": first_amount.get("amount_raw", ""),
            "amount_basis": first_amount.get("amount_basis", ""),
            "amount_kind": "分包价" if len(packages) > 1 else first_amount.get("amount_kind", "总价"),
            "tax_included": first_amount.get("tax_included"),
        }
        info["missing_reasons"] = _missing_reasons(info)
        return info

    suppliers = [kv["supplier"]] if kv.get("supplier") else extract_suppliers(text)
    if kv.get("amount_yuan") is not None:
        amount, amount_raw = kv["amount_yuan"], kv["amount_raw"]
        amount_cents, amount_basis = kv["amount_cents"], kv["amount_basis"]
        tax_included, amount_kind = kv["tax_included"], kv["amount_kind"] or "总价"
    else:
        amount, amount_raw = extract_amount(text)
        amount_info = normalize_amount(amount_raw) if amount else _empty_amount("", "正文未匹配金额")
        amount_cents = int(round(amount * 100)) if amount is not None else None
        amount_basis = amount_info["note"]
        tax_included = amount_info["tax_included"]
        amount_kind = amount_info["kind"] or ("总价" if amount is not None else "")
    info = {
        "purchaser": purchaser,
        "project_name": project_name,
        "project_no": project_no,
        "agency": agency,
        "notice_type": notice_type,
        "publish_date": publish_date,
        "packages": [],
        "suppliers": suppliers,
        "amount_yuan": amount,
        "amount_cents": amount_cents,
        "amount_raw": amount_raw,
        "amount_basis": amount_basis,
        "amount_kind": amount_kind,
        "tax_included": tax_included,
    }
    info["missing_reasons"] = _missing_reasons(info)
    return info
