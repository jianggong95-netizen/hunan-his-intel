"""湖南省政府采购网采集器测试：列表过滤、详情解析、附件与供应商抽取。"""
import unittest
from types import SimpleNamespace

from app.collectors.hunan_ccgp import HunanCcgpCollector


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.encoding = "utf-8"
        self.text = ""
        self.url = ""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _ts(date_str="2025-06-01"):
    from datetime import datetime

    return int(datetime.strptime(date_str, "%Y-%m-%d").timestamp() * 1000)


LIST_PAYLOAD = {
    "result": {
        "data": {
            "data": [
                {
                    "articleId": "A1",
                    "firstCode": "A01",
                    "parentId": "P1",
                    "pathName": "政府采购|中标(成交)公告",
                    "districtName": "邵阳市隆回县",
                    "publishDate": _ts("2025-06-01"),
                    "title": "<em>隆回县人民医院</em>医院信息系统采购中标公告",
                },
                {
                    "articleId": "A2",
                    "firstCode": "A01",
                    "parentId": "P2",
                    "pathName": "政府采购|中标(成交)公告",
                    "districtName": "长沙市",
                    "publishDate": _ts("2025-06-02"),
                    "title": "某学校教学楼维修中标公告",
                },
                {
                    "articleId": "A3",
                    "firstCode": "A01",
                    "parentId": "P3",
                    "pathName": "政府采购|招标公告",
                    "districtName": "长沙市",
                    "publishDate": _ts("2025-06-03"),
                    "title": "某医院信息系统采购招标公告",
                },
                {
                    "articleId": "A4",
                    "firstCode": "A01",
                    "parentId": "P4",
                    "pathName": "政府采购|中标(成交)公告",
                    "districtName": "岳阳市",
                    "publishDate": _ts("2025-06-04"),
                    "title": "某医院信息系统采购中标公告",
                    "invalid": True,
                },
            ]
        }
    }
}

DETAIL_PAYLOAD = {
    "result": {
        "data": {
            "content": (
                "<p>采购人：隆回县人民医院</p>"
                "<p>供应商名称：深圳市联医科技有限公司</p>"
                "<p>中标（成交）金额：1306000元</p>"
            ),
            "attachmentList": [
                {"fileName": "采购需求及技术参数.pdf", "fileUrl": "http://example.com/param.pdf"},
            ],
        }
    }
}


class HunanCcgpTest(unittest.TestCase):
    def _collector(self, list_payload=None, detail_payload=None):
        calls = []

        def fake_get(url, method="GET", json=None, params=None, **kwargs):
            calls.append({"url": url, "method": method, "json": json, "params": params})
            if url.endswith("/portal/all"):
                return FakeResponse(list_payload if list_payload is not None else LIST_PAYLOAD)
            if url.endswith("/portal/detail"):
                return FakeResponse(detail_payload if detail_payload is not None else DETAIL_PAYLOAD)
            return FakeResponse({}, status_code=404)

        self.calls = calls
        return HunanCcgpCollector(SimpleNamespace(get=fake_get), lambda msg, level="info": None)

    def test_search_filters_non_hospital_non_award_and_invalid(self):
        collector = self._collector()
        items = collector.search("医院信息系统", max_pages=1, since="2025-01-01")
        self.assertEqual([i["article_id"] for i in items], ["A1"])
        self.assertIn("/luban/front/detail", items[0]["url"])
        self.assertIn("articleId=A1", items[0]["url"])
        self.assertEqual(items[0]["city_hint"], "邵阳")
        self.assertEqual(items[0]["publish_date"], "2025-06-01")

    def test_search_respects_since(self):
        collector = self._collector()
        items = collector.search("医院信息系统", max_pages=1, since="2025-07-01")
        self.assertEqual(items, [])

    def test_detail_parses_text_and_attachments(self):
        collector = self._collector()
        text, atts = collector._detail("A1")
        self.assertIn("采购人：隆回县人民医院", text)
        self.assertIn("深圳市联医科技有限公司", text)
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]["url"], "http://example.com/param.pdf")

    def test_collect_attaches_detail_and_supplier(self):
        collector = self._collector()
        items = collector.collect(["医院信息系统"], max_pages=1, since="2025-01-01")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn("隆回县人民医院", item["detail_text"])
        self.assertEqual(item["supplier_override"], "深圳市联医科技有限公司")
        self.assertEqual(item["detail_attachments"][0]["name"], "采购需求及技术参数.pdf")
        # 一次列表 POST + 一次详情 GET
        methods = [c["method"] for c in self.calls]
        self.assertEqual(methods, ["POST", "GET"])
        self.assertEqual(self.calls[0]["json"]["keyword"], "医院信息系统")

    def test_broken_json_returns_empty(self):
        collector = self._collector(list_payload=ValueError("bad json"))
        items = collector.search("医院信息系统", max_pages=1)
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
