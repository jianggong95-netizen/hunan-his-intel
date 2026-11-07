"""湖南省 14 个市州、站点域名与区县归属映射。"""
from __future__ import annotations

CITIES = [
    {"code": "430100", "name": "长沙市", "short": "长沙", "scope": "971101000"},
    {"code": "430200", "name": "株洲市", "short": "株洲", "scope": "973000000"},
    {"code": "430300", "name": "湘潭市", "short": "湘潭", "scope": "974000000"},
    {"code": "430400", "name": "衡阳市", "short": "衡阳", "scope": "972101000"},
    {"code": "430500", "name": "邵阳市", "short": "邵阳", "scope": "975101000"},
    {"code": "430600", "name": "岳阳市", "short": "岳阳", "scope": "976000000"},
    {"code": "430700", "name": "常德市", "short": "常德", "scope": "977001000"},
    {"code": "430800", "name": "张家界市", "short": "张家界", "scope": "978101000"},
    {"code": "430900", "name": "益阳市", "short": "益阳", "scope": "979000000"},
    {"code": "431000", "name": "郴州市", "short": "郴州", "scope": "980000000"},
    {"code": "431100", "name": "永州市", "short": "永州", "scope": "981101000"},
    {"code": "431200", "name": "怀化市", "short": "怀化", "scope": "982001000"},
    {"code": "431300", "name": "娄底市", "short": "娄底", "scope": "983101000"},
    {"code": "433100", "name": "湘西土家族苗族自治州", "short": "湘西", "scope": "984101000"},
]

PROVINCE_SCOPE = "001000000"


CITY_BY_SHORT = {c["short"]: c for c in CITIES}

# 域名/站点名 -> 市州
DOMAIN_RULES = [
    ("changsha", "长沙"), ("csggzy", "长沙"),
    ("zhuzhou", "株洲"), ("xiangtan", "湘潭"), ("hengyang", "衡阳"),
    ("shaoyang", "邵阳"), ("yueyang", "岳阳"), ("changde", "常德"),
    ("zhangjiajie", "张家界"), ("yiyang", "益阳"), ("chenzhou", "郴州"),
    ("yongzhou", "永州"), ("huaihua", "怀化"), ("loudi", "娄底"),
    ("xxz", "湘西"), ("xiangxi", "湘西"),
]

# 区县 -> 市州（用于从医院名推断所属市州）
COUNTY_RULES = {
    "长沙": ["浏阳", "宁乡", "长沙县", "望城"],
    "株洲": ["醴陵", "攸县", "茶陵", "炎陵", "株洲县", "渌口"],
    "湘潭": ["湘乡", "韶山", "湘潭县", "雨湖", "岳塘"],
    "衡阳": ["耒阳", "常宁", "衡阳县", "衡南", "衡山", "衡东", "祁东", "南岳", "珠晖", "雁峰", "石鼓", "蒸湘"],
    "邵阳": ["武冈", "洞口", "新宁", "绥宁", "城步", "隆回", "邵东", "新邵", "大祥", "双清", "北塔"],
    "岳阳": ["汨罗", "临湘", "湘阴", "平江", "华容", "岳阳县", "云溪", "君山", "岳阳楼"],
    "常德": ["安乡", "汉寿", "澧县", "临澧", "桃源", "石门", "津市", "武陵", "鼎城"],
    "张家界": ["慈利", "桑植", "永定", "武陵源"],
    "益阳": ["南县", "桃江", "安化", "沅江", "赫山", "资阳"],
    "郴州": ["资兴", "桂阳", "永兴", "宜章", "嘉禾", "临武", "汝城", "桂东", "安仁", "北湖", "苏仙"],
    "永州": ["祁阳", "东安", "双牌", "道县", "江永", "宁远", "蓝山", "新田", "江华", "零陵", "冷水滩"],
    "怀化": ["靖州", "会同", "沅陵", "辰溪", "麻阳", "新晃", "芷江", "通道", "溆浦", "洪江", "中方", "鹤城"],
    "娄底": ["冷水江", "涟源", "双峰", "新化", "娄星"],
    "湘西": ["吉首", "泸溪", "凤凰", "花垣", "保靖", "古丈", "永顺", "龙山"],
}


def scope_to_short() -> dict[str, str]:
    return {c["scope"]: c["short"] for c in CITIES}


def city_by_short(short: str) -> dict | None:
    return CITY_BY_SHORT.get(short)


def detect_city(*texts: str) -> str | None:
    """从 URL、站点名、医院名、正文中推断市州，返回 short。"""
    joined = " ".join(t for t in texts if t).lower()
    if not joined:
        return None
    for token, short in DOMAIN_RULES:
        if token in joined:
            return short
    for short, counties in COUNTY_RULES.items():
        if short in joined:
            return short
        for county in counties:
            if county in joined:
                return short
    # 兜底：市州全称
    for c in CITIES:
        if c["name"] in joined:
            return c["short"]
    return None
