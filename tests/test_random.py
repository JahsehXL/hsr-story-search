from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.data import create_schema, normalize_text, open_db


WALTER_QUOTE = "这趟旅程并不短，先回列车上做些准备吧。"


def make_random_database(path):
    with open_db(path, readonly=False) as conn:
        create_schema(conn)
        conn.execute("INSERT INTO info VALUES('schema_version','1')")
        conn.executemany("INSERT INTO missions(id,name,chapter,world,type,version) VALUES(?,?,?,?,?,?)", [
            ("station", "启程之前", "序章", "空间站", "Main", "1.0"),
            ("express", "车厢夜话", "同行", "列车", "Companion", ""),
            ("unlinked", "未关联", "", "空间站", "Main", "1.0"),
        ])
        conn.execute("INSERT INTO chunks VALUES(1,'first','station','Act',0,'',?,?,?,?)", (
            WALTER_QUOTE, WALTER_QUOTE, "test-hash", '["walter","march"]',
        ))
        lines = [
            ("walter", "瓦尔特", WALTER_QUOTE),
            ("mirror", "瓦尔特", WALTER_QUOTE, "station", "Mirror"),
            ("walter-2", "瓦尔特·杨", "有时间的话，我想和你聊聊漫长的旅途。"),
            ("march", "三月七", "告诉我吧，下一站我们又会遇到什么呢？"),
            ("express", "瓦尔特", "列车会在下一次跃迁之前停靠一段时间。", "express"),
            ("option", "瓦尔特", "这个玩家选项虽然很长也不应被抽到。", "station", "Act", "option"),
            ("no-speaker", " \t\u3000", "这一行没有实际的说话人，不应被抽到。"),
            ("short", "瓦尔特", "           简短的句子          "),
            ("punctuation", "瓦尔特", "………………………………"),
            ("fake-words", "瓦尔特", "哈哈哈哈哈哈哈哈哈哈哈哈哈"),
            ("markup", "瓦尔特", "<color=#ffffff>！！！</color>"),
            ("placeholder", "占位角色", "【测试文本】………………"),
            ("unlinked-script", "瓦尔特", "这句剧情来自未关联脚本，不应该被抽到。", "station", "unlinked"),
            ("unlinked-mission", "瓦尔特", "这句剧情来自未关联任务，不应该被抽到。", "unlinked"),
            ("missing-mission", "瓦尔特", "没有对应任务的台词也不应该被抽到。", "missing"),
        ]
        for position, (line_id, speaker, text, *metadata) in enumerate(lines):
            mission, script, kind = metadata + ["station", "Act", "dialogue"][len(metadata):]
            # Different speakers share a chunk to distinguish scene and line filters.
            conn.execute("INSERT INTO lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                line_id, mission, script, position, 0, line_id, speaker, text, normalize_text(text), kind, "", "", 1,
            ))


@pytest.fixture
def random_app(tmp_path, monkeypatch):
    monkeypatch.setenv("HSR_ADMIN_CODE", "test-random-admin-code")
    monkeypatch.delenv("HSR_ACCESS_CODE", raising=False)
    monkeypatch.setenv("HSR_PUBLIC_PREFIX", "/")
    monkeypatch.setenv("HSR_ADMIN_AUTH", "code")
    app = create_app(tmp_path / "data", mode="server")
    source = tmp_path / "source.db"
    make_random_database(source)
    app.state.runtime.install_snapshot(source)
    with TestClient(app, base_url="https://story.example") as client:
        yield app, client


def draw(client, filters=None, recent=(), **other):
    response = client.post("/api/v1/random", json={"filters": filters or {}, "recent_line_ids": list(recent), **other})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["search_mode"] == "random"
    assert data["total"] == len(data["results"]) <= 1
    return data


def test_random_is_filtered_by_speaker_and_returns_a_snapshot_anchor(random_app):
    app, client = random_app
    filters = {"world": "空间站", "version": "1.0", "type": "Main", "character": "老杨"}
    with patch.object(app.state.runtime, "semantic_search") as semantic, patch("backend.runtime.embed") as embed:
        data = draw(client, filters, ["walter-2"], query="无关联关键词", mode="hybrid", query_vector=[1])
        semantic.assert_not_called()
        embed.assert_not_called()
    (result,) = data["results"]
    assert result["line_id"] in {"walter", "mirror"}
    assert result["match_type"] == "random" and result["kind"] == "dialogue"
    assert result["speaker"] == "瓦尔特" and result["display_text"] == WALTER_QUOTE
    assert result["mission_name"] == "启程之前"
    assert data["snapshot"] == app.state.runtime.active_path()[1]
    params = {"snapshot": data["snapshot"], "line_id": result["line_id"]}
    detail = client.get(f"/api/v1/missions/{result['mission_id']}", params=params).json()
    assert detail["snapshot"] == data["snapshot"] and not detail["anchor_missing"]
    assert next(line["text"] for line in detail["lines"] if line["id"] == detail["anchor_line_id"]) == WALTER_QUOTE


def test_random_excludes_noise_and_recent_quotes_then_falls_back_with_same_filters(random_app):
    _, client = random_app
    filters = {"world": "空间站", "character": "杨叔"}
    assert draw(client, filters, ["walter"])["results"][0]["line_id"] == "walter-2"
    # Even when the pool is exhausted, do not immediately return the same quote.
    assert draw(client, filters, ["mirror", "walter-2"])["results"][0]["line_id"] == "walter-2"
    assert draw(client, {"character": "三月"}, ["march"])["results"][0]["line_id"] == "march"
    assert draw(client, {"version": "unknown", "type": "Companion"})["results"][0]["line_id"] == "express"
    assert draw(client, {"worlds": ["列车"], "characters": ["杨叔", "三月"]})["results"][0]["line_id"] == "express"
    for filters in (
        {"world": "列车", "version": "1.0"}, {"type": "Daily"},
        {"character": "占位角色"}, {"character": "瓦尔特%"}, {"character": "' OR 1=1 --"},
    ):
        empty = draw(client, filters, ["walter"])
        assert empty["results"] == [] and empty["notice"]


def test_random_uses_current_snapshot_but_old_anchor_stays_addressable(random_app, tmp_path):
    app, client = random_app
    before = draw(client, {"character": "三月"})
    source = tmp_path / "next.db"
    make_random_database(source)
    with open_db(source, readonly=False) as conn:
        conn.execute("DELETE FROM lines WHERE id <> ?", ("express",))
    new_snapshot = app.state.runtime.install_snapshot(source)
    after = draw(client, snapshot=before["snapshot"])
    assert after["snapshot"] == new_snapshot != before["snapshot"]
    assert after["results"][0]["line_id"] == "express"
    old_detail = client.get("/api/v1/missions/station", params={"line_id": "march", "snapshot": before["snapshot"]}).json()
    assert old_detail["anchor_line_id"] == "march"


def test_random_shares_read_access_and_rate_limit_with_search(random_app, tmp_path, monkeypatch):
    app, client = random_app
    assert client.post("/api/v1/random", json={"recent_line_ids": ["walter"] * 31}).status_code == 422
    assert client.post("/api/v1/random", json={"filters": {"wrong_key": "空间站"}}).status_code == 422
    app.state.runtime.settings.update({"limits": {"search_per_minute": 1}})
    assert draw(client)["total"] == 1
    assert client.post("/api/v1/random", json={}).status_code == 429
    assert client.post("/api/v1/search", json={"query": "列车", "mode": "text"}).status_code == 429
    monkeypatch.setenv("HSR_ACCESS_CODE", "test-private-code")
    with TestClient(create_app(tmp_path / "private", mode="server"), base_url="https://story.example") as private:
        assert private.post("/api/v1/random", json={}).status_code == 401
