"""文本归一：医院名、采购单位、供应商、金额、日期、品牌与 HIS 相关性。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache

from . import config
from .regions import detect_city

_CN_DIGITS = {
    "零": 0, "○": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2, "三": 3, "叁": 3,
    "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7,
    "八": 8, "捌": 8, "九": 9, "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_ORG_SUFFIX = ("公司", "集团", "中心", "研究院", "研究所", "医院", "厂", "店", "事务所", "联合体")


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def normalize_name(name: str) -> str:
    """医院/单位名归一：去括号、去'本级'、统一全半角、去尾部标点。"""
    if not name:
        return ""
    name = clean_text(name)
    name = name.replace("（", "(").replace("）", ")").replace("，", ",")
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r"（[^）]*）", "", name)
    name = re.sub(r"(本级|采购单位|采购人|受.{0,20}委托)$", "", name)
    name = re.sub(r"[、,，。；;:：\s]+$", "", name)
    name = name.replace(" ", "")
    # 常见简称归一
    name = re.sub(r"(?<=市|县|区)一医院$", "第一人民医院", name)
    name = re.sub(r"(医院|保健院|卫生院)(医院)$", r"\1", name)
    return name


def extract_purchaser(text: str, title: str = "") -> str:
    patterns = [
        r"采购单位(?:名称)?\s*[:：]\s*([^\n，。；]{2,60})",
        r"采购人(?:名称)?\s*[:：]\s*([^\n，。；]{2,60})",
        r"招标人(?:名称)?\s*[:：]\s*([^\n，。；]{2,60})",
        r"受\s*([^\s，。]{4,40})\s*的委托",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            value = m.group(1).strip()
            if value and value not in ("/", "-", "无"):
                return normalize_name(value)
    if title:
        m = re.match(r"^(.{4,40}?(?:医院|卫生院|卫生服务中心|妇幼保健院|疾病预防控制中心|血站))", title)
        if m:
            return normalize_name(m.group(1))
    return ""


def parse_cn_number(text: str) -> float | None:
    """解析中文大写金额（到元），如 玖佰柒拾柒万柒仟柒佰 -> 9777700。"""
    text = re.sub(r"[元圆整人民币]", "", text)
    total = 0.0
    section = 0.0
    number = 0.0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        elif ch == "万":
            section = (section + number) * 10000
            total += section
            section = number = 0
        elif ch == "亿":
            section = (section + number) * 100000000
            total += section
            section = number = 0
    return total + section + number or None


_AMOUNT_LABEL = re.compile(
    r"(总中标金额|中标（成交）金额|中标\(成交\)金额|中标金额|成交金额|中标价|成交价|中标价格)"
)
_NUM = re.compile(r"(?:￥|¥|RMB)?\s*([0-9][0-9,，]*(?:\.[0-9]+)?)\s*(亿元|万元|万|元)?")


def extract_amount(text: str) -> tuple[float | None, str]:
    """提取中标/成交金额（元）。返回 (金额, 原始片段)。"""
    if not text:
        return None, ""
    for m in _AMOUNT_LABEL.finditer(text):
        window = text[m.end(): m.end() + 80]
        num = _NUM.search(window)
        if num:
            raw = num.group(0).strip()
            value = float(num.group(1).replace(",", "").replace("，", ""))
            unit = num.group(2) or ""
            if unit in ("亿元",):
                value *= 100000000
            elif unit in ("万元", "万"):
                value *= 10000
            elif value < 100 and "万" in window[: num.end() + 2]:
                value *= 10000
            if 1000 <= value <= 5_000_000_000:
                return value, (m.group(0) + window[:40]).strip()
        cn = re.search(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿]{3,}元", window)
        if cn:
            value = parse_cn_number(cn.group(0))
            if value and 1000 <= value <= 5_000_000_000:
                return value, (m.group(0) + cn.group(0)).strip()
    return None, ""


_SUPPLIER_LABEL = re.compile(
    r"(中标供应商名称|成交供应商名称|中标供应商|成交供应商|中标人名称|中标人|成交人)"
)
_AGENCY_WORDS = ("代理", "项目管理", "招标咨询", "采购代理")


def extract_suppliers(text: str) -> list[str]:
    """提取中标供应商候选：优先“中标供应商/成交供应商”标签，排除代理机构。"""
    found: list[str] = []
    for m in _SUPPLIER_LABEL.finditer(text):
        window = text[m.end(): m.end() + 50]
        candidate = re.split(r"[\s，。；、:：]", window.lstrip("（( ）)名称:："))[0]
        candidate = candidate.strip(" ：:()（）")
        if not (3 <= len(candidate) <= 60 and any(s in candidate for s in _ORG_SUFFIX)):
            continue
        if any(w in candidate for w in _AGENCY_WORDS):
            continue
        if candidate not in found:
            found.append(candidate)
            break  # 只取首个“中标供应商”，避免把投标人/联合体成员当成中标单位
    # 兜底：正文中“有限公司/集团”，仅在无中标供应商标签时使用
    if not found:
        for m in re.finditer(r"[\u4e00-\u9fa5]{2,30}(?:有限公司|有限责任公司|股份有限公司|集团)", text):
            candidate = m.group(0)
            if any(w in candidate for w in _AGENCY_WORDS):
                continue
            if candidate not in found:
                found.append(candidate)
                if len(found) >= 2:
                    break
    return found[:3]


def parse_date(value: str) -> str | None:
    if not value:
        return None
    m = re.search(r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})", value)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


@lru_cache(maxsize=1)
def _brand_catalog() -> tuple[list[dict], list[dict]]:
    return config.load_brands(), config.load_supplier_rules()


def match_brand(supplier: str, full_text: str, allow_text: bool = True) -> tuple[str | None, float, str]:
    """返回 (品牌, 置信度, 依据)。供应商直接命中给高置信，正文命中给低置信。

    allow_text=False 时只认供应商名称，避免把“正文提到某品牌”误当成中标厂家。
    """
    brands, rules = _brand_catalog()
    supplier = supplier or ""
    for rule in sorted(rules, key=lambda r: r["priority"]):
        pattern = rule["pattern"]
        if rule["match_type"] == "regex":
            hit = re.search(pattern, supplier) is not None
        else:
            hit = pattern in supplier
        if hit:
            return rule["brand_name"], 0.9, "供应商名称命中规则"
    for brand in brands:
        for alias in brand.get("aliases", []):
            if alias and alias in supplier:
                return brand["name"], 0.85, "供应商名称命中别名"
    if allow_text:
        for brand in brands:
            for alias in brand.get("aliases", []):
                if alias and len(alias) >= 2 and alias in full_text:
                    # 正文提及给低置信，避免把集成商误判为厂家
                    return brand["name"], 0.45, "正文提及品牌，需人工复核"
    return None, 0.0, "未识别"


def his_relevance(title: str, text: str) -> tuple[float, list[str]]:
    """标题命中权重 3，正文 1；返回分数与命中词。"""
    keywords = config.load_keywords()
    matched: list[str] = []
    score = 0.0
    for kw in keywords:
        in_title = kw.lower() in (title or "").lower()
        in_text = kw.lower() in (text or "").lower()
        if in_title or in_text:
            matched.append(kw)
            score += 3 if in_title else 1
    return score, matched


_VOID_WORDS = ("流标", "废标", "终止", "采购失败", "中标无效", "取消中标", "中止公告")


def is_void(title: str, text: str = "") -> bool:
    joined = f"{title or ''} {text[:200] if text else ''}"
    return any(w in joined for w in _VOID_WORDS)


_AWARD_WORDS = ("中标", "成交", "结果公告", "中标结果")


def is_award_notice(notice_type: str, title: str) -> bool:
    joined = f"{notice_type or ''} {title or ''}"
    return any(w in joined for w in _AWARD_WORDS)


def guess_city(*texts: str) -> str | None:
    return detect_city(*texts)


def normalize_company(name: str) -> str:
    if not name:
        return ""
    name = clean_text(name)
    name = re.sub(r"(（.*?）|\(.*?\))", "", name)
    return name.replace(" ", "")


def iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_KIND_RULES = (
    ("参数文件", ("参数", "需求", "技术规格", "技术参数", "配置清单", "功能要求", "用户需求")),
    ("招标文件", ("招标文件", "采购文件", "磋商文件", "谈判文件", "询价文件", "招标书", "标书", "采购需求")),
    ("中标公告", ("中标", "成交", "结果公告", "结果公示", "评标结果", "中标结果")),
)


def classify_attachment(name: str, url: str = "") -> str:
    """按文件名/URL 归类附件：参数文件 / 招标文件 / 中标公告 / 其他。"""
    text = f"{name or ''} {url or ''}".lower()
    for kind, words in _KIND_RULES:
        if any(w in text for w in words):
            return kind
    return "其他"
