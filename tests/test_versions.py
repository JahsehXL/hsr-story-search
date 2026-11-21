import json
import sqlite3

import pytest

from backend.version_metadata import apply_versions, load_metadata, normalize_name
from scripts.sync_versions import build_mappings, fetch_page, main, parse_infobox


def mission(identifier="123", **values):
    return {"id": identifier, "name": "任务标题", "chapter": "章节", "world": "地区",
            "type": "开拓任务", **values}


def page(**values):
    return {"title": "任务标题", "version": "2.0", "type": "开拓任务",
            "source": "https://wiki.biligame.com/sr/example", "retrieved_at": "2026-11-13",
            "method": "bwiki-task-infobox", "status": "verified", **values}


def test_parser_requires_exact_task_name_and_explicit_single_release():
    html = """<table><tr><th>任务名称</th><td>任务标题</td></tr>
    <tr><th>任务类型</th><td><a>开拓任务</a></td></tr>
    <tr><th>所属版本</th><td>2.0</td></tr></table>"""
    assert parse_infobox(html, "任务标题")["version"] == "2.0"
    assert parse_infobox(html, "其他任务") is None
    assert parse_infobox(html.replace("2.0", "2.0 / 2.1"), "任务标题") is None
    assert parse_infobox(html.replace("所属版本", "当前版本"), "任务标题") is None


def test_duplicates_require_same_chapter_world_and_type():
    records, _ = build_mappings([mission("1"), mission("2")], [page()])
    assert [r["id"] for r in records] == ["1", "2"]
    records, report = build_mappings([mission("1"), mission("2", chapter="另一章")], [page()])
    assert records == []
    assert report["ambiguous_names"] == ["任务标题"]
    records, _ = build_mappings([mission("1"), mission("2", type="同行任务")], [page()])
    assert [r["id"] for r in records] == ["1"]


def test_game_branch_enum_includes_event_quests():
    records, _ = build_mappings([mission("1", type="支线任务")], [page(type="活动任务")])
    assert records[0]["version"] == "2.0"
    assert records[0]["wiki_type"] == "活动任务"


def test_applier_preserves_curated_values_and_checks_mission_identity():
    connection = sqlite3.connect(":memory:")
    connection.execute("""CREATE TABLE missions (
        id TEXT PRIMARY KEY, name TEXT, chapter TEXT, world TEXT, type TEXT,
        version TEXT DEFAULT '', version_source TEXT DEFAULT '', version_status TEXT DEFAULT 'unknown')""")
    for identifier in ("1", "2", "3"):
        m = mission(identifier)
        connection.execute("INSERT INTO missions(id,name,chapter,world,type) VALUES(:id,:name,:chapter,:world,:type)", m)
    connection.execute("UPDATE missions SET version='1.0',version_status='manual' WHERE id='2'")
    connection.execute("UPDATE missions SET name='改名' WHERE id='3'")
    metadata, _ = build_mappings([mission("1"), mission("2"), mission("3")], [page()])
    assert apply_versions(connection, metadata) == 1
    assert connection.execute("SELECT version FROM missions ORDER BY id").fetchall() == [("2.0",), ("1.0",), ("",)]
    assert apply_versions(connection, metadata) == 0


def test_metadata_validation_and_bundled_catalog(tmp_path):
    entries = load_metadata()
    assert len(entries) >= 450
    for entry in entries:
        if entry["method"] == "versioned-gameplay-recording":
            assert entry["source"].startswith("https://www.bilibili.com/video/")
        elif entry["method"] == "upstream-snapshot-first-appearance":
            assert entry["source"].startswith("https://github.com/DimbreathBot/TurnBasedGameData/")
        elif entry["method"] == "official-release-notes":
            assert entry["source"].startswith(("https://www.hoyoverse.com/", "https://www.hoyolab.com/"))
        else:
            assert entry["method"].startswith("bwiki-")
            assert entry["source"].startswith("https://wiki.biligame.com/sr/")
    target = tmp_path / "metadata.json"
    target.write_text(json.dumps({"schema_version": 1, "missions": [entries[0], entries[0]]}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_metadata(target)

    bad_world = tmp_path / "bad-world.json"
    bad_world.write_text(
        json.dumps({"schema_version": 1, "missions": [{**mission("999", world="二相乐园", version="1.0"), "source": "https://wiki.biligame.com/sr/test", "retrieved_at": "2026-11-21", "method": "bwiki-series-infobox", "status": "verified"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="predates"):
        load_metadata(bad_world)


def test_name_normalization_keeps_meaningful_punctuation():
    assert normalize_name("模拟宇宙• 始发测试") == normalize_name("模拟宇宙·始发测试")
    assert normalize_name("任务·一") != normalize_name("任务一")


def test_wiki_sync_retains_reviewed_gameplay_version(tmp_path, monkeypatch):
    database, output = tmp_path / "story.db", tmp_path / "versions.json"
    identity = mission()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE missions (id,name,chapter,world,type)")
        connection.execute("INSERT INTO missions VALUES (:id,:name,:chapter,:world,:type)", identity)
    records, _ = build_mappings([identity], [page()])
    reviewed = {**records[0], "version": "2.1", "method": "versioned-gameplay-recording",
                "source": "https://www.bilibili.com/video/example/"}
    output.write_text(json.dumps({"missions": [reviewed]}), encoding="utf-8")
    monkeypatch.setattr("scripts.sync_versions.fetch_page", lambda *args: page())
    monkeypatch.setattr("sys.argv", ["sync_versions", "--database", str(database),
                                    "--output", str(output), "--cache", str(tmp_path / "cache")])
    main()
    assert load_metadata(output) == [reviewed]


def test_offline_cache_miss_or_interrupted_cache_is_recoverable(tmp_path):
    import hashlib

    assert fetch_page("任务标题", tmp_path, True, None) is None
    cache = tmp_path / (hashlib.sha256("任务标题".encode()).hexdigest() + ".json")
    cache.write_text('{"title":', encoding="utf-8")
    assert fetch_page("任务标题", tmp_path, True, None) is None
