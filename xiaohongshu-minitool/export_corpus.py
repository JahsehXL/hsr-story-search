"""Export the active web snapshot into ordered, independently loaded H5 resources."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data import (  # noqa: E402
    _collapse_mirrored_scripts, _script_sort_key, dialogue_origin, get_catalog, get_mission,
    normalize_text, open_db, search_tokens,
)
from backend.search import ALIASES, KNOWN_ENTITIES, NON_SPEAKER_ENTITIES, STOPWORD_PATTERN, TERM_SYNONYMS  # noqa: E402


class Dictionary:
    def __init__(self):
        self.values = [""]
        self.indices = {"": 0}

    def add(self, value: str) -> int:
        if value not in self.indices:
            self.indices[value] = len(self.values)
            self.values.append(value)
        return self.indices[value]


def write_resource(target: Path, name: str, callback: str, *args) -> int:
    data = ",".join(json.dumps(arg, ensure_ascii=False, separators=(",", ":")) for arg in args)
    data = data.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    path = target / name
    path.write_text(f"self.{callback}({data});\n", encoding="utf-8", newline="\n")
    return path.stat().st_size


def write_index(target: Path, arrays: dict[str, list]) -> list[str]:
    """Keep each lazily loaded index script small enough for the preview runtime."""
    files = []
    budget = 384 * 1024
    for key, values in arrays.items():
        start = 0
        size = 0
        for end, value in enumerate(values):
            item_size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 8
            if end > start and size + item_size > budget:
                name = f"index-{key.lower()}-{start}.js"
                write_resource(target, name, "hsrRegisterIndexPart", key, start, values[start:end])
                files.append(name)
                start = end
                size = 0
            size += item_size
        name = f"index-{key.lower()}-{start}.js"
        write_resource(target, name, "hsrRegisterIndexPart", key, start, values[start:])
        files.append(name)
    return files


def word_character_ranges() -> list[int]:
    """Freeze Python's alphanumeric/space classification for legacy WebViews."""
    ranges = []
    active = False
    for code in range(0x110000):
        char = chr(code)
        included = char.isalnum() or char.isspace() or 0x3400 <= code <= 0x9FFF
        if included != active:
            ranges.append(code)
            active = included
    return ranges


def reader_order(database: Path, mission_id: str, lines: list[dict]) -> tuple:
    first = get_mission(database, mission_id)
    ordered = list(first["lines"])
    for offset in range(len(ordered), first["total"], 1000):
        ordered.extend(get_mission(database, mission_id, offset=offset)["lines"])
    ordered_scripts = first["ordered_scripts"]
    _, aliases = _collapse_mirrored_scripts(sorted(lines, key=_script_sort_key), set(ordered_scripts))
    by_id = {line["id"]: index for index, line in enumerate(lines)}
    order = [by_id[line["id"]] for line in ordered]
    if set(by_id) != {line["id"] for line in ordered} | set(aliases):
        raise ValueError(f"Reader order export differs from web for mission {mission_id}")
    return (
        [] if order == list(range(len(lines))) else order,
        [[by_id[src], by_id[dst]] for src, dst in aliases.items()],
        ordered_scripts, first["optional_scripts"], first["recalls"],
        {line["script"]: line["scene_label"] for line in ordered if "scene_label" in line},
    )


def export(database: Path, target: Path, snapshot: str) -> dict:
    target.mkdir(parents=True, exist_ok=True)
    scripts, speakers, kinds, nodes = (Dictionary() for _ in range(4))
    catalog = get_catalog(database)
    catalog.pop("missions")
    files, missions, shard = [], [], []
    shard_lines = 0
    token_count = 0
    chunks_total = 0
    all_lines = 0
    exceptions = defaultdict(int)
    script_missions = defaultdict(list)
    origins = {}

    def flush():
        nonlocal shard_lines
        if shard:
            index = len(files)
            name = f"corpus-{index:03}.js"
            write_resource(target, name, "hsrRegisterShard", index, shard)
            files.append(name)
            shard.clear()
            shard_lines = 0

    with open_db(database) as conn:
        text_index = {
            row[0]: index for index, row in enumerate(conn.execute(
                "SELECT text FROM lines GROUP BY text "
                "HAVING count(*) > 1 AND length(CAST(text AS BLOB)) >= 25 ORDER BY text"
            ))
        }
        edges = defaultdict(list)
        for script, source, destination, kind in conn.execute("SELECT DISTINCT script,source,target,kind FROM edges"):
            edges[script].append([nodes.add(source), nodes.add(destination), kinds.add(kind)])
        rows = conn.execute("SELECT * FROM missions ORDER BY id").fetchall()
        for row in rows:
            mission = dict(row)
            key = row["id"]
            lines = [dict(line) for line in conn.execute("SELECT * FROM lines WHERE mission_id=? ORDER BY chunk_id,rowid", (key,))]
            if not lines:
                continue
            if shard_lines and shard_lines + len(lines) > 2000:
                flush()
            order, mirrors, ordered, optional, recalls, labels = reader_order(database, key, lines)
            position_in_mission = len(missions)
            missions.append([mission, len(files)])
            context = " · ".join(row[column] for column in ("world", "chapter", "name") if row[column])
            by_chunk = defaultdict(list)
            for line in lines:
                by_chunk[line["chunk_id"]].append(line)
            chunks = []
            previous_chunk = 0
            previous_sentence = 0
            for chunk in conn.execute(
                "SELECT c.id,c.script,c.branch,c.text,f.terms FROM chunks c "
                "JOIN chunks_fts f ON f.rowid=c.id WHERE c.mission_id=? ORDER BY c.id", (key,),
            ):
                script_index = scripts.add(chunk["script"])
                chunk_lines = by_chunk.pop(chunk["id"])
                packed = []
                for line in chunk_lines:
                    original = line["text"]
                    sentence = line["sentence_id"]
                    if origin := dialogue_origin(line["script"], sentence):
                        origins.setdefault(script_index, (origin, set()))[1].add(sentence)
                    sequence, position = line["sequence"], line["position"]
                    stable_source = f"{line['script']}:{sequence}:{position}:{sentence}:{line['kind']}"
                    generated_id = hashlib.sha256(stable_source.encode()).hexdigest()[:32]
                    if generated_id != line["id"]:
                        raise ValueError(f"Unexpected stable ID for {key}/{line['id']}")
                    # Numeric IDs preserve leading zeroes and values outside JS's safe integer range as text.
                    compact_sentence = int(sentence) if sentence.isascii() and sentence.isdigit() and len(sentence) <= 15 and str(int(sentence)) == sentence else sentence
                    if isinstance(compact_sentence, int):
                        previous_sentence, compact_sentence = compact_sentence, compact_sentence - previous_sentence
                    item = [sequence, position, compact_sentence, speakers.add(line["speaker"]),
                            kinds.add(line["kind"]), nodes.add(line["branch"]), nodes.add(line["trigger"]),
                            text_index.get(original, original)]
                    if normalize_text(original) != line["search_text"]:
                        exceptions["search_text"] += 1
                        item.append(line["search_text"])
                    if line["script"] != chunk["script"]:
                        raise ValueError(f"Chunk {chunk['id']} spans more than one script")
                    packed.append(item)
                expected = "\n".join(f"{line['speaker']}：{line['search_text']}" if line["speaker"] else line["search_text"] for line in chunk_lines)
                doc_length = len(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", chunk["terms"]))
                token_count += doc_length
                entry = [chunk["id"] - previous_chunk, script_index, nodes.add(chunk["branch"]), doc_length, packed]
                previous_chunk = chunk["id"]
                if expected != chunk["text"] or search_tokens(f"{context}\n{chunk['text']}".strip()) != chunk["terms"]:
                    exceptions["fts"] += 1
                    entry.extend([chunk["text"], chunk["terms"]])
                chunks.append(entry)
            if by_chunk:
                raise ValueError(f"Missing indexed chunks for {key}")
            mission_scripts = sorted({line["script"] for line in lines})
            for script in mission_scripts:
                script_missions[scripts.add(script)].append(position_in_mission)
            edge_rows = [[scripts.add(script), edges[script]] for script in mission_scripts if script in edges]
            shard.append([
                position_in_mission, chunks, order, mirrors, edge_rows,
                [scripts.add(script) for script in ordered], [scripts.add(script) for script in optional], recalls,
                [[scripts.add(script), label] for script, label in sorted(labels.items())],
            ])
            all_lines += len(lines)
            chunks_total += len(chunks)
            shard_lines += len(lines)
    flush()
    index_files = write_index(target, {
        "missions": missions, "scripts": scripts.values, "speakers": speakers.values, "kinds": kinds.values,
        "scriptMissions": [owners[0] if len(owners) == 1 else owners
                           for owners in (script_missions[index] for index in range(len(scripts.values)))],
        "nodes": nodes.values, "texts": list(text_index),
    })
    manifest = {
        "snapshot": snapshot, "catalog": {k: catalog[k] for k in ("worlds", "types", "versions", "characters")},
        "shards": files, "lines": all_lines, "chunks": chunks_total,
        "avgDocLength": token_count / chunks_total,
        "aliases": ALIASES, "entities": KNOWN_ENTITIES, "nonSpeakers": sorted(NON_SPEAKER_ENTITIES),
        "stopwords": STOPWORD_PATTERN.pattern, "synonyms": TERM_SYNONYMS, "wordRanges": word_character_ranges(),
        "dialogueOrigins": {index: [origin, sorted(ids)] for index, (origin, ids) in origins.items()},
        "indexFiles": index_files,
    }
    write_resource(target, "catalog.js", "hsrRegisterCatalog", manifest)
    return {"snapshot": snapshot, "missions": len(missions), "lines": all_lines, "chunks": chunks_total,
            "shards": len(files), "index_files": index_files, "multi_origin_scripts": len(origins), "exceptions": dict(exceptions)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--snapshot")
    args = parser.parse_args()
    snapshot = args.snapshot or json.loads((ROOT / "data/current.json").read_text(encoding="utf-8"))["snapshot"]
    if not re.fullmatch(r"[a-z0-9_-]+", snapshot):
        parser.error("invalid snapshot ID")
    database = ROOT / "data/snapshots" / snapshot / "story.db"
    print(json.dumps(export(database, args.output, snapshot), ensure_ascii=False))


if __name__ == "__main__":
    main()
