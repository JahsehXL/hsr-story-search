"""Attributed mission release versions, independent of game data revision IDs."""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path

DEFAULT_METADATA = Path(__file__).parent / "resources" / "mission_versions.json"


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.translate(str.maketrans({"•": "·", "・": "·"}))
    return re.sub(r"\s+", "", value)


def load_metadata(path: Path | str | None = None) -> list[dict]:
    document = json.loads(Path(path or DEFAULT_METADATA).read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("Unsupported mission version metadata schema")
    entries = document["missions"]
    seen = set()
    for entry in entries:
        if (entry["id"] in seen or not re.fullmatch(r"\d+\.\d+", entry["version"])
                or entry.get("status") != "verified" or not entry.get("source", "").startswith("https://")
                or not entry.get("retrieved_at") or not entry.get("method")):
            raise ValueError("Invalid or duplicate mission version metadata")
        seen.add(entry["id"])
    return entries


def apply_versions(connection: sqlite3.Connection, metadata: list[dict] | None = None) -> int:
    """Fill unknown releases after checking ID and mission identity, without committing."""
    updated = 0
    for entry in load_metadata() if metadata is None else metadata:
        row = connection.execute(
            "SELECT name,chapter,world,type,version,version_status FROM missions WHERE id=?", (entry["id"],)
        ).fetchone()
        if row is None or row[4] or row[5] not in ("", "unknown"):
            continue
        actual = tuple(normalize_name(str(value or "")) for value in row[:4])
        expected = tuple(normalize_name(entry.get(key, "")) for key in ("name", "chapter", "world", "type"))
        if actual != expected:
            continue
        connection.execute(
            "UPDATE missions SET version=?,version_source=?,version_status='verified' WHERE id=?",
            (entry["version"], entry["source"], entry["id"]),
        )
        updated += 1
    return updated
