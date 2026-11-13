import json
import tempfile
import unittest
from pathlib import Path

from backend.data import get_catalog, get_mission, import_data, open_db
from backend.search import get_chunks, lexical_search


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "raw"
        self.db = Path(self.temp.name) / "dataset.db"
        texts = {"1": "长日入夜行", "2": "喧哗与骚动", "3": "匹诺康尼", "4": "三月七",
                 "10": "欢迎来到星穹列车。", "11": "我选择留下。", "12": "我选择离开。",
                 "13": "只有留下才能见到这条分支台词。", "14": "这是未关联台词。", "15": "瓦尔特", "16": "开拓途中，注意休息。"}
        self.write("TextMap/TextMapCHS.json", texts)
        self.write("ExcelOutput/MainMission.json", [{"MainMissionID": 1, "Name": {"Hash": 1}, "WorldID": 3, "ChapterID": 2, "Type": "Main"}])
        self.write("ExcelOutput/MissionChapterConfig.json", [{"ID": 2, "ChapterName": {"Hash": 2}}])
        self.write("ExcelOutput/WorldDataConfig.json", [{"ID": 3, "WorldName": {"Hash": 3}}])
        self.write("ExcelOutput/TalkSentenceConfig.json", [{"TalkSentenceID": value, "TalkSentenceText": {"Hash": value}, "TextmapTalkSentenceName": {"Hash": 15 if value == 16 else 4}} for value in [10, 11, 12, 13, 14, 16]])
        self.write("Config/Level/Mission/1/Act/Act1.json", {"OnStartSequece": [
            {"TaskList": [{"$type": "RPG.GameCore.PlayAndWaitSimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 10}, {"TalkSentenceID": 16}]},
                          {"$type": "RPG.GameCore.PlayOptionTalk", "OptionList": [{"TalkSentenceID": 11, "TriggerCustomString": "stay"}, {"TalkSentenceID": 12, "TriggerCustomString": "leave"}]}]},
            {"TaskList": [{"$type": "RPG.GameCore.WaitCustomString", "CustomString": {"Value": "stay"}},
                          {"$type": "RPG.GameCore.PlayAndWaitSimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 13}]}]},
        ]})
        self.stats = import_data(self.root, self.db)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_names_branches_and_orphans(self):
        mission = get_mission(self.db, "1")
        self.assertEqual(mission["mission"]["name"], "长日入夜行")
        self.assertEqual(mission["mission"]["chapter"], "喧哗与骚动")
        self.assertEqual(mission["mission"]["world"], "匹诺康尼")
        self.assertEqual(mission["mission"]["version_status"], "unknown")
        self.assertTrue(any(edge["target"] == "stay" for edge in mission["edges"]))
        matches = lexical_search(self.db, "分支台词")
        self.assertTrue(matches)
        self.assertEqual(matches[0]["branch"], "stay")
        self.assertEqual(lexical_search(self.db, "未关联台词"), [])
        with open_db(self.db) as conn:
            for row in conn.execute("SELECT text FROM chunks"):
                self.assertFalse("我选择留下" in row[0] and "我选择离开" in row[0])

    def test_alias_short_queries_filters_and_punctuation(self):
        self.assertTrue(lexical_search(self.db, "老杨"))
        self.assertTrue(lexical_search(self.db, "三月"))
        self.assertTrue(lexical_search(self.db, "开拓途中注意休息"))
        self.assertTrue(lexical_search(self.db, "三月七 星穹列车"))
        self.assertTrue(lexical_search(self.db, "老杨 休息"))
        self.assertTrue(lexical_search(self.db, "休息", {"character": "老杨"}))
        self.assertTrue(lexical_search(self.db, "列车", {"character": "三月"}))
        self.assertEqual(lexical_search(self.db, "星穹列午"), [])
        self.assertTrue(lexical_search(self.db, "星穹列午", allow_fuzzy=True))
        self.assertEqual(lexical_search(self.db, "列车", {"world": "不存在"}), [])
        self.assertTrue(lexical_search(self.db, "列车", {"world": "匹诺康尼"}))
        self.assertEqual(lexical_search(self.db, '" OR * % _'), [])

    def test_stable_identifiers(self):
        other = Path(self.temp.name) / "second.db"
        import_data(self.root, other)
        with open_db(self.db) as first, open_db(other) as second:
            self.assertEqual(list(first.execute("SELECT id FROM lines ORDER BY id")), list(second.execute("SELECT id FROM lines ORDER BY id")))

    def test_story_scope_and_readable_match_fields(self):
        catalog = get_catalog(self.db)
        self.assertEqual(catalog["stats"]["unlinked_lines"], 0)
        self.assertEqual(catalog["stats"]["excluded_unlinked_sentences"], 1)
        self.assertEqual(catalog["stats"]["narrative_lines"], 5)
        self.assertNotIn("unlinked", [mission["id"] for mission in catalog["missions"]])
        # Simulate an older snapshot, which retained unassociated sentence records.
        with open_db(self.db, readonly=False) as conn:
            conn.execute("INSERT INTO missions(id,name,chapter,world,type) VALUES('unlinked','尚未关联的台词','','','待关联')")
            conn.execute("INSERT INTO chunks(stable_id,mission_id,script,sequence,branch,text,input_text,content_hash,line_ids) VALUES('legacy','unlinked','unlinked',0,'','未关联台词','未关联台词','legacy','[]')")
            identifier = conn.execute("SELECT id FROM chunks WHERE script='unlinked'").fetchone()[0]
        self.assertEqual(get_chunks(self.db, [identifier], "未关联台词"), [])
        option = lexical_search(self.db, "我选择离开")[0]
        self.assertEqual(option["kind"], "option")
        self.assertEqual(option["matched_field"], "text")
        self.assertEqual(option["text"], "我选择离开。")
        self.assertEqual(lexical_search(self.db, "长日入夜行")[0]["matched_field"], "mission")
        alias = lexical_search(self.db, "老杨")[0]
        self.assertEqual(alias["matched_field"], "speaker")
        self.assertEqual(alias["text"], "开拓途中,注意休息。")

    def test_body_typo_outranks_inherited_metadata(self):
        with open_db(self.db, readonly=False) as conn:
            conn.execute("UPDATE missions SET world='星穹列车' WHERE id='1'")
            conn.execute("UPDATE chunks SET input_text='星穹列车 ' || input_text")
            conn.execute("DELETE FROM chunks_fts")
            from backend.data import search_tokens
            for row in conn.execute("SELECT id,input_text FROM chunks"):
                conn.execute("INSERT INTO chunks_fts(rowid,terms) VALUES(?,?)", (row["id"], search_tokens(row["input_text"])))
        matches = lexical_search(self.db, "星穹列午", allow_fuzzy=True)
        self.assertTrue(matches)
        self.assertEqual(matches[0]["text"], "欢迎来到星穹列车。")
        self.assertEqual(matches[0]["matched_field"], "text")
        self.assertFalse(any(item["text"] == "我选择离开。" for item in matches))

    def test_keyword_search_keeps_all_keywords_and_requires_fuzzy_opt_in(self):
        for query in ("星穹列车", "欢迎 列车", "长日入夜行", "老杨"):
            matches = lexical_search(self.db, query)
            self.assertTrue(matches)
            self.assertTrue(all(item["match_type"] in {"exact", "keyword"} for item in matches))
        self.assertEqual(lexical_search(self.db, "欢迎 不存在"), [])
        self.assertEqual(lexical_search(self.db, "星穹列午"), [])
        approximate = lexical_search(self.db, "星穹列午", allow_fuzzy=True)
        self.assertTrue(approximate)
        self.assertEqual(approximate[0]["match_type"], "fuzzy")
        self.assertEqual(approximate[0]["matched_field"], "text")
        self.assertEqual(approximate[0]["text"], "欢迎来到星穹列车。")

        # A conjunction can span a multi-line scene. Its preview must still
        # contain a queried term when no single line contains every term.
        with open_db(self.db, readonly=False) as conn:
            conn.execute("""INSERT INTO lines
                SELECT 'intro',mission_id,script,sequence,-1,'999',speaker,'无关的开场白',
                       '无关的开场白',kind,branch,trigger,chunk_id FROM lines WHERE sentence_id='10'""")
        matches = lexical_search(self.db, "星穹列车 休息")
        self.assertTrue(matches)
        self.assertEqual(matches[0]["text"], "欢迎来到星穹列车。")

    def test_nonblocking_simple_talk_preserves_option_followup(self):
        script_path = self.root / "Config/Level/Mission/1/Act/Act1.json"
        script = json.loads(script_path.read_text(encoding="utf-8"))
        followup = script["OnStartSequece"][1]["TaskList"]
        followup[1]["$type"] = "RPG.GameCore.PlaySimpleTalk"
        followup.append({"$type": "RPG.GameCore.WaitSimpleTalkFinish"})
        self.write("Config/Level/Mission/1/Act/Act1.json", script)
        output = Path(self.temp.name) / "nonblocking.db"
        import_data(self.root, output)
        mission = get_mission(output, "1")
        self.assertTrue(any(line["sentence_id"] == "13" and line["branch"] == "stay"
                            and line["kind"] == "dialogue" for line in mission["lines"]))
        self.assertEqual(lexical_search(output, "分支台词")[0]["branch"], "stay")

    def test_custom_string_reference_and_missing_text_recovery(self):
        config = self.root / "ExcelOutput/TalkSentenceConfig.json"
        records = json.loads(config.read_text(encoding="utf-8"))
        records.append({"TalkSentenceID": 99, "TalkSentenceText": {"Hash": 99}})
        self.write("ExcelOutput/TalkSentenceConfig.json", records)
        self.write("Story/Discussion/Mission/1/DS1.json", {"OnStartSequece": [
            {"TaskList": [{"$type": "RPG.GameCore.TriggerCustomString", "CustomString": {"Value": "TalkSentence_14"}},
                          {"$type": "RPG.GameCore.PlayAndWaitSimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 99}, {"TalkSentenceID": 16}]}]},
        ]})
        before = Path(self.temp.name) / "before.db"
        import_data(self.root, before)
        text_path = self.root / "TextMap/TextMapCHS.json"
        texts = json.loads(text_path.read_text(encoding="utf-8"))
        texts["99"] = "新恢复的文字"
        self.write("TextMap/TextMapCHS.json", texts)
        after = Path(self.temp.name) / "after.db"
        import_data(self.root, after)
        with open_db(before) as first, open_db(after) as second:
            sql = "SELECT id FROM lines WHERE script LIKE 'Story/%' AND sentence_id='16'"
            self.assertEqual(first.execute(sql).fetchone()[0], second.execute(sql).fetchone()[0])
            self.assertTrue(second.execute("SELECT 1 FROM lines WHERE sentence_id='14' AND kind='reference'").fetchone())


if __name__ == "__main__":
    unittest.main()
