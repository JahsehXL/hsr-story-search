import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend.data import VERIFIED_SCENES, get_catalog, get_mission, import_data, normalize_text, open_db, search_tokens
from backend.data_maintenance import update_script_order
from backend.search import _extract_content_terms, _score_fields, get_chunks, lexical_search, semantic_context_bonus


BASE_TEXTS = {
    "1": "长日入夜行", "2": "喧哗与骚动", "3": "匹诺康尼", "4": "三月七",
    "10": "欢迎来到星穹列车。", "11": "我选择留下。", "12": "我选择离开。",
    "13": "只有留下才能见到这条分支台词。", "14": "这是未关联台词。", "15": "瓦尔特", "16": "开拓途中，注意休息。",
}


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "raw"
        self.reset_raw()

    def tearDown(self):
        self.temp.cleanup()

    def reset_raw(self):
        if self.root.exists():
            shutil.rmtree(self.root)
        self.write("TextMap/TextMapCHS.json", BASE_TEXTS)
        self.write("ExcelOutput/MainMission.json", [{"MainMissionID": 1, "Name": {"Hash": 1}, "WorldID": 3, "ChapterID": 2, "Type": "Main"}])
        self.write("ExcelOutput/MissionChapterConfig.json", [{"ID": 2, "ChapterName": {"Hash": 2}}])
        self.write("ExcelOutput/WorldDataConfig.json", [{"ID": 3, "WorldName": {"Hash": 3}}])
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": value, "TalkSentenceText": {"Hash": value}, "TextmapTalkSentenceName": {"Hash": 15 if value == 16 else 4}}
            for value in (10, 11, 12, 13, 14, 16)
        ])
        self.write("Config/Level/Mission/1/Act/Act1.json", {"OnStartSequece": [
            {"TaskList": [{"$type": "RPG.GameCore.PlayAndWaitSimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 10}, {"TalkSentenceID": 16}]},
                          {"$type": "RPG.GameCore.PlayOptionTalk", "OptionList": [{"TalkSentenceID": 11, "TriggerCustomString": "stay"}, {"TalkSentenceID": 12, "TriggerCustomString": "leave"}]}]},
            {"TaskList": [{"$type": "RPG.GameCore.WaitCustomString", "CustomString": {"Value": "stay"}},
                          {"$type": "RPG.GameCore.PlayAndWaitSimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 13}]}]},
        ]})

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def add_mission_with_sentences(self, mission_id: int, pairs: list[tuple[int, str]], mtype: str = "Main") -> None:
        missions = json.loads((self.root / "ExcelOutput/MainMission.json").read_text(encoding="utf-8"))
        if not any(m["MainMissionID"] == mission_id for m in missions):
            missions.append({"MainMissionID": mission_id, "Name": {"Hash": 1}, "Type": mtype})
            self.write("ExcelOutput/MainMission.json", missions)
        texts = json.loads((self.root / "TextMap/TextMapCHS.json").read_text(encoding="utf-8"))
        sentences = json.loads((self.root / "ExcelOutput/TalkSentenceConfig.json").read_text(encoding="utf-8"))
        for sid, text in pairs:
            texts[str(sid)] = text
            sentences.append({"TalkSentenceID": sid, "TalkSentenceText": {"Hash": sid}, "TextmapTalkSentenceName": {"Hash": 4}})
        self.write("TextMap/TextMapCHS.json", texts)
        self.write("ExcelOutput/TalkSentenceConfig.json", sentences)

    def simple_perf(self, rel: str, sentence_ids: list[int]) -> None:
        self.write(rel, {"OnStartSequece": [{"TaskList": [{
            "$type": "RPG.GameCore.PlayAndWaitSimpleTalk",
            "SimpleTalkList": [{"TalkSentenceID": sid} for sid in sentence_ids],
        }]}]})

    def test_core_import_search_metadata_and_stable_ids(self):
        first_db = Path(self.temp.name) / "first.db"
        second_db = Path(self.temp.name) / "second.db"
        import_data(self.root, first_db)
        import_data(self.root, second_db)

        with open_db(first_db) as a, open_db(second_db) as b:
            self.assertEqual(list(a.execute("SELECT id FROM lines ORDER BY id")), list(b.execute("SELECT id FROM lines ORDER BY id")))
            for row in a.execute("SELECT text FROM chunks"):
                self.assertFalse("我选择留下" in row[0] and "我选择离开" in row[0])

        mission = get_mission(first_db, "1")
        self.assertEqual((mission["mission"]["name"], mission["mission"]["chapter"], mission["mission"]["world"], mission["mission"]["version_status"]),
                         ("长日入夜行", "喧哗与骚动", "匹诺康尼", "unknown"))
        self.assertTrue(any(edge["target"] == "stay" for edge in mission["edges"]))
        self.assertEqual(lexical_search(first_db, "分支台词")[0]["branch"], "stay")
        self.assertEqual(lexical_search(first_db, "未关联台词"), [])

        for query in ("老杨", "三月", "开拓途中注意休息", "三月七 星穹列车", "老杨 休息", "星穹列车", "欢迎 列车", "长日入夜行"):
            matches = lexical_search(first_db, query)
            self.assertTrue(matches, query)
            self.assertTrue(all(item["match_type"] in {"exact", "keyword"} for item in matches))
        self.assertTrue(lexical_search(first_db, "休息", {"character": "老杨"}))
        self.assertTrue(lexical_search(first_db, "列车", {"character": "三月"}))
        self.assertEqual(lexical_search(first_db, "星穹列午"), [])
        fuzzy = lexical_search(first_db, "星穹列午", allow_fuzzy=True)
        self.assertEqual((fuzzy[0]["match_type"], fuzzy[0]["matched_field"], fuzzy[0]["text"]), ("fuzzy", "text", "欢迎来到星穹列车。"))
        self.assertEqual(lexical_search(first_db, "列车", {"world": "不存在"}), [])
        self.assertTrue(lexical_search(first_db, "列车", {"world": "匹诺康尼"}))
        self.assertEqual(lexical_search(first_db, '" OR * % _'), [])

        catalog = get_catalog(first_db)
        self.assertEqual((catalog["stats"]["unlinked_lines"], catalog["stats"]["excluded_unlinked_sentences"], catalog["stats"]["narrative_lines"]), (0, 1, 5))
        self.assertNotIn("unlinked", [m["id"] for m in catalog["missions"]])
        opt_hit = lexical_search(first_db, "我选择离开")[0]
        self.assertEqual((opt_hit["kind"], opt_hit["matched_field"], opt_hit["text"]), ("option", "text", "我选择离开。"))
        self.assertEqual(lexical_search(first_db, "长日入夜行")[0]["matched_field"], "mission")
        self.assertEqual((lexical_search(first_db, "老杨")[0]["matched_field"], lexical_search(first_db, "老杨")[0]["text"]), ("speaker", "开拓途中,注意休息。"))

        with open_db(first_db, readonly=False) as conn:
            conn.execute("INSERT INTO missions(id,name,chapter,world,type) VALUES('unlinked','尚未关联的台词','','','待关联')")
            conn.execute("INSERT INTO chunks(stable_id,mission_id,script,sequence,branch,text,input_text,content_hash,line_ids) VALUES('legacy','unlinked','unlinked',0,'','未关联台词','未关联台词','legacy','[]')")
            unlinked_id = conn.execute("SELECT id FROM chunks WHERE script='unlinked'").fetchone()[0]
            conn.execute("""INSERT INTO lines SELECT 'intro',mission_id,script,sequence,-1,'999',speaker,'无关的开场白',
                            '无关的开场白',kind,branch,trigger,chunk_id FROM lines WHERE sentence_id='10'""")
            conn.execute("UPDATE missions SET world='星穹列车' WHERE id='1'")
            conn.execute("UPDATE chunks SET input_text='星穹列车 ' || input_text")
            conn.execute("DELETE FROM chunks_fts")
            for row in conn.execute("SELECT id,input_text FROM chunks"):
                conn.execute("INSERT INTO chunks_fts(rowid,terms) VALUES(?,?)", (row["id"], search_tokens(row["input_text"])))
        self.assertEqual(get_chunks(first_db, [unlinked_id], "未关联台词"), [])
        self.assertEqual(lexical_search(first_db, "星穹列车 休息")[0]["text"], "欢迎来到星穹列车。")
        typo_hits = lexical_search(first_db, "星穹列午", allow_fuzzy=True)
        self.assertEqual((typo_hits[0]["text"], typo_hits[0]["matched_field"]), ("欢迎来到星穹列车。", "text"))
        self.assertFalse(any(item["text"] == "我选择离开。" for item in typo_hits))

        # Preserve embedding inputs on metadata fill, invalidate on content/title changes, and verify version/fuzzy/verified scenes.
        self.write("ExcelOutput/MainMission.json", [{"MainMissionID": 1, "Type": "Main"}])
        baseline = Path(self.temp.name) / "without-metadata.db"
        import_data(self.root, baseline)
        with open_db(baseline, readonly=False) as conn:
            conn.execute("INSERT INTO info VALUES('embedding','{}')")
            original = conn.execute("SELECT input_text,content_hash FROM chunks ORDER BY id").fetchall()
            self.assertTrue(original[0]["input_text"].startswith("任务 1\n"))
        self.write("ExcelOutput/MainMission.json", [{"MainMissionID": 1, "Name": {"Hash": 1}, "WorldID": 3, "ChapterID": 2, "Type": "Main"}])
        candidate = Path(self.temp.name) / "new-metadata.db"
        self.assertEqual(import_data(self.root, candidate, baseline=baseline)["preserved_embedding_inputs"], len(original))
        with open_db(candidate) as conn:
            self.assertEqual(original, conn.execute("SELECT input_text,content_hash FROM chunks ORDER BY id").fetchall())

        self.write("ExcelOutput/MainMission.json", [
            {"MainMissionID": 1, "Name": {"Hash": 3}, "Type": "Main"},
            {"MainMissionID": 1054701, "Name": {"Hash": 200}, "WorldID": 3, "ChapterID": 2, "Type": "Main"},
            {"MainMissionID": int(VERIFIED_SCENES[0][0]), "Name": {"Hash": 1}, "Type": "Main"},
        ])
        with open_db(first_db, readonly=False) as conn:
            conn.execute("INSERT OR IGNORE INTO info VALUES('embedding','{}')")
        texts = json.loads((self.root / "TextMap/TextMapCHS.json").read_text(encoding="utf-8"))
        texts.update({
            "11": "你是幕后黑手吧。", "13": "噫，别再说了！在恐怖片里，出现的这人肯定是幕后黑手。",
            "200": "世界图像的时代", "201": "星期日", "202": "知更鸟",
            "203": "我们关于笼中鸟的争论该结束了。", "204": "这只星穹列车上的小鸟终于飞起来了。",
        })
        mission_id, v_script, v_sids = VERIFIED_SCENES[0]
        texts.update({str(sid): f"演出对白 {idx}" for idx, sid in enumerate(v_sids)})
        texts["201110601"] = "喂，丹恒，快把你隐藏的力量用出来呀！"
        texts["201110602"] = "…你先吧。"
        self.write("TextMap/TextMapCHS.json", texts)
        sentences = json.loads((self.root / "ExcelOutput/TalkSentenceConfig.json").read_text(encoding="utf-8"))
        sentences.extend([
            {"TalkSentenceID": 157010101, "TalkSentenceText": {"Hash": 203}, "TextmapTalkSentenceName": {"Hash": 201}},
            {"TalkSentenceID": 157010102, "TalkSentenceText": {"Hash": 204}, "TextmapTalkSentenceName": {"Hash": 202}},
            *[{"TalkSentenceID": sid, "TalkSentenceText": {"Hash": sid}, "TextmapTalkSentenceName": {"Hash": 15 if sid == 201110602 else 4}}
              for sid in v_sids],
        ])
        self.write("ExcelOutput/TalkSentenceConfig.json", sentences)
        self.simple_perf("Story/Mission/1054701/Story105470101.json", [157010101, 157010102])
        rich_db = Path(self.temp.name) / "rich.db"
        stats = import_data(self.root, rich_db, baseline=first_db)
        self.assertEqual((stats["preserved_embedding_inputs"], stats["verified_scene_lines"]), (0, len(v_sids)))
        with open_db(rich_db, readonly=False) as conn:
            conn.execute("UPDATE missions SET version='1.0' WHERE id='1'")
            conn.execute("UPDATE missions SET version='4.7' WHERE id='1054701'")
        self.assertEqual(get_catalog(rich_db)["versions"], ["4.7", "1.0"])
        self.assertEqual([item["version"] for item in lexical_search(rich_db, "星穹列车")], ["4.7", "1.0"])
        self.assertEqual(lexical_search(rich_db, "", {"version": "4.7"})[0]["mission_id"], "1054701")
        self.assertEqual(lexical_search(rich_db, "星期日和知更鸟关于笼中鸟的争论")[0]["mission_id"], "1054701")
        f_hit = lexical_search(rich_db, "恐怖片才会出现的幕后黑手", allow_fuzzy=True)[0]
        self.assertEqual((f_hit["speaker"], f_hit["match_type"], f_hit["branch"]), ("三月七", "fuzzy", "stay"))
        v_hit = lexical_search(rich_db, "三月七让丹恒用出隐藏的力量", allow_fuzzy=True)[0]
        self.assertEqual((v_hit["mission_id"], v_hit["script"], v_hit["sentence_id"]), (mission_id, v_script, "201110601"))
        v_detail = get_mission(rich_db, mission_id, line_id=v_hit["line_id"])
        found = next(i for i, line in enumerate(v_detail["lines"]) if line["id"] == v_hit["line_id"])
        self.assertEqual(v_detail["lines"][found + 1]["text"], texts["201110602"])

    def test_text_cleanup_and_description_scoring(self):
        self.assertEqual(normalize_text("<报社总部的传社玉玺>"), "<报社总部的传社玉玺>")
        self.assertEqual(normalize_text("<color=#fff>{M#他说}{F#她说}</color>"), "他说 她说")
        self.assertEqual(set(_extract_content_terms("卡芙卡在空间站唤醒开拓者")), {"卡芙卡", "空间站", "开拓者", "唤醒"})
        self.assertEqual(set(_extract_content_terms("大黑塔与黑塔讨论空间站")), {"大黑塔", "黑塔", "空间站"})
        self.assertEqual(set(_extract_content_terms("星期日和知更鸟关于谐乐鸽笼子的对话")), {"星期日", "知更鸟", "谐乐", "鸽笼", "笼子"})

        terms = _extract_content_terms("卡芙卡在空间站唤醒开拓者")
        direct = semantic_context_bonus(terms, "卡芙卡：这儿？一个空间站，不重要。", {"world": "空间站「黑塔」"}, {"卡芙卡"})
        mention = semantic_context_bonus(terms, "开拓者：我记得卡芙卡在空间站唤醒我。", {"world": "翁法罗斯"}, {"开拓者"})
        self.assertGreater(direct, mention)
        self.assertAlmostEqual(semantic_context_bonus(terms, "唤醒", {}, set()), 0.025)
        self.assertEqual(semantic_context_bonus([], "", {}, set()), 0.0)
        unrelated = {"text": "我记得自己在空间站被唤醒。", "speaker": "开拓者"}
        self.assertEqual(_score_fields("卡芙卡在空间站唤醒开拓者", unrelated)[0], 0.0)
        self.assertGreater(_score_fields("卡芙卡在空间站唤醒开拓者", {**unrelated, "speaker": "卡芙卡"})[0], 0.0)

    def test_mirrored_scripts_captions_and_references(self):
        mirror = "Story/Mission/1/Story1.json"
        self.simple_perf(mirror, [10])
        self.simple_perf("Config/Level/Mission/1/Act/Act2.json", [10, 16])
        different = "Story/Mission/1/Story2.json"
        self.simple_perf(different, [10, 13])

        records = json.loads((self.root / "ExcelOutput/TalkSentenceConfig.json").read_text(encoding="utf-8"))
        records.append({"TalkSentenceID": 99, "TalkSentenceText": {"Hash": 99}})
        self.write("ExcelOutput/TalkSentenceConfig.json", records)
        self.write("Story/Discussion/Mission/1/DS1.json", {"OnStartSequece": [
            {"TaskList": [{"$type": "RPG.GameCore.TriggerCustomString", "CustomString": {"Value": "TalkSentence_14"}},
                          {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 99}, {"TalkSentenceID": 16}]},
                          {"$type": "RPG.GameCore.WaitSimpleTalkFinish"}]},
        ]})
        self.write("Story/Mission/1/transition.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PerformanceTransition", "TalkSentenceID": 14, "TextEnabled": True},
            {"$type": "RPG.GameCore.PerformanceTransition", "TalkSentenceID": 13, "TextEnabled": False},
        ]}]})
        self.add_mission_with_sentences(1, [(17, "独立过渡后对白")])
        self.write("Config/Level/Mission/1/Mission_101.json", {"OnStartSequece": [
            {"TaskList": [
                {"$type": "RPG.GameCore.PlayScreenTransfer", "TextEnabled": True, "TalkSentenceID": 10},
                {"$type": "RPG.GameCore.TriggerCustomString", "CustomString": {"Value": "caption"}},
                {"$type": "RPG.GameCore.WaitCustomString", "CustomString": {"Value": "caption_done"}},
                {"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 17}]},
            ]},
            {"TaskList": [
                {"$type": "RPG.GameCore.WaitCustomString", "CustomString": {"Value": "caption"}},
                {"$type": "RPG.GameCore.PerformanceTransition", "TextEnabled": True, "TalkSentenceID": 14},
                {"$type": "RPG.GameCore.TriggerCustomString", "CustomString": {"Value": "caption_done"}},
            ]},
        ]})

        before = Path(self.temp.name) / "before.db"
        import_data(self.root, before)
        texts = json.loads((self.root / "TextMap/TextMapCHS.json").read_text(encoding="utf-8"))
        texts["99"] = "新恢复的文字"
        self.write("TextMap/TextMapCHS.json", texts)
        after = Path(self.temp.name) / "after.db"
        import_data(self.root, after)

        legacy_line = get_mission(after, "1", script=mirror)["lines"][0]
        detail = get_mission(after, "1")
        self.assertFalse(any(line["script"] == mirror for line in detail["lines"]))
        self.assertEqual([line["sentence_id"] for line in detail["lines"] if line["script"] == different], ["10", "13"])
        anchor = get_mission(after, "1", line_id=legacy_line["id"], limit=1)
        self.assertEqual((anchor["lines"][0]["id"], anchor["lines"][0]["script"]), (anchor["anchor_line_id"], "Config/Level/Mission/1/Act/Act1.json"))
        self.assertEqual([line["sentence_id"] for line in get_mission(after, "1", script="Story/Mission/1/transition.json")["lines"]], ["14"])
        self.assertEqual([(l["sequence"], l["sentence_id"]) for l in get_mission(after, "1", script="Config/Level/Mission/1/Mission_101.json")["lines"]],
                         [(0, "10"), (0, "14"), (0, "17")])
        with open_db(before) as first, open_db(after) as second:
            sql = "SELECT id FROM lines WHERE script='Story/Discussion/Mission/1/DS1.json' AND sentence_id='16'"
            self.assertEqual(first.execute(sql).fetchone()[0], second.execute(sql).fetchone()[0])
            self.assertTrue(second.execute("SELECT 1 FROM lines WHERE sentence_id='14' AND kind='reference'").fetchone())

    def test_containers_mission_dedup_and_alt_branches(self):
        self.add_mission_with_sentences(1, [(sid, f"扩展{sid}") for sid in (*range(100, 114), 200, 201, 202, 203, 210, 211, 212, 220, 221, 222, 223)])
        self.write("Config/Level/Mission/1/Act/ActNested.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PredicateTaskList",
             "SuccessTaskList": [{"$type": "RPG.GameCore.PlayAndWaitSimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 100}]},
                                 {"$type": "RPG.GameCore.PlayOptionTalk", "OptionList": [{"TalkSentenceID": 101, "TriggerCustomString": "opt_a"}]}],
             "FailedTaskList": [{"$type": "RPG.GameCore.PlayOptionTalk", "OptionList": [{"TalkSentenceID": 102, "TriggerCustomString": "opt_b"}]}]},
            {"$type": "RPG.GameCore.SwitchCase",
             "TaskList": [{"SuccessTaskList": [{"$type": "RPG.GameCore.PlayRogueSimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 103}]}]}],
             "DefaultTask": [{"$type": "RPG.GameCore.PlayRogueOptionTalk", "OptionList": [{"TalkSentenceID": 104, "TriggerCustomString": "opt_r"}]}]},
            {"$type": "RPG.GameCore.RandomConfig", "TaskList": [{"$type": "RPG.GameCore.PlayNPCBubbleTalk", "BubbleTalkInfoList": [{"TalkSentenceID": 105}]}]},
            {"$type": "RPG.GameCore.PropSetupTrigger", "OnTriggerEnter": [{"$type": "RPG.GameCore.PlayNPCSingleBubbleTalk", "TalkSentenceID": {"IsDynamic": False, "FixedValue": {"Value": 106}}}]},
            {"$type": "RPG.GameCore.PlayFullScreenTransfer", "TextInfo": {"TextList": [{"TalkSentenceID": 107}, {"TalkSentenceID": 108}]}},
            {"$type": "RPG.GameCore.ConsumeOrigamiItem", "OnSubmitConfirm": [{"$type": "RPG.GameCore.PlayOrigamiTraceTalk", "TalkList": [{"TalkInfo": {"SimpleTalkList": [{"TalkSentenceID": 109}, {"TalkSentenceID": 110}]}}]}]},
            {"$type": "RPG.GameCore.PerformanceEndBlackText", "TalkSentenceID": 111},
        ]}]})
        self.write("Config/Level/Mission/1/Act/ActMutex.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 200}]},
            {"$type": "RPG.GameCore.PredicateTaskList",
             "SuccessTaskList": [{"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 201}]}],
             "FailedTaskList": [{"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 202}]}]},
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 203}]},
        ]}]})
        self.write("Config/Level/Mission/1/Act/ActSingleSide.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PredicateTaskList",
             "SuccessTaskList": [{"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 210}]},
                                 {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 211}]}],
             "FailedTaskList": []},
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 212}]},
        ]}]})
        self.write("Config/Level/Mission/1/Act/ActSwitchRand.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.SwitchCase", "TaskList": [{"SuccessTaskList": [{"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 220}]}]}],
             "DefaultTask": [{"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 221}]}]},
            {"$type": "RPG.GameCore.RandomConfig", "TaskList": [{"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 222}]},
                                                                {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 223}]}]},
        ]}]})
        self.write("Config/Level/Mission/1/Mission_100.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 10}, {"TalkSentenceID": 16}]},
        ]}]})
        self.write("Config/Level/Mission/1/Mission_101.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PropSetupTrigger", "OnTriggerEnter": [{"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 14}]}]},
        ]}]})

        out = Path(self.temp.name) / "containers.db"
        import_data(self.root, out)

        nested = get_mission(out, "1", script="Config/Level/Mission/1/Act/ActNested.json")["lines"]
        self.assertEqual([line["sentence_id"] for line in nested], [str(sid) for sid in range(100, 112)])
        options = [line for line in nested if line["kind"] == "option"]
        self.assertEqual([(l["sentence_id"], l["branch"]) for l in options], [("101", "sequence:0#alt:0#cond:1"), ("102", "sequence:0#alt:0#cond:2"), ("104", "sequence:0#alt:1#cond:2")])
        self.assertEqual(len({l["position"] // 100 for l in options}), 3)

        all_lines = get_mission(out, "1")["lines"]
        self.assertFalse(any(line["script"].endswith("Mission_100.json") for line in all_lines))
        self.assertEqual([line["script"].rsplit("/", 1)[-1] for line in all_lines if line["sentence_id"] == "14"], ["Mission_101.json"])

        with open_db(out) as conn:
            mutex = conn.execute("SELECT sentence_id, position, branch, chunk_id FROM lines WHERE script='Config/Level/Mission/1/Act/ActMutex.json' ORDER BY position").fetchall()
            self.assertEqual([(r[0], r[1], r[2]) for r in mutex], [("200", 0, ""), ("201", 10000, "sequence:0#alt:1#cond:1"), ("202", 10100, "sequence:0#alt:1#cond:2"), ("203", 20000, "")])
            self.assertEqual(len({r[3] for r in mutex}), 4)
            single = conn.execute("SELECT sentence_id, position, branch FROM lines WHERE script='Config/Level/Mission/1/Act/ActSingleSide.json' ORDER BY position").fetchall()
            self.assertEqual([tuple(r) for r in single], [("210", 0, ""), ("211", 100, ""), ("212", 10000, "")])
            sw = conn.execute("SELECT sentence_id, position, branch, chunk_id FROM lines WHERE script='Config/Level/Mission/1/Act/ActSwitchRand.json' ORDER BY position").fetchall()
            self.assertEqual([(r[0], r[1], r[2]) for r in sw], [("220", 0, "sequence:0#alt:0#cond:1"), ("221", 100, "sequence:0#alt:0#cond:2"), ("222", 10000, "sequence:0#alt:1#rand:1"), ("223", 10100, "sequence:0#alt:1#rand:2")])
            self.assertEqual(len({r[3] for r in sw}), 4)

    def test_timeline_calibration_and_verified_owners(self):
        missions = [
            (2022005, "Main"), (2022205, "Main"), (2022001, "Main"), (2022201, "Main"),
            (1032601, "Main"), (1032602, "Main"), (5014001, "Main"), (5034699, "Main"), (8024113, "Main"),
            (8021131, "Branch"), (1040504, "Main"), (1031901, "Main"), (1032201, "Main"), (1032202, "Main"), (1034204, "Main"),
        ]
        self.write("ExcelOutput/MainMission.json", [{"MainMissionID": mid, "Name": {"Hash": 1}, "Type": mtype} for mid, mtype in missions])
        all_sids = [
            222050302, 222010201, 222010202, 222010203,
            103260101, 103260201, 103260301, 103265001, 103265101, 103265201, 510010101, 536999001, 824113101,
            *[821130001 + slot * 100 for slot in range(10, 15)],
            145040102, 145040202, 103190102, 103190202, 103220102,
            103437001, 103437101, 103437201, 103437301, 103437401,
        ]
        texts = {**BASE_TEXTS, "201": "前一句", "202": "正中间", "203": "后一句"}
        self.write("TextMap/TextMapCHS.json", texts)
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": sid, "TalkSentenceText": {"Hash": sid % 100 + 200 if 222010201 <= sid <= 222010203 else 10}, "TextmapTalkSentenceName": {"Hash": 4}}
            for sid in all_sids
        ])
        perf_ds = [
            {"PerformanceID": 202220503, "PerformancePath": "Story/Discussion/Mission/2022205/DS202200503.json"},
            {"PerformanceID": 104050401, "PerformancePath": "Story/Discussion/Mission/1040504/DS104050401.json"},
            {"PerformanceID": 103190101, "PerformancePath": "Story/Discussion/Mission/1031901/DS103190101.json"},
            {"PerformanceID": 103220101, "PerformancePath": "Story/Discussion/Mission/1032201/DS103220101.json"},
        ]
        self.write("ExcelOutput/PerformanceDS.json", perf_ds)
        self.write("ExcelOutput/PerformanceC.json", [{"PerformanceID": 202220102, "PerformancePath": "Story/Mission/2022201/Story202220102.json"}])
        for empty in ("Story/Discussion/Mission/2022205/DS202200503.json", "Story/Mission/2022201/Story202220102.json"):
            self.write(empty, {"OnStartSequece": []})
        for tl in (
            "Story/Discussion/Mission/2022005/DS202200503.json",
            "Story/Discussion/Mission/1040504/DS104050401.json",
            "Story/Discussion/Mission/1031901/DS103190101.json",
            "Story/Discussion/Mission/1032201/DS103220101.json",
        ):
            self.write(tl, {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayTimeline"}]}]})
        self.simple_perf("Story/Discussion/Mission/2022001/DS202200102.json", [222010201, 222010203])

        for mid, anchors in ((1032601, (103260101, 103260301)), (1032602, (103265001, 103265201))):
            for slot, sid in zip((1, 3), anchors):
                self.write(f"Config/Level/Mission/{mid}/Act/Act{mid}{slot:02}.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayMultiVoiceTalk", "TalkSentenceID": sid}]}]})
            self.write(f"Story/Discussion/Mission/{mid}/DS{mid}02.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayTimeline"}]}]})
        for slot in range(1, 6):
            tasks = [{"$type": "RPG.GameCore.PlayTimeline"}] + ([{"$type": "RPG.GameCore.PerformanceTransition", "TalkSentenceID": 821130001 + (slot + 9) * 100}] if slot in (1, 2, 4) else [])
            self.write(f"Story/Discussion/Mission/8021131/DS8021131{slot:02}.json", {"OnStartSequece": [{"TaskList": tasks}]})
        for family, slot in [(10343, i) for i in range(70, 75)] + [(10344, 72)]:
            tasks = [{"$type": "RPG.GameCore.PlayTimeline"}] + ([{"$type": "RPG.GameCore.PerformanceTransition", "TalkSentenceID": family * 10000 + slot * 100 + 1}] if family == 10343 and slot in (70, 71, 73) else [])
            self.write(f"Story/Discussion/Mission/1034204/DS{family}00{slot}.json", {"OnStartSequece": [{"TaskList": tasks}]})

        out = Path(self.temp.name) / "timelines.db"
        import_data(self.root, out)

        d2022005 = get_mission(out, "2022005")
        self.assertEqual(([l["sentence_id"] for l in d2022005["lines"]], d2022005["lines"][0]["scene_label"]), (["222050302"], "比试结果 · 彦卿获胜"))
        self.assertEqual(get_mission(out, "2022205")["lines"], [])
        d2022001 = get_mission(out, "2022001")
        self.assertEqual(([l["text"] for l in d2022001["lines"]], [l["position"] for l in d2022001["lines"]]), (["前一句", "正中间", "后一句"], [0, 0.5, 1]))
        self.assertFalse(get_mission(out, "2022201")["lines"])
        self.assertEqual([l["sentence_id"] for l in get_mission(out, "8021131")["lines"]], ["821131001", "821131101", "821131201", "821131301"])
        self.assertEqual([l["sentence_id"] for l in get_mission(out, "1040504")["lines"]], ["145040102"])
        self.assertEqual([l["sentence_id"] for l in get_mission(out, "1031901")["lines"]], ["103190102"])
        self.assertFalse(get_mission(out, "1032201")["lines"])
        lines_34204 = get_mission(out, "1034204")["lines"]
        self.assertEqual([l["sentence_id"] for l in lines_34204], ["103437001", "103437101", "103437201", "103437301"])
        self.assertTrue(lines_34204[2]["script"].endswith("DS103430072.json::timeline-1034372"))
        with open_db(out) as conn:
            for sid, mid in ((103260201, "1032601"), (103265101, "1032602")):
                self.assertEqual(tuple(conn.execute("SELECT mission_id,script FROM lines WHERE sentence_id=?", (str(sid),)).fetchone()),
                                 (mid, f"Story/Discussion/Mission/{mid}/DS{mid}02.json::timeline-{str(sid)[:7]}"))
            for excluded in (510010101, 536999001, 824113101):
                self.assertIsNone(conn.execute("SELECT 1 FROM lines WHERE sentence_id=?", (str(excluded),)).fetchone())

    def test_early_gap_recovery_and_conflict_guards(self):
        missions = [
            {"MainMissionID": 1000101, "Name": {"Hash": 1}, "Type": "Main"},
            {"MainMissionID": 1000201, "Name": {"Hash": 1}, "Type": "Main"},
            {"MainMissionID": 1040534, "Name": {"Hash": 1}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1054400, "Name": {"Hash": 3}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1040403, "Name": {"Hash": 2}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1040412, "Name": {"Hash": 2}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1040208, "Name": {"Hash": 1}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1040209, "Name": {"Hash": 2}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1033101, "Name": {"Hash": 1}, "Type": "Main"},
            {"MainMissionID": 5000410, "Name": {"Hash": 1}, "Type": "Branch"},
        ]
        self.write("ExcelOutput/MainMission.json", missions)
        texts = dict(BASE_TEXTS)
        extra_sids = [
            100000005, 103310101, 103310201, 103311201,
            500050001, 500050002, 500050101,
            500050201, 500050202, 500050203, 500050204, 500050205, 500050301, 500050302,
            105002801, 105002802, 105002803, 105002899,
            144031701, 144031702, 144031703, 144031704,
            104031301, 104031302, 104031303, 104031305, 104031390,
        ]
        for prefix in (1000201, 1000301, 1000401, 1000501):
            for suffix in range(12, 41):
                sid = prefix * 100 + suffix
                extra_sids.append(sid)
                texts[str(sid)] = "这里是一个空间站。" if sid == 100020114 else f"对白{sid}"
        self.write("TextMap/TextMapCHS.json", texts)
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": sid, "TalkSentenceText": {"Hash": sid if str(sid) in texts else 10}, "TextmapTalkSentenceName": {"Hash": 4}}
            for sid in [10, *extra_sids]
        ])

        def anchored(prefix, timeline=True):
            tasks = ([{"$type": "RPG.GameCore.PlayTimeline"}] if timeline else []) + [{
                "$type": "RPG.GameCore.PlayOptionTalk",
                "OptionList": [{"TalkSentenceID": prefix * 100 + s, "TriggerCustomString": f"TalkSentence_{prefix}{s}"} for s in (13, 15, 18, 26, 30, 33, 39)],
            }]
            return {"OnStartSequece": [{"TaskList": tasks}, *[{"TaskList": [{"$type": "RPG.GameCore.WaitCustomString", "CustomString": {"Value": f"TalkSentence_{prefix}{s}"}}]} for s in (13, 15, 18, 21, 26, 30, 33, 36, 39)]]}

        self.write("Story/Mission/1000101/intro.json", anchored(1000201))
        self.write("Config/Level/Mission/1000101/Act/shadow.json", anchored(1000201, False))
        self.write("Story/Mission/1000101/plain.json", anchored(1000301, False))
        self.write("Story/Mission/1000101/shared.json", anchored(1000401))
        self.write("Story/Mission/1000201/shared.json", anchored(1000401))
        self.write("Story/Mission/1000101/ambiguous-a.json", anchored(1000501))
        self.write("Story/Mission/1000101/ambiguous-b.json", anchored(1000501))

        self.simple_perf("Story/Discussion/Mission/1040534/DS104053409.json", [105002801, 105002803])
        self.write("Config/Level/Mission/1054400/Mission_105440014.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 105002899}]}]}]})
        self.simple_perf("Config/Level/Mission/1040403/Act/Act104040317.json", [144031702])
        self.write("Config/Level/Mission/1040412/Mission_104041212.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 144031703}]}]}]})
        self.write("Config/Level/Mission/1040208/Mission_104020801.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 104031301}, {"TalkSentenceID": 104031303}]}]}]})
        self.simple_perf("Story/Discussion/Mission/1040209/DS104020901.json", [104031390])
        self.write("Config/Level/Mission/8014108/Mission_801410801.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 10}]}]}]})
        self.simple_perf("Story/Mission/1033101/Story103310101.json", [103310101])
        self.simple_perf("Story/Mission/1033101/Story103310102.json", [103310201])
        self.write("Story/Mission/1033101/Story103310112.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PredicateTaskList", "SuccessTaskList": [{"$type": "RPG.GameCore.PerformanceEndBlackText", "TalkSentenceID": 100000005}]},
            {"$type": "RPG.GameCore.PlayTimeline"},
        ]}]})
        self.write("Story/Discussion/Mission/5000410/DS500041001.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayTimeline"}, {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 500050002}]}]}]})
        self.write("Story/Discussion/Mission/5000410/DS500041002.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayTimeline"}]}]})
        self.write("Story/Discussion/Mission/5000410/DS500041003.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayTimeline"},
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": sid} for sid in (500050202, 500050203, 500050204, 500050205)]},
            {"$type": "RPG.GameCore.PredicateTaskList", "SuccessTaskList": [{"$type": "RPG.GameCore.PlayOptionTalk", "OptionList": [{"TalkSentenceID": 500050302, "TriggerCustomString": "opt"}]}]},
        ]}]})
        self.write("Story/Discussion/Mission/5000410/DS500041004.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayTimeline"}, {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 500050301}]}]}]})

        out = Path(self.temp.name) / "recovery-guards.db"
        stats = import_data(self.root, out)
        self.assertNotIn("1050028", stats["timeline_conflicts"])
        self.assertNotIn("1440317", stats["timeline_conflicts"])
        self.assertEqual(stats["unverified_timeline_groups"]["1040313"]["sentences"], 1)
        self.assertEqual(lexical_search(out, "这里是一个空间站")[0]["mission_id"], "1000101")
        with open_db(out) as conn:
            self.assertEqual(tuple(conn.execute("SELECT mission_id,script,sequence,branch,kind FROM lines WHERE sentence_id='100020114'").fetchone()),
                             ("1000101", "Story/Mission/1000101/intro.json", 1, "TalkSentence_100020113", "dialogue"))
            for sid, seq, br in ((100020116, 2, "TalkSentence_100020115"), (100020127, 5, "TalkSentence_100020126"), (100020131, 6, "TalkSentence_100020130"), (100020134, 7, "TalkSentence_100020133")):
                self.assertEqual(tuple(conn.execute("SELECT sequence,branch FROM lines WHERE sentence_id=?", (str(sid),)).fetchone()), (seq, br))
            for excluded in (100020112, 100020140, 100030114, 100040114, 100050114, 104031305):
                self.assertIsNone(conn.execute("SELECT 1 FROM lines WHERE sentence_id=?", (str(excluded),)).fetchone())
            for sid, owner in (("105002802", "1040534"), ("144031701", "1040403"), ("144031704", "1040412"), ("104031302", "1040208"), ("500050001", "5000410"), ("500050101", "5000410"), ("500050201", "5000410")):
                self.assertEqual(conn.execute("SELECT mission_id FROM lines WHERE sentence_id=?", (sid,)).fetchone()[0], owner)
            self.assertIsNone(conn.execute("SELECT 1 FROM missions WHERE id='8014108'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM lines WHERE mission_id='1033101' AND sentence_id='100000005'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM lines WHERE mission_id='1033101' AND sentence_id='103311201'").fetchone())

    def test_mission_dag_order_and_edge_cases(self):
        # 1) 1054706-style DAG + FixedValue bubble + line.id/chunk preservation.
        self.add_mission_with_sentences(1054706, [
            (157063001, "人类的一分钟"), (157062802, "再走近些吧"), (157064201, "只是一个回答"), (157063201, "我要怎么回去呢"), (157063401, "这边"),
        ])
        s30, s28, s42, s32, s34 = (
            "Story/Discussion/Mission/1054706/DS105470630.json",
            "Recovered/Mission/1054706/1570628.json",
            "Story/Discussion/Mission/1054706/DS105470642.json",
            "Story/Discussion/Mission/1054706/DS105470632.json",
            "Story/Mission/1054706/Story105470634.json",
        )
        for rel, sid in ((s30, 157063001), (s42, 157064201), (s32, 157063201), (s34, 157063401)):
            self.simple_perf(rel, [sid])
        self.write("ExcelOutput/PerformanceDS.json", [
            {"PerformanceID": 105470630, "PerformancePath": s30}, {"PerformanceID": 105470642, "PerformancePath": s42}, {"PerformanceID": 105470632, "PerformancePath": s32},
        ])
        self.write("ExcelOutput/PerformanceD.json", [{"PerformanceID": 105470634, "PerformancePath": s34}])

        without_dag = Path(self.temp.name) / "without-dag.db"
        import_data(self.root, without_dag)

        # 2) Shared persistent group EnableCondition(Finish) waits for MultiSequence join.
        self.add_mission_with_sentences(1034109, [(3410901, "开头"), (3410902, "分支一"), (3410903, "分支二"), (3410950, "共同完成后")])
        for folder, prefix, pid, sid in (
            ("Config/Level/Mission/1034109/Act", "Act", 103410901, 3410901),
            ("Story/Discussion/Mission/1034109", "DS", 103410902, 3410902),
            ("Story/Discussion/Mission/1034109", "DS", 103410904, 3410903),
            ("Config/Level/Mission/1034109/Act", "Act", 103410950, 3410950),
        ):
            self.simple_perf(f"{folder}/{prefix}{pid}.json", [sid])

        # 3) AnySequence with multiple predecessors keeps both win/lose branches, ignores PlayVideo, honors VERIFIED_SCENE_ORDER.
        self.add_mission_with_sentences(2022005, [(222050201, "比试前"), (222050601, "比试中"), (222050302, "彦卿赢"), (222050402, "云璃赢"), (222050501, "收尾")])
        for sc in ("Story/Discussion/Mission/2022005/DS202200502.json", "Story/Discussion/Mission/2022005/DS202200503.json",
                   "Story/Discussion/Mission/2022005/DS202200504.json", "Story/Mission/2022005/Story202200505.json", "Config/Level/Mission/2022005/Act/Act202200506.json"):
            self.write(sc, {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayTimeline"}]}]})

        self.write("Config/Level/Mission/1054706/MissionInfo_1054706.partial.json", {
            "StartSubMissionList": [105470615],
            "SubMissionList": [
                {"ID": 105470615, "TakeType": "Auto", "LevelFloorID": 10000000, "GroupIDList": [10]},
                {"ID": 105470634, "TakeType": "AnySequence", "TakeParamIntList": [105470615], "LevelFloorID": 10000000, "GroupIDList": [11]},
                {"ID": 105470635, "TakeType": "AnySequence", "TakeParamIntList": [105470634], "LevelFloorID": 10000000, "GroupIDList": [12]},
            ],
        })
        self.write("Config/Level/GroupGraph/F10000000/Group_F10000000_G10.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "DS", "PerformanceID": 105470630},
        ]}]})
        self.write("Config/Level/GroupGraph/F10000000/Group_F10000000_G11.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayMissionTalk", "StartSentenceID": {"FixedValue": {"Value": 157062800}}, "EndSentenceID": {"FixedValue": {"Value": 157062805}}},
            {"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "DS", "PerformanceID": 105470642},
        ]}]})
        self.write("Config/Level/GroupGraph/F10000000/Group_F10000000_G12.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "DS", "PerformanceID": 105470632},
            {"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "D", "PerformanceID": 105470634},
        ]}]})

        self.write("Config/Level/Mission/1034109/MissionInfo_1034109.json", {
            "StartSubMissionList": [103410901],
            "SubMissionList": [
                {"ID": 103410901, "TakeType": "Auto", "LevelFloorID": 10304001, "GroupIDList": [35]},
                {"ID": 103410902, "TakeType": "AnySequence", "TakeParamIntList": [103410901], "LevelFloorID": 10304001, "GroupIDList": [36, 71]},
                {"ID": 103410904, "TakeType": "AnySequence", "TakeParamIntList": [103410901], "LevelFloorID": 10304001, "GroupIDList": [72, 71]},
                {"ID": 103410905, "TakeType": "MultiSequence", "TakeParamIntList": [103410902, 103410904], "LevelFloorID": 10304001, "GroupIDList": [37, 71]},
            ],
        })
        for gid, pid, ptype in ((35, 103410901, "A"), (36, 103410902, "DS"), (72, 103410904, "DS"), (37, 103410905, "E")):
            self.write(f"Config/Level/GroupGraph/F10304001/Group_F10304001_G{gid}.json", {"OnStartSequece": [{"TaskList": [
                {"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": ptype, "PerformanceID": pid},
            ]}]})
        self.write("Config/LevelOutput/SharedRuntimeGroup/Groups_P10304_F10304001/LevelGroup_P10304_F10304001_G71.json", {"PropList": [{"Dialog": {
            "EnableCondition": {"Conditions": [{"Type": "SubMission", "Phase": "Finish", "ID": 103410904}]},
            "LevelGraph": "Config/Level/PropDialogue/P10304/F10304001_G71/DialogueMain_G71.json",
        }}]})
        self.write("Config/Level/PropDialogue/P10304/F10304001_G71/DialogueMain_G71.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "E", "PerformanceID": 103410950},
        ]}]})

        self.write("Config/Level/Mission/2022005/MissionInfo_2022005.json", {
            "StartSubMissionList": [202200502],
            "SubMissionList": [
                {"ID": 202200502, "TakeType": "Auto", "LevelFloorID": 40235001, "GroupIDList": [17]},
                {"ID": 202200506, "TakeType": "AnySequence", "TakeParamIntList": [202200502]},
                {"ID": 202200507, "TakeType": "AnySequence", "TakeParamIntList": [202200502]},
                {"ID": 202200503, "TakeType": "AnySequence", "TakeParamIntList": [202200506, 202200507], "LevelFloorID": 40235001, "GroupIDList": [18]},
                {"ID": 202200504, "TakeType": "AnySequence", "TakeParamIntList": [202200503], "LevelFloorID": 40235001, "GroupIDList": [19]},
                {"ID": 202200505, "TakeType": "AnySequence", "TakeParamIntList": [202200504], "LevelFloorID": 40235001, "GroupIDList": [20]},
            ],
        })
        self.write("Config/Level/GroupGraph/F40235001/Group_F40235001_G17.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "D", "PerformanceID": 202200502}]}]})
        self.write("Config/Level/GroupGraph/F40235001/Group_F40235001_G18.json", {"OnStartSequece": [{"TaskList": [{
            "$type": "RPG.GameCore.PredicateTaskList",
            "Predicate": {"$type": "RPG.GameCore.ByCompareSubMissionState", "SubMissionID": 202200506, "SubMissionState": "Finish"},
            "SuccessTaskList": [{"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "D", "PerformanceID": 202200503}],
            "FailedTaskList": [{"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "D", "PerformanceID": 202200504}],
        }]}]})
        self.write("Config/Level/GroupGraph/F40235001/Group_F40235001_G19.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "PlayVideo", "PerformanceID": 202200506}]}]})
        self.write("Config/Level/GroupGraph/F40235001/Group_F40235001_G20.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "D", "PerformanceID": 202200505}]}]})

        with_dag = Path(self.temp.name) / "with-dag.db"
        import_data(self.root, with_dag)

        detail = get_mission(with_dag, "1054706")
        self.assertEqual([line["script"] for line in detail["lines"]], [s30, s28, s42, s32, s34])
        self.assertEqual(detail["ordered_scripts"], sorted([s30, s28, s42, s32, s34]))
        with open_db(without_dag) as a, open_db(with_dag) as b:
            self.assertEqual(
                list(a.execute("SELECT id FROM lines WHERE mission_id='1054706' ORDER BY id")),
                list(b.execute("SELECT id FROM lines WHERE mission_id='1054706' ORDER BY id")),
            )
            self.assertEqual(
                list(a.execute("SELECT content_hash FROM chunks WHERE mission_id='1054706' ORDER BY id")),
                list(b.execute("SELECT content_hash FROM chunks WHERE mission_id='1054706' ORDER BY id")),
            )

        self.assertEqual([l["sentence_id"] for l in get_mission(with_dag, "1034109")["lines"]], ["3410901", "3410902", "3410903", "3410950"])
        contest = get_mission(with_dag, "2022005")
        self.assertEqual([l["sentence_id"] for l in contest["lines"]], ["222050201", "222050601", "222050302", "222050402", "222050501"])
        self.assertIn("Config/Level/Mission/2022005/Act/Act202200506.json::timeline-2220506", contest["ordered_scripts"])

    def test_mission_dag_raid_zip_and_roundtrip(self):
        self.add_mission_with_sentences(1054706, [(157060101, "幻月内关卡"), (157060901, "回到外层任务")])
        s01, s09 = "Story/Discussion/Mission/1054706/DS105470601.json", "Story/Discussion/Mission/1054706/DS105470609.json"
        self.simple_perf(s01, [157060101])
        self.simple_perf(s09, [157060901])

        base_db, clone_db = Path(self.temp.name) / "no-info.db", Path(self.temp.name) / "clone.db"
        import_data(self.root, base_db)
        import_data(self.root, clone_db)

        self.write("ExcelOutput/RaidConfig.json", [{"RaidID": 40572, "MainMissionIDList": [1054707]}])
        self.write("Config/Level/Mission/1054706/MissionInfo_1054706.json", {
            "StartSubMissionList": [105470601],
            "SubMissionList": [
                {"ID": 105470601, "TakeType": "Auto", "FinishType": "RaidFinishedAndTransfer", "ParamInt1": 40572},
                {"ID": 105470602, "TakeType": "AnySequence", "TakeParamIntList": [105470601], "LevelFloorID": 10501001, "GroupIDList": [20]},
            ],
        })
        self.write("Config/Level/Mission/1054707/MissionInfo_1054707.json", {
            "StartSubMissionList": [105470701],
            "SubMissionList": [{"ID": 105470701, "TakeType": "Auto", "LevelFloorID": 40572001, "GroupIDList": [1]}],
        })
        self.write("Config/Level/GroupGraph/F40572001/Group_F40572001_G1.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "DS", "PerformanceID": 105470601}]}]})
        self.write("Config/Level/GroupGraph/F10501001/Group_F10501001_G20.json", {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.TriggerPerformance", "PerformanceType": "DS", "PerformanceID": 105470609}]}]})

        source_zip = Path(self.temp.name) / "source.zip"
        with zipfile.ZipFile(source_zip, "w") as archive:
            for item in self.root.rglob("*"):
                if item.is_file():
                    archive.write(item, f"TurnBasedGameData-test/{item.relative_to(self.root).as_posix()}")

        payload_gz = Path(self.temp.name) / "script_order.json.gz"
        update_report = update_script_order(base_db, source=source_zip, export_file=payload_gz)
        self.assertTrue(update_report["audit"]["ok"])
        self.assertEqual((update_report["order"]["missions"], update_report["order"]["scripts"]), (1, 2))
        import_report = update_script_order(clone_db, import_file=payload_gz)
        self.assertTrue(import_report["audit"]["ok"])
        self.assertEqual([l["script"] for l in get_mission(base_db, "1054706")["lines"]], [s01, s09])
        self.assertEqual(get_mission(clone_db, "1054706")["lines"], get_mission(base_db, "1054706")["lines"])


if __name__ == "__main__":
    unittest.main()
