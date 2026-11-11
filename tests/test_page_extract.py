"""详情页结构化抽取测试：表头式包件、标签式字段、代理机构排除、金额边界。"""
import unittest

from app import page_extract
from app.collectors.detail import parse_detail


TABLE_HTML = """
<table>
  <tr><th>包号</th><th>中标供应商名称</th><th>中标金额（元）</th></tr>
  <tr><td>1</td><td>湖南创星科技股份有限公司</td><td>1,234,567.00</td></tr>
  <tr><td>2</td><td>卫宁健康科技集团股份有限公司</td><td>86.5万元</td></tr>
</table>
"""

KV_HTML = """
<table>
  <tr><td>采购人</td><td>隆回县人民医院</td></tr>
  <tr><td>中标供应商</td><td>湖南创星科技股份有限公司</td></tr>
  <tr><td>中标金额</td><td>123.45万元</td></tr>
</table>
"""

BIDDER_HTML = """
<table>
  <tr><th>供应商名称</th><th>报价（元）</th></tr>
  <tr><td>湖南甲科技有限公司</td><td>1000000</td></tr>
</table>
"""


class TablePackageTest(unittest.TestCase):
    def test_multi_package_table(self):
        info = page_extract.parse_award_html(TABLE_HTML)
        self.assertEqual(len(info["packages"]), 2)
        self.assertEqual(info["packages"][0]["supplier"], "湖南创星科技股份有限公司")
        self.assertEqual(info["packages"][0]["package_no"], "1")
        self.assertEqual(info["packages"][0]["amount_yuan"], 1234567.0)
        self.assertEqual(info["packages"][1]["amount_yuan"], 865000.0)
        self.assertEqual(info["amount_yuan"], 1234567.0)
        self.assertIn("卫宁健康科技集团股份有限公司", info["suppliers"])

    def test_kv_table_fields(self):
        info = page_extract.parse_award_html(KV_HTML)
        self.assertEqual(info["packages"], [])
        self.assertEqual(info["purchaser"], "隆回县人民医院")
        self.assertEqual(info["suppliers"], ["湖南创星科技股份有限公司"])
        self.assertEqual(info["amount_yuan"], 1234500.0)

    def test_bidder_table_not_treated_as_award(self):
        info = page_extract.parse_award_html(BIDDER_HTML)
        self.assertEqual(info["packages"], [])
        self.assertEqual(info["suppliers"], [])

    def test_agency_excluded(self):
        self.assertFalse(page_extract.is_supplier("湖南某某招标有限公司"))
        self.assertTrue(page_extract.is_supplier("湖南创星科技股份有限公司"))

    def test_amount_cell_ignores_package_number(self):
        self.assertEqual(page_extract.parse_amount_cell("1"), (None, ""))
        self.assertEqual(page_extract.parse_amount_cell("2,710,000.00元")[0], 2710000.0)

    def test_text_fallback_when_no_table(self):
        text = "中标供应商名称：湖南创星科技股份有限公司\n中标（成交）金额：1306000元"
        info = page_extract.parse_award_html("", "", text)
        self.assertEqual(info["suppliers"], ["湖南创星科技股份有限公司"])
        self.assertEqual(info["amount_yuan"], 1306000.0)

    def test_table_package_merges_amount_with_unit(self):
        info = page_extract.parse_award_html(
            "<table><tr><th>中标供应商</th><th>中标价</th></tr>"
            "<tr><td>湖南医沃信息技术有限公司</td><td>49.8万</td></tr></table>"
        )
        self.assertEqual(info["amount_yuan"], 498000.0)


CANDIDATE_HTML = """
<table>
  <tr><th>中标（成交 ）候选人名称</th><th>投标报价</th><th>综合得分</th><th>推荐排名</th></tr>
  <tr><td>湖南维斯登信息科技有限公司</td><td>3330000.00</td><td>77.43</td><td>1</td></tr>
  <tr><td>长沙瀚云信息科技有限公司</td><td>3305000.00</td><td>70.00</td><td>2</td></tr>
</table>
"""

CANDIDATE_TEXT_RANK_HTML = """
<table>
  <tr><th>推荐排名</th><th>投 标 人 名 称</th><th>投标报价(元)</th><th>综合得分</th></tr>
  <tr><td>第一中标候选人</td><td>湖南福行信息科技有限公司</td><td>1 369000.00</td><td>89.29</td></tr>
  <tr><td>第二中标候选人</td><td>长沙艾赛思坦信息技术有限公司</td><td>1 380000.00</td><td>62.26</td></tr>
</table>
"""


class CandidateTableTest(unittest.TestCase):
    def test_numeric_rank_picks_first_candidate(self):
        info = page_extract.parse_award_html(CANDIDATE_HTML)
        self.assertEqual(len(info["packages"]), 1)
        self.assertEqual(info["packages"][0]["supplier"], "湖南维斯登信息科技有限公司")
        self.assertEqual(info["amount_yuan"], 3330000.0)
        self.assertIn("第一中标候选人报价", info["amount_raw"])

    def test_text_rank_and_space_thousand_separator(self):
        info = page_extract.parse_award_html(CANDIDATE_TEXT_RANK_HTML)
        self.assertEqual(info["packages"][0]["supplier"], "湖南福行信息科技有限公司")
        self.assertEqual(info["amount_yuan"], 1369000.0)

    def test_plain_bidder_list_without_rank_not_award(self):
        info = page_extract.parse_award_html(BIDDER_HTML)
        self.assertEqual(info["packages"], [])


class ParseDetailTest(unittest.TestCase):
    def test_detail_keeps_html_for_structured_extraction(self):
        html = "<html><body><div class='article_box'><p>" + "正文" * 60 + "</p></div></body></html>"
        detail = parse_detail(html, "http://example.com/a")
        self.assertIn("html", detail)
        self.assertEqual(detail["html"], html)


if __name__ == "__main__":
    unittest.main()
