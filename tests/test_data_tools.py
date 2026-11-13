import hashlib
import json
from pathlib import Path

import pytest

from backend.data import import_data, open_db
from backend.data_maintenance import (
    audit_database, build_candidate, compare_databases, game_version_from_title,
    inspect_source, normalize_provenance, repair_metadata, package_candidate,
)


def write_json(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "raw"
    write_json(root, "TextMap/TextMapMainCHS.json", {"1": "任务", "2": "对白", "3": "三月七"})
    write_json(root, "ExcelOutput/MainMission.json", [{"MainMissionID": 1, "Name": {"Hash": 1}, "Type": "Main"}])
    write_json(root, "ExcelOutput/TalkSentenceConfig.json", [
        {"TalkSentenceID": 10, "TalkSentenceText": {"Hash": 2}, "TextmapTalkSentenceName": {"Hash": 3}}])
    write_json(root, "Story/Mission/1/intro.json", {"OnStartSequece": [{"TaskList": [
        {"$type": "RPG.GameCore.PlayAndWaitSimpleTalk", "SimpleTalkList": [{"TalkSentenceID": 10}]}]}]})
    return root


def provenance(**overrides):
    return {"repository": "DimbreathBot/TurnBasedGameData", "commit": "a" * 40,
            "commit_title": "OSPRODWin4.7.0_D17180467_A17176203_L17175930", **overrides}


def test_candidate_package_roundtrip_for_management_upload(source, tmp_path):
    from backend.runtime import Runtime

    database = tmp_path / "candidate.db"
    build_candidate(source, database, provenance())
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    target = tmp_path / "hsr-candidate.zip"
    report = package_candidate(database, target)
    assert report["audit"]["ok"]
    assert report["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match="已存在"):
        package_candidate(database, target)
    runtime = Runtime(tmp_path / "recipient", "local")
    try:
        runtime.install_package(target, tmp_path / "unpacked")
        assert runtime.status_data()["ready"]
        assert runtime.status_data()["stats"]["game_version"] == "4.7"
    finally:
        runtime.close()


def test_production_title_records_snapshot_version_without_guessing_task_release():
    value = normalize_provenance(provenance())
    assert value["game_version"] == "4.7"
    assert value["game_version_source"].endswith("a" * 40)
    assert game_version_from_title("OSPRODWin4.7.0_D2_A3_L4") == "4.7"
    assert game_version_from_title("4.8 beta update") == ""
    assert normalize_provenance(provenance(commit_title="maintenance"))["game_version"] == ""
    with pytest.raises(ValueError, match="不一致"):
        normalize_provenance(provenance(game_version="4.8"))
    with pytest.raises(ValueError, match="HTTPS"):
        normalize_provenance(provenance(commit_title="maintenance", game_version="4.8"))
    with pytest.raises(ValueError, match="40"):
        normalize_provenance(provenance(commit="abcdef"))


def test_main_textmap_and_wrapper_build_preserve_source_and_old_database(source, tmp_path):
    before = hashlib.sha256((source / "TextMap/TextMapMainCHS.json").read_bytes()).hexdigest()
    report = inspect_source(source.parent)
    assert report["source"] == str(source)
    assert report["scripts"] == 1
    first = tmp_path / "first.db"
    built = build_candidate(source, first, provenance())
    assert built["audit"]["ok"]
    assert built["audit"]["summary"]["game_version"] == "4.7"
    with open_db(first) as database:
        assert database.execute("SELECT version,version_status FROM missions").fetchone()[:] == ("", "unknown")
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="已存在"):
        build_candidate(source, first, provenance())
    assert hashlib.sha256(first.read_bytes()).hexdigest() == digest
    assert hashlib.sha256((source / "TextMap/TextMapMainCHS.json").read_bytes()).hexdigest() == before
    second = tmp_path / "second.db"
    build_candidate(source, second, provenance(), baseline=first)
    comparison = compare_databases(first, second)
    assert comparison["delta"]["lines"] == 0
    assert comparison["reusable_chunks"] == 1
    assert comparison["new_or_changed_chunks"] == 0
    assert comparison["changed_lines"] == 0


def test_diagnostics_detect_broken_links_without_writing(source, tmp_path):
    target = tmp_path / "story.db"
    import_data(source, target)
    with open_db(target, readonly=False) as connection:
        connection.execute("DELETE FROM chunk_lines")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    report = audit_database(target)
    assert not report["ok"]
    assert "missing_chunk_lines" in report["errors"]
    assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
    missing = tmp_path / "missing.db"
    assert not audit_database(missing)["ok"]
    assert not missing.exists()


def test_metadata_repair_uses_new_copy_and_preserves_original(source, tmp_path):
    original, candidate = tmp_path / "original.db", tmp_path / "repaired.db"
    import_data(source, original)
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    metadata = tmp_path / "versions.json"
    write_json(tmp_path, "versions.json", {"schema_version": 1, "missions": [
        {"id": "1", "name": "任务", "chapter": "", "world": "", "type": "开拓任务", "version": "4.7",
         "status": "verified", "source": "https://wiki.biligame.com/sr/example", "retrieved_at": "2026-11-14",
         "method": "task-infobox"}]})
    result = repair_metadata(original, candidate, metadata)
    assert result["updated_missions"] == 1
    assert result["audit"]["ok"]
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    with open_db(candidate) as database:
        assert database.execute("SELECT version FROM missions").fetchone()[0] == "4.7"
    with pytest.raises(ValueError, match="已存在"):
        repair_metadata(original, candidate, metadata)


def test_empty_or_missing_mission_inputs_fail_explicitly(source, tmp_path):
    write_json(source, "ExcelOutput/MainMission.json", [])
    with pytest.raises(ValueError, match="非空"):
        inspect_source(source)
    write_json(source, "ExcelOutput/TalkSentenceConfig.json", [])
    with pytest.raises(ValueError, match="没有解析出"):
        import_data(source, tmp_path / "empty.db")


def test_unexpected_script_schema_is_reported_before_build(source):
    write_json(source, "Story/Mission/1/intro.json", {"OnStartSequence": []})
    with pytest.raises(ValueError, match="OnStartSequece"):
        inspect_source(source)


def test_malformed_vector_metadata_is_a_structured_quality_error(source, tmp_path):
    target = tmp_path / "invalid-embedding.db"
    import_data(source, target)
    with open_db(target, readonly=False) as connection:
        connection.execute("INSERT INTO info VALUES('embedding',?)", (json.dumps("invalid"),))
    report = audit_database(target)
    assert not report["ok"]
    assert "invalid_vector_metadata" in report["errors"]
