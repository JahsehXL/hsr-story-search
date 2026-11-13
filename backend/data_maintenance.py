"""Read-only diagnostics and isolated candidate creation for AI-assisted maintenance."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import sqlite_vec

from .data import IMPORTER_VERSION, import_data, open_db, read_records, source_root, source_scripts
from .settings import embedding_signature
from .version_metadata import apply_versions, load_metadata


def game_version_from_title(title: str) -> str:
    """Recognize the upstream production build title without guessing task releases."""
    match = re.fullmatch(r"OSPRODWin(\d+\.\d+)\.\d+_D\d+_A\d+_L\d+", title.strip())
    return match[1] if match else ""


def normalize_provenance(value: dict) -> dict:
    repository = str(value.get("repository", "")).strip()
    commit = str(value.get("commit", "")).strip().lower()
    title = str(value.get("commit_title", "")).strip()
    version = str(value.get("game_version", "")).strip()
    evidence = str(value.get("game_version_source", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("来源仓库应为 owner/repository")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("来源提交必须是完整的 40 位 SHA")
    if not title or len(title) > 500:
        raise ValueError("请记录上游提交的完整首行标题（最多 500 字符）")
    detected = game_version_from_title(title)
    if detected and version and detected != version:
        raise ValueError("填写的游戏版本与上游生产版本标题不一致")
    version = version or detected
    if version and not re.fullmatch(r"\d+\.\d+", version):
        raise ValueError("游戏版本应为 major.minor，例如 4.7")
    commit_url = f"https://github.com/{repository}/commit/{commit}"
    if version and not detected and not evidence.startswith("https://"):
        raise ValueError("无法从提交标题识别版本，请提供游戏版本的 HTTPS 依据")
    return {"repository": repository, "commit": commit, "commit_title": title,
            "commit_url": commit_url, "game_version": version,
            "game_version_source": commit_url if detected else evidence}


def fetch_upstream(repository: str = "DimbreathBot/TurnBasedGameData", commit: str = "HEAD") -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("来源仓库应为 owner/repository")
    if commit != "HEAD" and not re.fullmatch(r"[a-fA-F0-9]{40}", commit):
        raise ValueError("指定 HEAD 或完整提交 SHA")
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        response = client.get(f"https://api.github.com/repos/{repository}/commits/{commit}",
                              headers={"Accept": "application/vnd.github+json", "User-Agent": "HSR-Story-Search"})
        response.raise_for_status()
        value = response.json()
    return normalize_provenance({"repository": repository, "commit": value["sha"],
                                 "commit_title": value["commit"]["message"].splitlines()[0]})


def inspect_source(source: Path | str) -> dict:
    root = source_root(source)
    scripts = source_scripts(root)
    if not scripts:
        raise ValueError("数据目录中没有找到任务剧情脚本")
    textmaps = sorted(path for path in (root / "TextMap").glob("*.json")
                      if path.name in {"TextMapCHS.json", "TextMapMainCHS.json"})
    texts = {}
    for path in textmaps:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("中文 TextMap 必须是 JSON 对象")
        texts.update(value)
    missions = read_records(root / "ExcelOutput/MainMission.json")
    sentences = read_records(root / "ExcelOutput/TalkSentenceConfig.json")
    if not texts or not missions or not sentences:
        raise ValueError("中文文字、任务表和台词表均需要非空记录")
    task_types: dict[str, int] = {}
    timeline_scripts = 0
    for path in scripts:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict) or not isinstance(value.get("OnStartSequece"), list):
            raise ValueError(f"剧情脚本缺少 OnStartSequece 数组：{path.relative_to(root).as_posix()}")
        types = [str(task.get("$type", "")).rsplit(".", 1)[-1]
                 for sequence in value["OnStartSequece"] for task in sequence.get("TaskList", [])]
        timeline_scripts += "PlayTimeline" in types
        for task_type in types:
            task_types[task_type] = task_types.get(task_type, 0) + 1
    inputs = sorted(set(textmaps + list((root / "ExcelOutput").glob("*.json")) + scripts))
    digest = hashlib.sha256()
    for path in inputs:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        file_hash = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                file_hash.update(block)
        digest.update(file_hash.digest())
    return {"source": str(root), "textmap_entries": len(texts), "missions": len(missions),
            "sentence_records": len(sentences), "scripts": len(scripts),
            "timeline_scripts": timeline_scripts, "task_types": dict(sorted(task_types.items())),
            "input_files": len(inputs), "input_sha256": digest.hexdigest(), "importer_version": IMPORTER_VERSION}


def stamp_provenance(database: Path | str, provenance: dict, source_report: dict | None = None) -> dict:
    """Record source coverage separately from each mission's first release."""
    normalized = normalize_provenance(provenance)
    with open_db(database, readonly=False) as connection:
        values = {"source_provenance": normalized, "source_commit": normalized["commit"],
                  "game_version": normalized["game_version"]}
        if source_report:
            values["source_audit"] = source_report
        connection.executemany("INSERT OR REPLACE INTO info(key,value) VALUES(?,?)",
                               [(key, json.dumps(value, ensure_ascii=False)) for key, value in values.items()])
    return normalized


def _open_diagnostic_database(path: Path | str) -> sqlite3.Connection:
    connection = open_db(path)
    connection.enable_load_extension(True)
    try:
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)
    return connection


def audit_database(database: Path | str) -> dict:
    """Return quality findings without changing the database or active snapshot."""
    result = {"database": str(Path(database).resolve()), "ok": False, "errors": [], "warnings": [], "summary": {}}
    try:
        with _open_diagnostic_database(database) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchall()
            if [row[0] for row in integrity] != ["ok"]:
                result["errors"].append("sqlite_integrity_failed")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"missions", "lines", "chunks", "chunk_lines", "chunks_fts", "info", "edges", "recalls"}
            missing = sorted(required - tables)
            if missing:
                result["errors"].append("missing_tables:" + ",".join(missing))
                return result
            result["info"] = {row[0]: json.loads(row[1]) for row in connection.execute("SELECT key,value FROM info")}
            result["counts"] = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                                for table in ("missions", "lines", "chunks", "chunk_lines", "chunks_fts")}
            if result["info"].get("schema_version") != 1:
                result["errors"].append("unsupported_schema_version")
            if not result["counts"]["lines"] or not result["counts"]["chunks"]:
                result["errors"].append("empty_dialogue_dataset")
            checks = {
                "orphan_lines": "SELECT count(*) FROM lines l LEFT JOIN missions m ON m.id=l.mission_id LEFT JOIN chunks c ON c.id=l.chunk_id WHERE m.id IS NULL OR c.id IS NULL",
                "orphan_chunk_lines": "SELECT count(*) FROM chunk_lines cl LEFT JOIN chunks c ON c.id=cl.chunk_id LEFT JOIN lines l ON l.id=cl.line_id WHERE c.id IS NULL OR l.id IS NULL OR l.chunk_id!=cl.chunk_id",
                "missing_chunk_lines": "SELECT count(*) FROM lines l WHERE NOT EXISTS(SELECT 1 FROM chunk_lines cl WHERE cl.line_id=l.id AND cl.chunk_id=l.chunk_id)",
                "missing_fts_chunks": "SELECT count(*) FROM chunks c WHERE NOT EXISTS(SELECT 1 FROM chunks_fts f WHERE f.rowid=c.id)",
                "orphan_fts_chunks": "SELECT count(*) FROM chunks_fts f WHERE NOT EXISTS(SELECT 1 FROM chunks c WHERE c.id=f.rowid)",
                "unlinked_lines": "SELECT count(*) FROM lines WHERE mission_id='unlinked' OR script='unlinked'",
            }
            result["relations"] = {name: connection.execute(sql).fetchone()[0] for name, sql in checks.items()}
            result["errors"].extend(name for name, count in result["relations"].items() if count)
            result["versions"] = [dict(row) for row in connection.execute("SELECT version,version_status,count(*) missions FROM missions GROUP BY version,version_status ORDER BY version")]
            result["unknown_missions"] = connection.execute("SELECT count(*) FROM missions WHERE version='' OR version_status='unknown'").fetchone()[0]
            if result["unknown_missions"]:
                result["warnings"].append("missions_need_release_evidence")
            metadata = result["info"].get("embedding", {})
            if not isinstance(metadata, dict):
                result["errors"].append("invalid_vector_metadata")
                return result
            vectors = connection.execute("SELECT count(*) FROM chunk_vectors").fetchone()[0] if "chunk_vectors" in tables else 0
            result["embedding"] = {"vectors": vectors, "chunks": result["counts"]["chunks"], "metadata": metadata,
                                   "complete": bool(metadata and vectors == result["counts"]["chunks"] and metadata.get("count") == vectors)}
            if metadata and not result["embedding"]["complete"]:
                result["errors"].append("incomplete_vector_index")
            if metadata:
                dimension = metadata.get("dimension", 0)
                if (not isinstance(dimension, int) or not 1 <= dimension <= 32768
                        or not re.fullmatch(r"[a-f0-9]{64}", str(metadata.get("signature", "")))):
                    result["errors"].append("invalid_vector_metadata")
                if metadata.get("signature_version") == 2:
                    identity = metadata.get("identity", {})
                    if (not isinstance(identity, dict) or identity.get("preprocessing") != "hsr-chunks-v1"
                            or identity.get("dimension") != dimension or identity.get("model") != metadata.get("model")
                            or embedding_signature(identity, dimension) != metadata["signature"]):
                        result["errors"].append("invalid_vector_identity")
            elif vectors:
                result["errors"].append("vector_metadata_missing")
            elif not metadata:
                result["warnings"].append("no_vector_index")
            if not result["info"].get("source_provenance"):
                result["warnings"].append("legacy_source_provenance_missing")
            if result["info"].get("importer_version", 0) < IMPORTER_VERSION:
                result["warnings"].append("legacy_importer_version_missing_or_old")
            result["summary"] = {**result["counts"], "game_version": result["info"].get("game_version", ""),
                                 "vector_index_complete": result["embedding"]["complete"],
                                 "unknown_missions": result["unknown_missions"]}
            result["ok"] = not result["errors"]
    except (sqlite3.Error, ValueError, OSError) as exc:
        result["errors"].append(f"database_read_failed:{exc}")
    return result


def compare_databases(baseline: Path | str, candidate: Path | str) -> dict:
    before, after = audit_database(baseline), audit_database(candidate)
    result = {"baseline": before, "candidate": after}
    if not before["ok"] or not after["ok"]:
        return result
    with _open_diagnostic_database(candidate) as connection:
        connection.execute("ATTACH DATABASE ? AS baseline", (Path(baseline).resolve().as_uri() + "?mode=ro",))
        result["delta"] = {key: after["counts"][key] - before["counts"][key] for key in before["counts"]}
        result["added_missions"] = [dict(row) for row in connection.execute("SELECT id,name,version,version_status FROM missions WHERE id NOT IN (SELECT id FROM baseline.missions) ORDER BY id")]
        result["removed_missions"] = [dict(row) for row in connection.execute("SELECT id,name FROM baseline.missions WHERE id NOT IN (SELECT id FROM missions) ORDER BY id")]
        for name, sql in {
            "added_lines": "SELECT count(*) FROM lines l WHERE NOT EXISTS(SELECT 1 FROM baseline.lines b WHERE b.id=l.id)",
            "removed_lines": "SELECT count(*) FROM baseline.lines b WHERE NOT EXISTS(SELECT 1 FROM lines l WHERE l.id=b.id)",
            "changed_lines": "SELECT count(*) FROM lines l JOIN baseline.lines b ON l.id=b.id WHERE l.text!=b.text OR l.speaker!=b.speaker OR l.mission_id!=b.mission_id OR l.branch!=b.branch",
            "reusable_chunks": "SELECT count(*) FROM chunks c WHERE EXISTS(SELECT 1 FROM baseline.chunks b WHERE b.content_hash=c.content_hash)",
        }.items():
            result[name] = connection.execute(sql).fetchone()[0]
        result["new_or_changed_chunks"] = after["counts"]["chunks"] - result["reusable_chunks"]
    return result


def build_candidate(source: Path | str, output: Path | str, provenance: dict,
                    baseline: Path | str | None = None, progress=None) -> dict:
    normalized = normalize_provenance(provenance)
    report = inspect_source(source)
    import_data(Path(report["source"]), Path(output), progress)
    stamp_provenance(output, normalized, report)
    result = {"source": report, "audit": audit_database(output)}
    if baseline:
        result["comparison"] = compare_databases(baseline, output)
    return result


def repair_metadata(database: Path | str, output: Path | str, metadata: Path | str | None = None) -> dict:
    """Copy a consistent SQLite snapshot and fill only identity-matched unknown versions."""
    source, target = Path(database).resolve(), Path(output).resolve()
    entries = load_metadata(metadata)
    if target.exists():
        raise ValueError("输出数据库已存在，请使用新的候选路径")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb"):
        pass
    with _open_diagnostic_database(source) as original, sqlite3.connect(target) as copied:
        original.backup(copied)
        applied = apply_versions(copied, entries)
        copied.execute("INSERT OR REPLACE INTO info VALUES('metadata_repaired_at',?)",
                       (json.dumps(datetime.now(timezone.utc).isoformat()),))
    return {"updated_missions": applied, "audit": audit_database(target)}


def package_candidate(database: Path | str, output: Path | str) -> dict:
    """Export an audited candidate through the existing portable package format."""
    from .runtime import Runtime, checksum

    target = Path(output).resolve()
    if target.exists():
        raise ValueError("输出数据包已存在，请使用新的路径")
    audit = audit_database(database)
    if not audit["ok"]:
        return {"audit": audit}
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hsr-candidate-package-") as temporary:
        runtime = Runtime(Path(temporary), "local")
        try:
            runtime.install_snapshot(Path(database), "offline-candidate")
            package = runtime.export_package()
            with package.open("rb") as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination)
        finally:
            runtime.close()
    return {"package": str(target), "sha256": checksum(target), "size": target.stat().st_size, "audit": audit}
