"""检索编排测试：查询计划、跨源去重、相关度排序、库内重复识别。"""
import sqlite3
import unittest

from app import search_plan
from app.normalize import his_relevance


def _item(title, url, source="hunan_ccgp", date="2026-01-05", notice_type="中标公告"):
    return {
        "title": title,
        "url": url,
        "source_id": source,
        "publish_date": date,
        "notice_type": notice_type,
    }


class BuildPlanTest(unittest.TestCase):
    def test_default_plan_expands_core_and_domain_queries(self):
        plan = search_plan.build_plan(["hunan_ccgp"], max_pages=6)
        queries = [q.keyword for q in plan]
        self.assertIn("医院信息", queries)
        self.assertIn("PACS", queries)
        self.assertIn("互联网医院", queries)
        self.assertEqual(len(queries), len(set(queries)))
        self.assertTrue(all(q.pages <= search_plan.SOURCE_CAPS["hunan_ccgp"]["max_pages"] for q in plan))

    def test_plan_covers_all_three_sources(self):
        plan = search_plan.build_plan(None, max_pages=2)
        sources = {q.source for q in plan}
        self.assertEqual(sources, {"hunan_ccgp", "hunan_gov_search", "huaihua_ggzy"})
        list_queries = [q for q in plan if q.source == "huaihua_ggzy"]
        self.assertEqual(len(list_queries), 1)
        self.assertEqual(list_queries[0].group, "list")

    def test_custom_keywords_do_not_append_domain_queries(self):
        plan = search_plan.build_plan(["hunan_ccgp"], keywords=["电子病历"], max_pages=1)
        self.assertEqual([q.keyword for q in plan], ["电子病历"])
        self.assertEqual(plan[0].group, "custom")

    def test_unknown_source_ignored(self):
        self.assertEqual(search_plan.build_plan(["not_exists"], max_pages=1), [])


class DedupeTest(unittest.TestCase):
    def test_same_url_kept_once_with_higher_score(self):
        low = _item("某医院信息平台项目", "http://a/1", notice_type="")
        high = _item("郴州市第一人民医院更换RIS/PACS系统中标公告", "http://a/1")
        kept, dropped = search_plan.dedupe([low, high])
        self.assertEqual(len(kept), 1)
        self.assertIn("PACS", kept[0]["title"])
        self.assertEqual(dropped[0]["reason"], "same_url")

    def test_cross_source_same_title_and_date_deduped(self):
        a = _item("石门县人民医院HIS系统采购中标公告", "http://a/1", source="hunan_ccgp")
        b = _item("石门县人民医院HIS系统采购中标公告", "http://b/2", source="hunan_gov_search")
        kept, dropped = search_plan.dedupe([a, b])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["reason"], "cross_source")

    def test_same_title_different_date_kept(self):
        a = _item("临湘市人民医院信息管理系统", "http://a/1", date="2020-09-08")
        b = _item("临湘市人民医院信息管理系统", "http://a/2", date="2020-08-19")
        kept, _ = search_plan.dedupe([a, b])
        self.assertEqual(len(kept), 2)

    def test_empty_url_dropped(self):
        kept, dropped = search_plan.dedupe([_item("某医院信息系统", "")])
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0]["reason"], "no_url")

    def test_db_cross_source_duplicate_dropped(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE announcements (id INTEGER PRIMARY KEY, title TEXT, publish_date TEXT, source_site TEXT)")
        conn.execute(
            "INSERT INTO announcements(title, publish_date, source_site) VALUES (?,?,?)",
            ("某县人民医院信息系统中标公告", "2026-01-05", "hunan_gov_search"),
        )
        item = _item("某县人民医院信息系统中标公告", "http://a/1", source="hunan_ccgp")
        kept, dropped = search_plan.dedupe([item], conn)
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0]["reason"], "db_cross_source")

    def test_db_same_source_not_treated_as_duplicate(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE announcements (id INTEGER PRIMARY KEY, title TEXT, publish_date TEXT, source_site TEXT)")
        conn.execute(
            "INSERT INTO announcements(title, publish_date, source_site) VALUES (?,?,?)",
            ("某县人民医院信息系统中标公告", "2026-01-05", "hunan_ccgp"),
        )
        item = _item("某县人民医院信息系统中标公告", "http://a/2", source="hunan_ccgp")
        kept, _ = search_plan.dedupe([item], conn)
        self.assertEqual(len(kept), 1)


class ScoreTest(unittest.TestCase):
    def test_pacs_title_scores_as_his(self):
        self.assertGreaterEqual(search_plan.score_candidate(_item("郴州市第一人民医院更换RIS/PACS系统", "http://a/1")), 3)
        self.assertTrue(search_plan.is_his_title("辰溪县妇幼保健院PACS和LIS系统"))

    def test_non_his_title_low_score(self):
        self.assertFalse(search_plan.is_his_title("某卫生院食堂改造工程"))
        self.assertLess(search_plan.score_candidate(_item("某卫生院食堂改造工程", "http://a/1")), 3)

    def test_void_penalised(self):
        self.assertLess(
            search_plan.score_candidate(_item("某医院信息系统采购流标公告", "http://a/1")), 3
        )

    def test_ascii_keyword_uses_word_boundary(self):
        score, _ = his_relevance("THIS is a test", "")
        self.assertEqual(score, 0)
        score, matched = his_relevance("医院HIS系统升级", "")
        self.assertGreaterEqual(score, 3)
        self.assertIn("HIS", matched)


if __name__ == "__main__":
    unittest.main()
