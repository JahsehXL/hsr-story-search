import json
import tempfile
import unittest
from pathlib import Path

from backend.data import VERIFIED_SCENES, get_catalog, get_mission, import_data, normalize_text, open_db
from backend.search import _extract_content_terms, _score_fields, get_chunks, lexical_search, semantic_context_bonus


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

    def test_candidate_preserves_embedding_input_when_metadata_is_added(self):
        self.write("ExcelOutput/MainMission.json", [{"MainMissionID": 1, "Type": "Main"}])
        baseline = Path(self.temp.name) / "without-metadata.db"
        import_data(self.root, baseline)
        with open_db(baseline, readonly=False) as conn:
            conn.execute("INSERT INTO info VALUES('embedding','{}')")
            original = conn.execute("SELECT input_text,content_hash FROM chunks ORDER BY id").fetchall()
            self.assertTrue(original[0]["input_text"].startswith("任务 1\n"))
        self.write("ExcelOutput/MainMission.json", [
            {"MainMissionID": 1, "Name": {"Hash": 1}, "WorldID": 3, "ChapterID": 2, "Type": "Main"},
        ])
        candidate = Path(self.temp.name) / "new-metadata.db"
        stats = import_data(self.root, candidate, baseline=baseline)
        with open_db(candidate) as conn:
            self.assertEqual(original, conn.execute("SELECT input_text,content_hash FROM chunks ORDER BY id").fetchall())
            self.assertEqual(conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH '匹诺'").fetchone()[0], len(original))
        self.assertEqual(stats["preserved_embedding_inputs"], len(original))

        # Meaningful context changes must invalidate the input even when dialogue survives verbatim.
        self.write("ExcelOutput/MainMission.json", [{"MainMissionID": 1, "Name": {"Hash": 3}, "Type": "Main"}])
        with open_db(self.db, readonly=False) as conn:
            conn.execute("INSERT INTO info VALUES('embedding','{}')")
        changed = Path(self.temp.name) / "changed-metadata.db"
        self.assertEqual(import_data(self.root, changed, baseline=self.db)["preserved_embedding_inputs"], 0)

    def test_verified_timeline_keeps_cross_release_conflict_out_of_the_reader(self):
        correct = "Story/Discussion/Mission/2022005/DS202200503.json"
        decoy = "Story/Discussion/Mission/2022205/DS202220503.json"
        self.write("ExcelOutput/MainMission.json", [
            {"MainMissionID": identifier, "Name": {"Hash": 1}, "Type": "Main"}
            for identifier in (2022005, 2022205)
        ])
        self.write("ExcelOutput/PerformanceDS.json", [{"PerformanceID": 202220503, "PerformancePath": decoy}])
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": 222050302, "TalkSentenceText": {"Hash": 10}},
        ])
        self.write(correct, {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayTimeline"}]}]})
        self.write(decoy, {"OnStartSequece": []})
        candidate = Path(self.temp.name) / "verified-timeline.db"
        import_data(self.root, candidate)

        detail = get_mission(candidate, "2022005")
        self.assertEqual([line["sentence_id"] for line in detail["lines"]], ["222050302"])
        self.assertEqual(detail["lines"][0]["scene_label"], "比试结果 · 彦卿获胜")
        self.assertEqual(get_mission(candidate, "2022205")["lines"], [])

    def test_reader_collapses_ordered_script_mirror_and_resolves_legacy_anchor(self):
        def transcript(*sentence_ids):
            return {"OnStartSequece": [{"TaskList": [{
                "$type": "RPG.GameCore.PlayAndWaitSimpleTalk",
                "SimpleTalkList": [{"TalkSentenceID": identifier} for identifier in sentence_ids],
            }]}]}

        mirror = "Story/Mission/1/Story1.json"
        self.write(mirror, transcript(10))
        self.write("Config/Level/Mission/1/Act/Act2.json", transcript(10, 16))
        different = "Story/Mission/1/Story2.json"
        self.write(different, transcript(10, 13))
        output = Path(self.temp.name) / "mirrors.db"
        import_data(self.root, output)

        legacy_line = get_mission(output, "1", script=mirror)["lines"][0]
        detail = get_mission(output, "1")
        self.assertFalse(any(line["script"] == mirror for line in detail["lines"]))
        self.assertEqual([line["sentence_id"] for line in detail["lines"] if line["script"] == different], ["10", "13"])
        anchor = get_mission(output, "1", line_id=legacy_line["id"], limit=1)
        self.assertEqual(anchor["lines"][0]["id"], anchor["anchor_line_id"])
        self.assertEqual(anchor["lines"][0]["script"], "Config/Level/Mission/1/Act/Act1.json")
        next_page = get_mission(output, "1", offset=anchor["offset"] + 1)
        self.assertEqual(anchor["total"], detail["total"])
        self.assertEqual(anchor["lines"] + next_page["lines"], detail["lines"][anchor["offset"]:])

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

    def test_fuzzy_search_ranks_fragments_in_the_same_dialogue_line(self):
        text_map = "TextMap/TextMapCHS.json"
        texts = json.loads((self.root / text_map).read_text(encoding="utf-8"))
        texts["11"] = "你是幕后黑手吧。"
        texts["13"] = "噫，别再说了！在恐怖片里，出现的这人肯定是幕后黑手。"
        self.write(text_map, texts)
        output = Path(self.temp.name) / "fuzzy.db"
        import_data(self.root, output)

        matches = lexical_search(output, "恐怖片才会出现的幕后黑手", allow_fuzzy=True)
        hit = matches[0]
        self.assertEqual(hit["speaker"], "三月七")
        self.assertEqual(hit["match_type"], "fuzzy")
        self.assertEqual(hit["branch"], "stay")
        detail = get_mission(output, hit["mission_id"], line_id=hit["line_id"])
        matched_line = next(line for line in detail["lines"] if line["id"] == hit["line_id"])
        self.assertEqual(matched_line["sentence_id"], "13")
        self.assertEqual(matched_line["text"], texts["13"])

    def test_scene_description_prefers_participants_and_anchors_on_topic_line(self):
        text_map = "TextMap/TextMapCHS.json"
        texts = json.loads((self.root / text_map).read_text(encoding="utf-8"))
        texts.update({"10": "不是，你还真有隐藏的力量啊？！", "13": "说到丹恒，我也有隐藏的力量。",
                      "15": "丹恒", "16": "说来话长，三月。是我。"})
        self.write(text_map, texts)
        output = Path(self.temp.name) / "scene.db"
        import_data(self.root, output)

        query = "三月七让丹恒用出隐藏的力量"
        for allow_fuzzy in (False, True):
            with self.subTest(allow_fuzzy=allow_fuzzy):
                hit = lexical_search(output, query, allow_fuzzy=allow_fuzzy)[0]
                self.assertEqual(hit["speaker"], "三月七")
                detail = get_mission(output, hit["mission_id"], line_id=hit["line_id"])
                matched_line = next(line for line in detail["lines"] if line["id"] == hit["line_id"])
                self.assertEqual(matched_line["text"], texts["10"])

    def test_verified_bronya_scene_is_indexed_without_script_sentence_references(self):
        mission_id, script, sentence_ids = VERIFIED_SCENES[0]
        self.write("ExcelOutput/MainMission.json", [
            {"MainMissionID": int(mission_id), "Name": {"Hash": 1}, "Type": "Main"},
        ])
        text_map = "TextMap/TextMapCHS.json"
        texts = json.loads((self.root / text_map).read_text(encoding="utf-8"))
        texts["15"] = "丹恒"
        texts.update({str(identifier): f"演出对白 {index}" for index, identifier in enumerate(sentence_ids)})
        texts["201110601"] = "喂，丹恒，快把你隐藏的力量用出来呀！"
        texts["201110602"] = "…你先吧。"
        self.write(text_map, texts)
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": identifier, "TalkSentenceText": {"Hash": identifier},
             "TextmapTalkSentenceName": {"Hash": 15 if identifier == 201110602 else 4}}
            for identifier in sentence_ids
        ])
        output = Path(self.temp.name) / "recovered.db"
        stats = import_data(self.root, output)
        self.assertEqual(stats["verified_scene_lines"], len(sentence_ids))
        hit = lexical_search(output, "三月七让丹恒用出隐藏的力量", allow_fuzzy=True)[0]
        self.assertEqual((hit["mission_id"], hit["script"], hit["sentence_id"]), (mission_id, script, "201110601"))
        detail = get_mission(output, mission_id, line_id=hit["line_id"])
        found = next(index for index, line in enumerate(detail["lines"]) if line["id"] == hit["line_id"])
        self.assertEqual(detail["lines"][found + 1]["text"], texts["201110602"])

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

    def test_version_descending_and_unspaced_natural_query(self):
        main_missions = json.loads((self.root / "ExcelOutput/MainMission.json").read_text(encoding="utf-8"))
        main_missions.append({"MainMissionID": 1054701, "Name": {"Hash": 200}, "WorldID": 3, "ChapterID": 2, "Type": "Main"})
        self.write("ExcelOutput/MainMission.json", main_missions)

        texts = json.loads((self.root / "TextMap/TextMapCHS.json").read_text(encoding="utf-8"))
        texts.update({
            "200": "世界图像的时代",
            "201": "星期日",
            "202": "知更鸟",
            "203": "我们关于笼中鸟的争论该结束了。",
            "204": "这只星穹列车上的小鸟终于飞起来了。",
        })
        self.write("TextMap/TextMapCHS.json", texts)

        sentences = json.loads((self.root / "ExcelOutput/TalkSentenceConfig.json").read_text(encoding="utf-8"))
        sentences.extend([
            {"TalkSentenceID": 157010101, "TalkSentenceText": {"Hash": 203}, "TextmapTalkSentenceName": {"Hash": 201}},
            {"TalkSentenceID": 157010102, "TalkSentenceText": {"Hash": 204}, "TextmapTalkSentenceName": {"Hash": 202}},
        ])
        self.write("ExcelOutput/TalkSentenceConfig.json", sentences)
        self.write("Story/Mission/1054701/Story105470101.json", {"OnStartSequece": [{"TaskList": [{
            "$type": "RPG.GameCore.PlaySimpleTalk",
            "SimpleTalkList": [{"TalkSentenceID": sid} for sid in (157010101, 157010102)],
        }]}]})

        db2 = Path(self.temp.name) / "timeline.db"
        import_data(self.root, db2)
        with open_db(db2, readonly=False) as conn:
            conn.execute("UPDATE missions SET version='1.0' WHERE id='1'")
            conn.execute("UPDATE missions SET version='4.7' WHERE id='1054701'")

        catalog = get_catalog(db2)
        self.assertEqual(catalog["versions"], ["4.7", "1.0"])

        ordered = lexical_search(db2, "星穹列车")
        self.assertEqual([item["version"] for item in ordered], ["4.7", "1.0"])

        by_version = lexical_search(db2, "", {"version": "4.7"})
        self.assertTrue(by_version)
        self.assertEqual(by_version[0]["mission_id"], "1054701")

        natural = lexical_search(db2, "星期日和知更鸟关于笼中鸟的争论")
        self.assertTrue(natural)
        self.assertEqual(natural[0]["mission_id"], "1054701")

    def test_timeline_uses_each_missions_observed_scene_numbers(self):
        self.write("ExcelOutput/MainMission.json", [
            {"MainMissionID": mid, "Type": "Main"}
            for mid in (1032601, 1032602, 5014001, 5034699, 8024113)
        ])
        ids = (103260101, 103260201, 103260301, 103265001, 103265101, 103265201,
               510010101, 536999001, 821131101)
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": sid, "TalkSentenceText": {"Hash": 10}} for sid in ids
        ])
        for mid, anchors in [(1032601, (103260101, 103260301)), (1032602, (103265001, 103265201))]:
            for slot, sid in zip((1, 3), anchors):
                self.write(f"Config/Level/Mission/{mid}/Act/Act{mid}{slot:02}.json", {"OnStartSequece": [{"TaskList": [{
                    "$type": "RPG.GameCore.PlayMultiVoiceTalk", "TalkSentenceID": sid,
                }]}]})
            self.write(f"Story/Discussion/Mission/{mid}/DS{mid}02.json", {"OnStartSequece": [{"TaskList": [{
                "$type": "RPG.GameCore.PlayTimeline",
            }]}]})
        out = Path(self.temp.name) / "observed-numbering.db"
        import_data(self.root, out)
        with open_db(out) as conn:
            for sid, mid in [(103260201, "1032601"), (103265101, "1032602")]:
                row = conn.execute("SELECT mission_id,script FROM lines WHERE sentence_id=?", (str(sid),)).fetchone()
                self.assertEqual(tuple(row), (mid, f"Story/Discussion/Mission/{mid}/DS{mid}02.json::timeline-{str(sid)[:7]}"))
            for sid in ids[-3:]:
                self.assertIsNone(conn.execute("SELECT 1 FROM lines WHERE sentence_id=?", (str(sid),)).fetchone())

    def test_transition_text_is_only_imported_when_enabled(self):
        self.write("Story/Mission/1/transition.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PerformanceTransition", "TalkSentenceID": 14, "TextEnabled": True},
            {"$type": "RPG.GameCore.PerformanceTransition", "TalkSentenceID": 13, "TextEnabled": False},
        ]}]})
        out = Path(self.temp.name) / "transition.db"
        import_data(self.root, out)
        self.assertEqual([line["sentence_id"] for line in get_mission(out, "1", script="Story/Mission/1/transition.json")["lines"]], ["14"])

    def test_side_story_timeline_requires_a_dense_observed_range(self):
        self.write("ExcelOutput/MainMission.json", [{"MainMissionID": 8021131, "Type": "Branch"}])
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": 821130001 + slot * 100, "TalkSentenceText": {"Hash": 10}}
            for slot in range(10, 15)
        ])
        for slot in range(1, 6):
            tasks = [{"$type": "RPG.GameCore.PlayTimeline"}]
            if slot in (1, 2, 4):
                tasks.append({"$type": "RPG.GameCore.PerformanceTransition", "TalkSentenceID": 821130001 + (slot + 9) * 100})
            self.write(f"Story/Discussion/Mission/8021131/DS8021131{slot:02}.json",
                       {"OnStartSequece": [{"TaskList": tasks}]})
        out = Path(self.temp.name) / "side-story.db"
        import_data(self.root, out)
        self.assertEqual([line["sentence_id"] for line in get_mission(out, "8021131")["lines"]],
                         ["821131001", "821131101", "821131201", "821131301"])

    def test_packed_main_story_performance_uses_its_full_asset_path(self):
        path = "Story/Discussion/Mission/1040504/DS104050401.json"
        self.write("ExcelOutput/MainMission.json", [{"MainMissionID": 1040504, "Type": "Main"}])
        self.write("ExcelOutput/PerformanceDS.json", [{"PerformanceID": 104050401, "PerformancePath": path}])
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": sid, "TalkSentenceText": {"Hash": 10}} for sid in (145040102, 145040202)
        ])
        self.write(path, {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayTimeline"}]}]})
        out = Path(self.temp.name) / "packed-main-story.db"
        import_data(self.root, out)
        self.assertEqual([line["sentence_id"] for line in get_mission(out, "1040504")["lines"]], ["145040102"])

    def test_legacy_main_story_requires_a_unique_family_and_an_actual_performance(self):
        self.write("ExcelOutput/MainMission.json", [
            {"MainMissionID": mid, "Type": "Main"} for mid in (1031901, 1032201, 1032202)
        ])
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": sid, "TalkSentenceText": {"Hash": 10}}
            for sid in (103190102, 103190202, 103220102)
        ])
        performances = []
        for mid in (1031901, 1032201):
            path = f"Story/Discussion/Mission/{mid}/DS{mid}01.json"
            self.write(path, {"OnStartSequece": [{"TaskList": [{"$type": "RPG.GameCore.PlayTimeline"}]}]})
            performances.append({"PerformanceID": mid * 100 + 1, "PerformancePath": path})
        self.write("ExcelOutput/PerformanceDS.json", performances)
        out = Path(self.temp.name) / "legacy-performance.db"
        import_data(self.root, out)
        self.assertEqual([line["sentence_id"] for line in get_mission(out, "1031901")["lines"]], ["103190102"])
        self.assertFalse(get_mission(out, "1032201")["lines"])

    def test_renumbered_performance_learns_only_within_its_observed_asset_range(self):
        self.write("ExcelOutput/MainMission.json", [{"MainMissionID": 1034204, "Type": "Main"}])
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": sid, "TalkSentenceText": {"Hash": 10}}
            for sid in (103437001, 103437101, 103437201, 103437301, 103437401)
        ])
        for family, slot in [(10343, i) for i in range(70, 75)] + [(10344, 72)]:
            tasks = [{"$type": "RPG.GameCore.PlayTimeline"}]
            if family == 10343 and slot in (70, 71, 73):
                tasks.append({"$type": "RPG.GameCore.PerformanceTransition", "TalkSentenceID": family * 10000 + slot * 100 + 1})
            self.write(f"Story/Discussion/Mission/1034204/DS{family}00{slot}.json",
                       {"OnStartSequece": [{"TaskList": tasks}]})
        out = Path(self.temp.name) / "renumbered-performance.db"
        import_data(self.root, out)
        lines = get_mission(out, "1034204")["lines"]
        self.assertEqual([line["sentence_id"] for line in lines], ["103437001", "103437101", "103437201", "103437301"])
        self.assertTrue(lines[2]["script"].endswith("DS103430072.json::timeline-1034372"))

    def test_mission_graph_and_waited_transition_captions_follow_source_order(self):
        def signal(task, value):
            return {"$type": f"RPG.GameCore.{task}", "CustomString": {"Value": value}}

        path = "Config/Level/Mission/1/Mission_101.json"
        self.write(path, {"OnStartSequece": [
            {"TaskList": [
                {"$type": "RPG.GameCore.PlayScreenTransfer", "TextEnabled": True, "TalkSentenceID": 10},
                signal("TriggerCustomString", "caption"),
                signal("WaitCustomString", "caption_done"),
                {"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 16}]},
            ]},
            {"TaskList": [
                signal("WaitCustomString", "caption"),
                {"$type": "RPG.GameCore.PerformanceTransition", "TextEnabled": True, "TalkSentenceID": 14},
                signal("TriggerCustomString", "caption_done"),
            ]},
        ]})
        out = Path(self.temp.name) / "mission-captions.db"
        import_data(self.root, out)
        lines = get_mission(out, "1", script=path)["lines"]
        self.assertEqual([(line["sequence"], line["sentence_id"]) for line in lines], [(0, "10"), (0, "14"), (0, "16")])

    def test_early_timeline_recovers_only_bounded_unique_mission_gaps(self):
        missions = json.loads((self.root / "ExcelOutput/MainMission.json").read_text(encoding="utf-8"))
        missions.extend({"MainMissionID": mid, "Name": {"Hash": 1}, "Type": "Main"}
                        for mid in (1000101, 1000201))
        self.write("ExcelOutput/MainMission.json", missions)
        texts = json.loads((self.root / "TextMap/TextMapCHS.json").read_text(encoding="utf-8"))
        sentences = json.loads((self.root / "ExcelOutput/TalkSentenceConfig.json").read_text(encoding="utf-8"))
        for prefix in (1000201, 1000301, 1000401, 1000501):
            for suffix in range(12, 41):
                sid = prefix * 100 + suffix
                texts[str(sid)] = "这里是一个空间站。" if sid == 100020114 else f"对白{sid}"
                sentences.append({"TalkSentenceID": sid, "TalkSentenceText": {"Hash": sid},
                                  "TextmapTalkSentenceName": {"Hash": 4}})
        self.write("TextMap/TextMapCHS.json", texts)
        self.write("ExcelOutput/TalkSentenceConfig.json", sentences)

        def anchored_script(prefix, timeline=True):
            tasks = [{"$type": "RPG.GameCore.PlayTimeline"}] if timeline else []
            tasks.append({"$type": "RPG.GameCore.PlayOptionTalk", "OptionList": [
                {"TalkSentenceID": prefix * 100 + suffix, "TriggerCustomString": f"TalkSentence_{prefix}{suffix}"}
                for suffix in (13, 15, 18, 26, 30, 33, 39)
            ]})
            branches = [{"TaskList": [
                {"$type": "RPG.GameCore.WaitCustomString", "CustomString": {"Value": f"TalkSentence_{prefix}{suffix}"}}
            ]} for suffix in (13, 15, 18, 21, 26, 30, 33, 36, 39)]
            return {"OnStartSequece": [{"TaskList": tasks}, *branches]}

        self.write("Story/Mission/1000101/intro.json", anchored_script(1000201))
        self.write("Config/Level/Mission/1000101/Act/shadow.json", anchored_script(1000201, timeline=False))
        self.write("Story/Mission/1000101/plain.json", anchored_script(1000301, timeline=False))
        self.write("Story/Mission/1000101/shared.json", anchored_script(1000401))
        self.write("Story/Mission/1000201/shared.json", anchored_script(1000401))
        self.write("Story/Mission/1000101/ambiguous-a.json", anchored_script(1000501))
        self.write("Story/Mission/1000101/ambiguous-b.json", anchored_script(1000501))
        output = Path(self.temp.name) / "early-timeline.db"
        import_data(self.root, output)
        with open_db(output) as conn:
            recovered = conn.execute("SELECT mission_id,script,sequence,branch,kind FROM lines WHERE sentence_id='100020114'").fetchone()
            self.assertEqual(tuple(recovered), ("1000101", "Story/Mission/1000101/intro.json", 1, "TalkSentence_100020113", "dialogue"))
            for sid, sequence, branch in ((100020116, 2, "TalkSentence_100020115"),
                                         (100020127, 5, "TalkSentence_100020126"),
                                         (100020131, 6, "TalkSentence_100020130"),
                                         (100020134, 7, "TalkSentence_100020133")):
                recovered = conn.execute("SELECT sequence,branch FROM lines WHERE sentence_id=?", (str(sid),)).fetchone()
                self.assertEqual(tuple(recovered), (sequence, branch))
            for excluded in (100020112, 100020140, 100030114, 100040114, 100050114):
                self.assertIsNone(conn.execute("SELECT 1 FROM lines WHERE sentence_id=?", (str(excluded),)).fetchone())
        self.assertEqual(lexical_search(output, "这里是一个空间站")[0]["mission_id"], "1000101")

    def test_timeline_uses_explicit_mission_anchor_and_inserts_between_adjacent_rows(self):
        self.write("ExcelOutput/MainMission.json", [
            {"MainMissionID": mid, "Name": {"Hash": 1}, "Type": "Main"}
            for mid in (2022001, 2022201)
        ])
        texts = {"1": "演武", "4": "三月七", "101": "前一句", "102": "正中间", "103": "后一句"}
        self.write("TextMap/TextMapCHS.json", texts)
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": sid, "TalkSentenceText": {"Hash": sid % 100 + 100}, "TextmapTalkSentenceName": {"Hash": 4}}
            for sid in (222010201, 222010202, 222010203)
        ])
        decoy = "Story/Mission/2022201/Story202220102.json"
        self.write("ExcelOutput/PerformanceC.json", [{"PerformanceID": 202220102, "PerformancePath": decoy}])
        self.write(decoy, {"OnStartSequece": []})
        self.write("Story/Discussion/Mission/2022001/DS202200102.json", {"OnStartSequece": [{"TaskList": [{
            "$type": "RPG.GameCore.PlaySimpleTalk",
            "SimpleTalkList": [{"TalkSentenceID": sid} for sid in (222010201, 222010203)],
        }]}]})
        output = Path(self.temp.name) / "timeline-owner.db"
        import_data(self.root, output)

        detail = get_mission(output, "2022001")
        self.assertEqual([line["text"] for line in detail["lines"]], ["前一句", "正中间", "后一句"])
        self.assertEqual([line["position"] for line in detail["lines"]], [0, 0.5, 1])
        self.assertFalse(get_mission(output, "2022201")["lines"])

    def test_text_cleanup_keeps_literal_choices_and_separates_variants(self):
        self.assertEqual(normalize_text("<报社总部的传社玉玺>"), "<报社总部的传社玉玺>")
        self.assertEqual(normalize_text("<color=#fff>{M#他说}{F#她说}</color>"), "他说 她说")

    def test_description_terms_separate_adjacent_entities_and_actions(self):
        terms = _extract_content_terms("卡芙卡在空间站唤醒开拓者")
        self.assertEqual(set(terms), {"卡芙卡", "空间站", "开拓者", "唤醒"})
        self.assertEqual(set(_extract_content_terms("大黑塔与黑塔讨论空间站")), {"大黑塔", "黑塔", "空间站"})
        self.assertEqual(set(_extract_content_terms("星期日和知更鸟关于谐乐鸽笼子的对话")),
                         {"星期日", "知更鸟", "谐乐", "鸽笼", "笼子"})

    def test_description_bonus_balances_terms_and_actual_scene_evidence(self):
        terms = _extract_content_terms("卡芙卡在空间站唤醒开拓者")
        scene = {"world": "空间站「黑塔」"}
        direct = semantic_context_bonus(terms, "卡芙卡：这儿？一个空间站，不重要。", scene, {"卡芙卡"})
        mention = semantic_context_bonus(terms, "开拓者：我记得卡芙卡在空间站唤醒我。", {"world": "翁法罗斯"}, {"开拓者"})
        self.assertGreater(direct, mention)
        action_only = semantic_context_bonus(terms, "唤醒", {}, set())
        self.assertAlmostEqual(action_only, 0.025)
        self.assertEqual(semantic_context_bonus([], "", {}, set()), 0.0)
        unrelated = {"text": "我记得自己在空间站被唤醒。", "speaker": "开拓者"}
        self.assertEqual(_score_fields("卡芙卡在空间站唤醒开拓者", unrelated)[0], 0.0)
        self.assertGreater(_score_fields("卡芙卡在空间站唤醒开拓者", {**unrelated, "speaker": "卡芙卡"})[0], 0.0)

    def test_nested_containers_and_new_dialogue_leaf_tasks(self):
        texts = json.loads((self.root / "TextMap/TextMapCHS.json").read_text(encoding="utf-8"))
        sentences = json.loads((self.root / "ExcelOutput/TalkSentenceConfig.json").read_text(encoding="utf-8"))
        extra_sids = range(100, 114)
        for sid in extra_sids:
            texts[str(sid)] = f"扩展台词{sid}"
            sentences.append({"TalkSentenceID": sid, "TalkSentenceText": {"Hash": sid}, "TextmapTalkSentenceName": {"Hash": 4}})
        self.write("TextMap/TextMapCHS.json", texts)
        self.write("ExcelOutput/TalkSentenceConfig.json", sentences)

        self.write("Config/Level/Mission/1/Act/ActNested.json", {"OnStartSequece": [{"TaskList": [
            {
                "$type": "RPG.GameCore.PredicateTaskList",
                "SuccessTaskList": [
                    {"$type": "RPG.GameCore.PlayAndWaitSimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 100}]},
                    {"$type": "RPG.GameCore.PlayOptionTalk", "OptionList": [{"TalkSentenceID": 101, "TriggerCustomString": "opt_a"}]},
                ],
                "FailedTaskList": [
                    {"$type": "RPG.GameCore.PlayOptionTalk", "OptionList": [{"TalkSentenceID": 102, "TriggerCustomString": "opt_b"}]},
                ],
            },
            {
                "$type": "RPG.GameCore.SwitchCase",
                "TaskList": [{"SuccessTaskList": [{"$type": "RPG.GameCore.PlayRogueSimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 103}]}]}],
                "DefaultTask": [{"$type": "RPG.GameCore.PlayRogueOptionTalk", "OptionList": [{"TalkSentenceID": 104, "TriggerCustomString": "opt_r"}]}],
            },
            {
                "$type": "RPG.GameCore.RandomConfig",
                "TaskList": [{"$type": "RPG.GameCore.PlayNPCBubbleTalk", "BubbleTalkInfoList": [{"TalkSentenceID": 105}]}],
            },
            {
                "$type": "RPG.GameCore.PropSetupTrigger",
                "OnTriggerEnter": [{"$type": "RPG.GameCore.PlayNPCSingleBubbleTalk", "TalkSentenceID": {"IsDynamic": False, "FixedValue": {"Value": 106}}}],
            },
            {
                "$type": "RPG.GameCore.PlayFullScreenTransfer",
                "TextInfo": {"TextList": [{"TalkSentenceID": 107}, {"TalkSentenceID": 108}]},
            },
            {
                "$type": "RPG.GameCore.ConsumeOrigamiItem",
                "OnSubmitConfirm": [{
                    "$type": "RPG.GameCore.PlayOrigamiTraceTalk",
                    "TalkList": [{"TalkInfo": {"SimpleTalkList": [{"TalkSentenceID": 109}, {"TalkSentenceID": 110}]}}],
                }],
            },
            {"$type": "RPG.GameCore.PerformanceEndBlackText", "TalkSentenceID": 111},
        ]}]})
        out = Path(self.temp.name) / "nested.db"
        import_data(self.root, out)
        lines = get_mission(out, "1", script="Config/Level/Mission/1/Act/ActNested.json")["lines"]
        self.assertEqual([line["sentence_id"] for line in lines], [str(sid) for sid in range(100, 112)])
        options = [line for line in lines if line["kind"] == "option"]
        self.assertEqual([line["sentence_id"] for line in options], ["101", "102", "104"])
        self.assertEqual(len({line["position"] // 100 for line in options}), 3)
        self.assertEqual(options[0]["branch"], "sequence:0#alt:0#cond:1")
        self.assertEqual(options[1]["branch"], "sequence:0#alt:0#cond:2")
        self.assertEqual(options[2]["branch"], "sequence:0#alt:1#cond:2")
        self.assertEqual(len({(line["sequence"], line["position"]) for line in lines}), len(lines))

    def test_mission_trigger_script_deduplicates_act_sentences_but_keeps_unique(self):
        self.write("Config/Level/Mission/1/Mission_100.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 10}, {"TalkSentenceID": 16}]},
        ]}]})
        self.write("Config/Level/Mission/1/Mission_101.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PropSetupTrigger", "OnTriggerEnter": [
                {"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 14}]},
            ]},
        ]}]})
        out = Path(self.temp.name) / "dedup-mission.db"
        import_data(self.root, out)
        lines = get_mission(out, "1")["lines"]
        self.assertEqual(sum(1 for line in lines if line["sentence_id"] == "10"), 1)
        self.assertEqual(sum(1 for line in lines if line["sentence_id"] == "16"), 1)
        self.assertFalse(any(line["script"].endswith("Mission_100.json") for line in lines))
        unique_rows = [line for line in lines if line["sentence_id"] == "14"]
        self.assertEqual(len(unique_rows), 1)
        self.assertTrue(unique_rows[0]["script"].endswith("Mission_101.json"))

    def test_conflict_recovery_tiers_handle_stray_mission_same_quest_and_sandwiched_gaps(self):
        self.write("ExcelOutput/MainMission.json", [
            {"MainMissionID": 1040534, "Name": {"Hash": 1}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1054400, "Name": {"Hash": 3}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1040403, "Name": {"Hash": 2}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1040412, "Name": {"Hash": 2}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1040208, "Name": {"Hash": 1}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
            {"MainMissionID": 1040209, "Name": {"Hash": 2}, "ChapterID": 2, "WorldID": 3, "Type": "Main"},
        ])
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": sid, "TalkSentenceText": {"Hash": 10}}
            for sid in (
                105002801, 105002802, 105002803, 105002899,
                144031701, 144031702, 144031703, 144031704,
                104031301, 104031302, 104031303, 104031305, 104031390,
            )
        ])
        # Tier 1: unique performance owner 1040534 vs 1-line unrelated Mission_ reuse in 1054400
        self.write("Story/Discussion/Mission/1040534/DS104053409.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 105002801}, {"TalkSentenceID": 105002803}]},
        ]}]})
        self.write("Config/Level/Mission/1054400/Mission_105440014.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 105002899}]},
        ]}]})
        # Case 2B: same-quest sub-mission split (1040403 & 1040412)
        self.write("Config/Level/Mission/1040403/Act/Act104040317.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 144031702}]},
        ]}]})
        self.write("Config/Level/Mission/1040412/Mission_104041212.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 144031703}]},
        ]}]})
        # Case 2A: distinct quests (1040208 & 1040209); only 104031302 is sandwiched in 1040208
        self.write("Config/Level/Mission/1040208/Mission_104020801.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 104031301}, {"TalkSentenceID": 104031303}]},
        ]}]})
        self.write("Story/Discussion/Mission/1040209/DS104020901.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 104031390}]},
        ]}]})

        out = Path(self.temp.name) / "conflict-tiers.db"
        stats = import_data(self.root, out)
        with open_db(out) as conn:
            self.assertEqual(conn.execute("SELECT mission_id FROM lines WHERE sentence_id='105002802'").fetchone()[0], "1040534")
            self.assertEqual(conn.execute("SELECT mission_id FROM lines WHERE sentence_id='144031701'").fetchone()[0], "1040403")
            self.assertEqual(conn.execute("SELECT mission_id FROM lines WHERE sentence_id='144031704'").fetchone()[0], "1040412")
            self.assertEqual(conn.execute("SELECT mission_id FROM lines WHERE sentence_id='104031302'").fetchone()[0], "1040208")
            self.assertIsNone(conn.execute("SELECT 1 FROM lines WHERE sentence_id='104031305'").fetchone())
        self.assertNotIn("1050028", stats["timeline_conflicts"])
        self.assertNotIn("1440317", stats["timeline_conflicts"])
        self.assertEqual(stats["unverified_timeline_groups"]["1040313"]["sentences"], 1)

    def test_orphaned_mission_folder_placeholder_blacktext_and_dominant_side_calibration(self):
        self.write("ExcelOutput/MainMission.json", [
            {"MainMissionID": 1033101, "Type": "Main"},
            {"MainMissionID": 5000410, "Type": "Branch"},
        ])
        self.write("ExcelOutput/TalkSentenceConfig.json", [
            {"TalkSentenceID": sid, "TalkSentenceText": {"Hash": 10}}
            for sid in (
                100000005,
                103310101, 103310201, 103311201,
                500050001, 500050002,
                500050101,
                500050201, 500050202, 500050203, 500050204, 500050205,
                500050301, 500050302,
            )
        ])
        # Orphaned Mission_*.json directory not listed in MainMission.json must be skipped.
        self.write("Config/Level/Mission/8014108/Mission_801410801.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayMissionTalk", "SimpleTalkList": [{"TalkSentenceID": 10}]},
        ]}]})
        # 1033101: generic 100000005 inside Story103310112 must not block timeline-1033112.
        for slot, sid in ((1, 103310101), (2, 103310201)):
            self.write(f"Story/Mission/1033101/Story1033101{slot:02}.json", {"OnStartSequece": [{"TaskList": [
                {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": sid}]},
            ]}]})
        self.write("Story/Mission/1033101/Story103310112.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PredicateTaskList", "SuccessTaskList": [
                {"$type": "RPG.GameCore.PerformanceEndBlackText", "TalkSentenceID": 100000005},
            ]},
            {"$type": "RPG.GameCore.PlayTimeline"},
        ]}]})
        # 5000410: dominant 5000502 (4 lines) + secondary conditional 5000503 (1 line) still calibrates 5000501.
        self.write("Story/Discussion/Mission/5000410/DS500041001.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayTimeline"},
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 500050002}]},
        ]}]})
        self.write("Story/Discussion/Mission/5000410/DS500041002.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayTimeline"},
        ]}]})
        self.write("Story/Discussion/Mission/5000410/DS500041003.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayTimeline"},
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": sid} for sid in (500050202, 500050203, 500050204, 500050205)]},
            {"$type": "RPG.GameCore.PredicateTaskList", "SuccessTaskList": [
                {"$type": "RPG.GameCore.PlayOptionTalk", "OptionList": [{"TalkSentenceID": 500050302, "TriggerCustomString": "opt"}]},
            ]},
        ]}]})
        self.write("Story/Discussion/Mission/5000410/DS500041004.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlayTimeline"},
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 500050301}]},
        ]}]})

        out = Path(self.temp.name) / "r5-guards.db"
        import_data(self.root, out)
        with open_db(out) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM missions WHERE id='8014108'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM lines WHERE mission_id='1033101' AND sentence_id='100000005'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM lines WHERE mission_id='1033101' AND sentence_id='103311201'").fetchone())
            for sid in ("500050001", "500050101", "500050201"):
                self.assertEqual(conn.execute("SELECT mission_id FROM lines WHERE sentence_id=?", (sid,)).fetchone()[0], "5000410")

    def test_mutually_exclusive_predicate_splits_chunks_edges_and_keeps_top_level_positions_stable(self):
        texts = json.loads((self.root / "TextMap/TextMapCHS.json").read_text(encoding="utf-8"))
        sentences = json.loads((self.root / "ExcelOutput/TalkSentenceConfig.json").read_text(encoding="utf-8"))
        for sid, text in ((200, "前一句"), (201, "未喝茶回应"), (202, "已喝茶回应"), (203, "后一句")):
            texts[str(sid)] = text
            sentences.append({"TalkSentenceID": sid, "TalkSentenceText": {"Hash": sid}, "TextmapTalkSentenceName": {"Hash": 4}})
        self.write("TextMap/TextMapCHS.json", texts)
        self.write("ExcelOutput/TalkSentenceConfig.json", sentences)

        self.write("Config/Level/Mission/1/Act/ActMutex.json", {"OnStartSequece": [{"TaskList": [
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 200}]},
            {
                "$type": "RPG.GameCore.PredicateTaskList",
                "SuccessTaskList": [{"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 201}]}],
                "FailedTaskList": [{"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 202}]}],
            },
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 203}]},
        ]}]})
        out = Path(self.temp.name) / "mutex-stable.db"
        import_data(self.root, out)
        with open_db(out) as conn:
            rows = conn.execute(
                "SELECT sentence_id, position, branch, chunk_id FROM lines WHERE script='Config/Level/Mission/1/Act/ActMutex.json' ORDER BY position"
            ).fetchall()
            self.assertEqual([(r[0], r[1], r[2]) for r in rows], [
                ("200", 0, ""),
                ("201", 10000, "sequence:0#alt:1#cond:1"),
                ("202", 10100, "sequence:0#alt:1#cond:2"),
                ("203", 20000, ""),
            ])
            self.assertEqual(len({r[3] for r in rows}), 4)
            edges = conn.execute(
                "SELECT source, target, kind FROM edges WHERE script='Config/Level/Mission/1/Act/ActMutex.json' ORDER BY target"
            ).fetchall()
            self.assertEqual([tuple(e) for e in edges], [
                ("sequence:0", "sequence:0#alt:1#cond:1", "continuation"),
                ("sequence:0", "sequence:0#alt:1#cond:2", "continuation"),
            ])

    def test_single_sided_predicate_stays_inline_without_shifting_later_top_level_tasks(self):
        texts = json.loads((self.root / "TextMap/TextMapCHS.json").read_text(encoding="utf-8"))
        sentences = json.loads((self.root / "ExcelOutput/TalkSentenceConfig.json").read_text(encoding="utf-8"))
        for sid, text in ((210, "单边分支A"), (211, "单边分支B"), (212, "后续顶层")):
            texts[str(sid)] = text
            sentences.append({"TalkSentenceID": sid, "TalkSentenceText": {"Hash": sid}, "TextmapTalkSentenceName": {"Hash": 4}})
        self.write("TextMap/TextMapCHS.json", texts)
        self.write("ExcelOutput/TalkSentenceConfig.json", sentences)

        self.write("Config/Level/Mission/1/Act/ActSingleSide.json", {"OnStartSequece": [{"TaskList": [
            {
                "$type": "RPG.GameCore.PredicateTaskList",
                "SuccessTaskList": [
                    {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 210}]},
                    {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 211}]},
                ],
                "FailedTaskList": [],
            },
            {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 212}]},
        ]}]})
        out = Path(self.temp.name) / "single-side.db"
        import_data(self.root, out)
        with open_db(out) as conn:
            rows = conn.execute(
                "SELECT sentence_id, position, branch FROM lines WHERE script='Config/Level/Mission/1/Act/ActSingleSide.json' ORDER BY position"
            ).fetchall()
            self.assertEqual([tuple(r) for r in rows], [
                ("210", 0, ""),
                ("211", 100, ""),
                ("212", 10000, ""),
            ])

    def test_switch_case_and_random_config_split_distinct_alt_branches_and_chunks(self):
        texts = json.loads((self.root / "TextMap/TextMapCHS.json").read_text(encoding="utf-8"))
        sentences = json.loads((self.root / "ExcelOutput/TalkSentenceConfig.json").read_text(encoding="utf-8"))
        for sid in range(220, 224):
            texts[str(sid)] = f"分支对白{sid}"
            sentences.append({"TalkSentenceID": sid, "TalkSentenceText": {"Hash": sid}, "TextmapTalkSentenceName": {"Hash": 4}})
        self.write("TextMap/TextMapCHS.json", texts)
        self.write("ExcelOutput/TalkSentenceConfig.json", sentences)

        self.write("Config/Level/Mission/1/Act/ActSwitchRand.json", {"OnStartSequece": [{"TaskList": [
            {
                "$type": "RPG.GameCore.SwitchCase",
                "TaskList": [{"SuccessTaskList": [{"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 220}]}]}],
                "DefaultTask": [{"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 221}]}],
            },
            {
                "$type": "RPG.GameCore.RandomConfig",
                "TaskList": [
                    {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 222}]},
                    {"$type": "RPG.GameCore.PlaySimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 223}]},
                ],
            },
        ]}]})
        out = Path(self.temp.name) / "switch-rand.db"
        import_data(self.root, out)
        with open_db(out) as conn:
            rows = conn.execute(
                "SELECT sentence_id, position, branch, chunk_id FROM lines WHERE script='Config/Level/Mission/1/Act/ActSwitchRand.json' ORDER BY position"
            ).fetchall()
            self.assertEqual([(r[0], r[1], r[2]) for r in rows], [
                ("220", 0, "sequence:0#alt:0#cond:1"),
                ("221", 100, "sequence:0#alt:0#cond:2"),
                ("222", 10000, "sequence:0#alt:1#rand:1"),
                ("223", 10100, "sequence:0#alt:1#rand:2"),
            ])
            self.assertEqual(len({r[3] for r in rows}), 4)


if __name__ == "__main__":
    unittest.main()
