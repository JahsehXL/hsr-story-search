"""Build a traceable Chinese dialogue dataset from expanded game JSON files."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


SCHEMA_VERSION = 1
TYPE_NAMES = {"Main": "开拓任务", "Branch": "支线任务", "Companion": "同行任务", "Daily": "每日任务"}
TAG_PATTERN = re.compile(r"<[^>]*>")
RUBY_PATTERN = re.compile(r"\{RUBY_B#([^}]*)\}(.*?)\{RUBY_E#\}")


def normalize_text(text: str) -> str:
    text = RUBY_PATTERN.sub(r"\2（\1）", text)
    text = TAG_PATTERN.sub("", text)
    text = re.sub(r"\{(?:F|M)#([^}]*)\}", r"\1", text)
    text = text.replace("{NICKNAME}", "开拓者").replace("\\n", "\n")
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def search_tokens(text: str) -> str:
    """Emit character and bigram terms so Chinese short queries work with FTS5."""
    tokens: set[str] = set()
    for run in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9_]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", run):
            tokens.update(run)
            tokens.update(run[index:index + 2] for index in range(len(run) - 1))
        else:
            tokens.add(run)
    return " ".join(sorted(tokens))


class DatasetConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def open_db(path: Path | str, readonly: bool = True) -> sqlite3.Connection:
    target = Path(path).resolve()
    if readonly:
        conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=30, factory=DatasetConnection)
    else:
        conn = sqlite3.connect(target, timeout=30, factory=DatasetConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return list(value.values()) if isinstance(value, dict) else value


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE missions(id TEXT PRIMARY KEY,name TEXT NOT NULL,chapter TEXT NOT NULL,
            world TEXT NOT NULL,type TEXT NOT NULL,version TEXT NOT NULL DEFAULT '',
            version_source TEXT NOT NULL DEFAULT '',version_status TEXT NOT NULL DEFAULT 'unknown');
        CREATE TABLE lines(id TEXT PRIMARY KEY,mission_id TEXT NOT NULL,script TEXT NOT NULL,
            sequence INTEGER NOT NULL,position INTEGER NOT NULL,sentence_id TEXT NOT NULL,
            speaker TEXT NOT NULL,text TEXT NOT NULL,search_text TEXT NOT NULL,kind TEXT NOT NULL,
            branch TEXT NOT NULL,trigger TEXT NOT NULL,chunk_id INTEGER);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY,stable_id TEXT UNIQUE NOT NULL,
            mission_id TEXT NOT NULL,script TEXT NOT NULL,sequence INTEGER NOT NULL,branch TEXT NOT NULL,
            text TEXT NOT NULL,input_text TEXT NOT NULL,content_hash TEXT NOT NULL,line_ids TEXT NOT NULL);
        CREATE TABLE chunk_lines(chunk_id INTEGER NOT NULL,line_id TEXT NOT NULL,
            PRIMARY KEY(chunk_id,line_id));
        CREATE TABLE edges(script TEXT NOT NULL,source TEXT NOT NULL,target TEXT NOT NULL,kind TEXT NOT NULL);
        CREATE TABLE recalls(id TEXT PRIMARY KEY,mission_id TEXT NOT NULL,name TEXT NOT NULL,
            performance_id TEXT NOT NULL,verified INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE VIRTUAL TABLE chunks_fts USING fts5(terms, tokenize='unicode61');
        CREATE INDEX line_mission ON lines(mission_id,script,sequence,position);
        CREATE INDEX line_sentence ON lines(sentence_id);
        CREATE INDEX line_chunk ON lines(chunk_id);
        CREATE INDEX chunk_mission ON chunks(mission_id);
        CREATE INDEX chunk_hash ON chunks(content_hash);
        CREATE INDEX chunk_line_reverse ON chunk_lines(line_id);
        CREATE INDEX edge_script ON edges(script);
    """)


class DatasetBuilder:
    def __init__(self, root: Path, conn: sqlite3.Connection):
        self.root = root
        self.conn = conn
        self.texts: dict[str, str] = {}
        for name in ("TextMapCHS.json", "TextMapMainCHS.json"):
            path = root / "TextMap" / name
            if path.exists():
                self.texts.update(json.loads(path.read_text(encoding="utf-8-sig")))
        self.sentences: dict[str, dict] = {}
        self.used_sentences: set[str] = set()
        self.missions: dict[str, dict] = {}
        self.unresolved_sentences = 0
        self.chunk_count = 0
        self.line_count = 0
        self.referenced_performance: dict[str, set[str]] = {}
        self.perf_paths: dict[str, tuple[str, str]] = {}

    def resolve(self, value: object) -> str:
        if isinstance(value, dict):
            value = value.get("Hash", "")
        return self.texts.get(str(value), "")

    def load_metadata(self) -> None:
        chapters = {str(row["ID"]): row for row in read_records(self.root / "ExcelOutput/MissionChapterConfig.json")}
        worlds = {str(row["ID"]): self.resolve(row.get("WorldName")) for row in read_records(self.root / "ExcelOutput/WorldDataConfig.json")}
        for row in read_records(self.root / "ExcelOutput/MainMission.json"):
            mission_id = str(row["MainMissionID"])
            chapter = chapters.get(str(row.get("ChapterID")), {})
            self.add_mission(mission_id, {
                "name": self.resolve(row.get("Name")) or f"任务 {mission_id}",
                "chapter": self.resolve(chapter.get("ChapterName")),
                "world": worlds.get(str(row.get("WorldID")), "") or self.resolve(chapter.get("StageName")),
                "type": TYPE_NAMES.get(row.get("Type", ""), row.get("Type", "其他任务")),
            })
        for row in read_records(self.root / "ExcelOutput/TalkSentenceConfig.json"):
            sentence_id = row.get("TalkSentenceID")
            text = self.resolve(row.get("TalkSentenceText"))
            if sentence_id is None or not text:
                self.unresolved_sentences += 1
                continue
            self.sentences[str(sentence_id)] = {
                "speaker": normalize_text(self.resolve(row.get("TextmapTalkSentenceName"))) or "开拓者",
                "text": text,
            }
        for name in ("PerformanceA.json", "PerformanceC.json", "PerformanceCG.json", "PerformanceD.json", "PerformanceDS.json", "PerformanceE.json"):
            for row in read_records(self.root / "ExcelOutput" / name):
                pid = str(row.get("PerformanceID", ""))
                path = str(row.get("PerformancePath", ""))
                if len(pid) == 9 and pid.isdigit() and "Mission/" in path:
                    parts = path.split("/")
                    if "Mission" in parts:
                        mid = parts[parts.index("Mission") + 1]
                        if pid[1] == "0":
                            self.perf_paths.setdefault(pid[0] + pid[2] + pid[4:9], (mid, path))
                        if pid.startswith("103"):
                            self.perf_paths.setdefault(pid[:5] + pid[-2:], (mid, path))

    def add_mission(self, mission_id: str, details: dict | None = None) -> None:
        if mission_id in self.missions:
            return
        item = {"id": mission_id, "name": f"任务 {mission_id}", "chapter": "", "world": "", "type": "其他任务"}
        if details:
            item.update(details)
        self.missions[mission_id] = item
        self.conn.execute("INSERT INTO missions(id,name,chapter,world,type) VALUES(:id,:name,:chapter,:world,:type)", item)

    def make_line(self, sentence_id: object, mission_id: str, script: str, sequence: int,
                  position: int, branch: str, kind: str, trigger: str = "") -> dict | None:
        sentence_id = str(sentence_id)
        sentence = self.sentences.get(sentence_id)
        if not sentence:
            return None
        self.used_sentences.add(sentence_id)
        stable_source = f"{script}:{sequence}:{position}:{sentence_id}:{kind}"
        return {
            "id": hashlib.sha256(stable_source.encode()).hexdigest()[:32],
            "mission_id": mission_id, "script": script, "sequence": sequence,
            "position": position, "sentence_id": sentence_id,
            "speaker": sentence["speaker"], "text": sentence["text"],
            "search_text": normalize_text(sentence["text"]), "kind": kind,
            "branch": branch, "trigger": trigger,
        }

    def save_chunk(self, lines: list[dict]) -> None:
        if not lines:
            return
        first = lines[0]
        mission = self.missions[first["mission_id"]]
        text = "\n".join(f"{row['speaker']}：{row['search_text']}" for row in lines)
        context = " · ".join(value for value in (mission["world"], mission["chapter"], mission["name"]) if value)
        input_text = f"{context}\n{text}".strip()
        line_ids = [row["id"] for row in lines]
        stable_id = hashlib.sha256("|".join(line_ids).encode()).hexdigest()
        row = self.conn.execute("""INSERT INTO chunks(stable_id,mission_id,script,sequence,branch,text,
            input_text,content_hash,line_ids) VALUES(?,?,?,?,?,?,?,?,?)""", (
                stable_id, first["mission_id"], first["script"], first["sequence"], first["branch"],
                text, input_text, hashlib.sha256(input_text.encode()).hexdigest(), json.dumps(line_ids),
            ))
        chunk_id = row.lastrowid
        self.conn.executemany("INSERT INTO chunk_lines VALUES(?,?)", [(chunk_id, item) for item in line_ids])
        self.conn.executemany("""INSERT INTO lines(id,mission_id,script,sequence,position,sentence_id,speaker,
            text,search_text,kind,branch,trigger,chunk_id) VALUES(:id,:mission_id,:script,:sequence,:position,
            :sentence_id,:speaker,:text,:search_text,:kind,:branch,:trigger,:chunk_id)""",
            [dict(item, chunk_id=chunk_id) for item in lines])
        self.conn.execute("INSERT INTO chunks_fts(rowid,terms) VALUES(?,?)", (chunk_id, search_tokens(input_text)))
        self.chunk_count += 1
        self.line_count += len(lines)

    def save_lines(self, lines: list[dict]) -> None:
        buffer: list[dict] = []
        size = 0
        for line in lines:
            # Alternatives are indexed independently, never concatenated as a conversation.
            if line["kind"] == "option":
                self.save_chunk(buffer)
                buffer, size = [], 0
                self.save_chunk([line])
                continue
            length = len(line["search_text"]) + len(line["speaker"]) + 2
            if buffer and size + length > 700:
                self.save_chunk(buffer)
                buffer, size = [], 0
            buffer.append(line)
            size += length
        self.save_chunk(buffer)

    @staticmethod
    def nested_triggers(value: object) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            if str(value.get("$type", "")).endswith("TriggerCustomString"):
                text = value.get("CustomString", {}).get("Value")
                if text:
                    found.add(str(text))
            for child in value.values():
                found.update(DatasetBuilder.nested_triggers(child))
        elif isinstance(value, list):
            for child in value:
                found.update(DatasetBuilder.nested_triggers(child))
        return found

    def process_file(self, path: Path) -> None:
        script = path.relative_to(self.root).as_posix()
        parts = path.relative_to(self.root).parts
        mission_id = parts[parts.index("Mission") + 1]
        self.add_mission(mission_id)
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        sequences = data.get("OnStartSequece", [])
        regular_ids: set[str] = set()
        for sequence in sequences:
            for task in sequence.get("TaskList", []):
                for key in ("SimpleTalkList", "OptionList"):
                    regular_ids.update(str(item.get("TalkSentenceID")) for item in task.get(key, []))
        references: set[str] = set()
        for sequence_index, sequence in enumerate(sequences):
            tasks = sequence.get("TaskList", [])
            waits = [str(task.get("CustomString", {}).get("Value", "")) for task in tasks
                     if str(task.get("$type", "")).endswith("WaitCustomString")]
            branch = next((value for value in waits if value), "")
            node = branch or f"sequence:{sequence_index}"
            lines: list[dict] = []
            for task_index, task in enumerate(tasks):
                source_position = task_index * 10000
                task_type = str(task.get("$type", ""))
                if task_type.endswith("WaitCustomString") and branch.startswith("TalkSentence_"):
                    sentence_id = branch.removeprefix("TalkSentence_")
                    if sentence_id not in regular_ids:
                        line = self.make_line(sentence_id, mission_id, script, sequence_index, source_position, branch, "dialogue")
                        if line:
                            lines.append(line)
                if task_type.endswith(("PlayAndWaitSimpleTalk", "PlaySimpleTalk")):
                    for item_index, item in enumerate(task.get("SimpleTalkList", [])):
                        line = self.make_line(item.get("TalkSentenceID"), mission_id, script, sequence_index, source_position + item_index, branch, "dialogue")
                        if line:
                            lines.append(line)
                if task_type.endswith("PlayOptionTalk"):
                    for item_index, item in enumerate(task.get("OptionList", [])):
                        trigger = str(item.get("TriggerCustomString") or "")
                        line = self.make_line(item.get("TalkSentenceID"), mission_id, script, sequence_index, source_position + item_index, branch, "option", trigger)
                        if line:
                            lines.append(line)
                        if trigger:
                            references.add(trigger)
                            self.conn.execute("INSERT INTO edges VALUES(?,?,?,?)", (script, node, trigger, "option"))
                for trigger in self.nested_triggers(task):
                    references.add(trigger)
                    self.conn.execute("INSERT INTO edges VALUES(?,?,?,?)", (script, node, trigger, "continuation"))
                # Explicit performance IDs are evidence for a script-level recall association.
                for field in ("PerformanceID", "StoryPerformanceID"):
                    if task.get(field):
                        self.referenced_performance.setdefault(str(task[field]), set()).add(mission_id)
            references.update(waits)
            self.save_lines(lines)
        for index, reference in enumerate(sorted(references)):
            match = re.fullmatch(r"TalkSentence_(\d+)", reference)
            if match and match[1] not in regular_ids:
                exists = self.conn.execute("SELECT 1 FROM lines WHERE script=? AND sentence_id=? LIMIT 1", (script, match[1])).fetchone()
                if not exists:
                    line = self.make_line(match[1], mission_id, script, len(sequences) + index, 0, reference, "reference")
                    if line:
                        self.save_lines([line])

    def import_timeline_sentences(self) -> None:
        from collections import defaultdict
        def _eligible(mid: str) -> bool:
            m = self.missions.get(mid)
            if not m or len(mid) != 7 or not mid.isdigit():
                return False
            if mid[1] == "0" and mid[3] == "4":
                return True
            if mid.startswith(("103", "104", "105", "202", "203", "204", "205")) and m.get("type") in {"开拓任务", "Gap", "同行任务"}:
                return True
            return False
        p5_to_mid: dict[str, str] = {}
        for mid in sorted(self.missions):
            if not _eligible(mid):
                continue
            if mid[1] == "0" and mid[3] in {"3", "4"}:
                p5_to_mid.setdefault(mid[0] + mid[2] + mid[4:], mid)
            if mid.startswith("103"):
                p5_to_mid.setdefault(mid[:5], mid)
        existing_p7: dict[str, str] = {}
        for p7, mid in self.conn.execute("SELECT DISTINCT substr(sentence_id,1,7), mission_id FROM lines WHERE length(sentence_id)=9"):
            if p7 and p7.isdigit() and _eligible(mid) and p7[:5] in p5_to_mid:
                existing_p7.setdefault(p7, mid)
        unused_by_p7: dict[str, list[str]] = defaultdict(list)
        for sid in sorted(self.sentences, key=lambda s: int(s) if s.isdigit() else 0):
            if sid not in self.used_sentences and len(sid) == 9 and sid.isdigit():
                p7 = sid[:7]
                if p7 in existing_p7 or p7[:5] in p5_to_mid or (p7 in self.perf_paths and _eligible(self.perf_paths[p7][0])):
                    unused_by_p7[p7].append(sid)
        for p7 in sorted(unused_by_p7):
            sids = unused_by_p7[p7]
            mid = existing_p7.get(p7)
            perf_script = ""
            if p7 in self.perf_paths and _eligible(self.perf_paths[p7][0]):
                if not mid:
                    mid = self.perf_paths[p7][0]
                if self.perf_paths[p7][0] == mid:
                    perf_script = self.perf_paths[p7][1]
            if not mid:
                mid = p5_to_mid.get(p7[:5], "")
            if not mid or not _eligible(mid):
                continue
            existing = self.conn.execute(
                "SELECT sentence_id, script, sequence, position, branch, kind FROM lines WHERE mission_id=? AND sentence_id LIKE ? ORDER BY CAST(sentence_id AS INTEGER)",
                (mid, f"{p7}%"),
            ).fetchall()
            if existing:
                first_sid = int(existing[0]["sentence_id"])
                base_script = existing[0]["script"]
                grouped: dict[tuple[str, int, str], list[dict]] = defaultdict(list)
                before_sids = [sid for sid in sids if int(sid) < first_sid]
                start_pos = 1
                if existing[0]["sequence"] == 0 and existing[0]["branch"] == "" and existing[0]["position"] <= len(before_sids):
                    start_pos = existing[0]["position"] - len(before_sids)
                for idx, sid in enumerate(before_sids):
                    line = self.make_line(sid, mid, base_script, 0, start_pos + idx, "", "dialogue")
                    if line:
                        grouped[(base_script, 0, "")].append(line)
                non_option_anchors = [row for row in existing if row["kind"] != "option"]
                for sid in sids:
                    sid_int = int(sid)
                    if sid_int < first_sid:
                        continue
                    anchor = None
                    for row in non_option_anchors:
                        if int(row["sentence_id"]) < sid_int:
                            anchor = row
                        else:
                            break
                    if anchor is None:
                        for row in existing:
                            if int(row["sentence_id"]) < sid_int:
                                anchor = row
                            else:
                                break
                    if anchor is not None:
                        pos = int(anchor["position"]) + (sid_int - int(anchor["sentence_id"]))
                        key = (anchor["script"], int(anchor["sequence"]), anchor["branch"])
                        line = self.make_line(sid, mid, key[0], key[1], pos, key[2], "dialogue")
                        if line:
                            grouped[key].append(line)
                for key_lines in grouped.values():
                    self.save_lines(key_lines)
            else:
                script = perf_script or f"Story/Discussion/Mission/{mid}/DS{mid}{p7[-2:]}.json"
                lines = []
                for idx, sid in enumerate(sids):
                    line = self.make_line(sid, mid, script, 0, idx, "", "dialogue")
                    if line:
                        lines.append(line)
                self.save_lines(lines)

    def save_recalls(self) -> None:
        for row in read_records(self.root / "ExcelOutput/PerformanceRecallData.json"):
            performance_id = str(row.get("PerformanceID", ""))
            # Unlock conditions alone do not prove that the recalled scene belongs to a mission.
            for mission_id in sorted(self.referenced_performance.get(performance_id, set())):
                self.conn.execute("INSERT OR IGNORE INTO recalls VALUES(?,?,?,?,0)", (
                    f"{row['ID']}:{mission_id}", mission_id, normalize_text(self.resolve(row.get("Name"))), performance_id,
                ))


def import_data(source: Path, output: Path, progress: Callable[[dict], None] | None = None) -> dict:
    source, output = Path(source).resolve(), Path(output).resolve()
    if not (source / "TextMap/TextMapCHS.json").is_file():
        candidates = [child for child in source.iterdir() if child.is_dir() and (child / "TextMap/TextMapCHS.json").is_file()] if source.is_dir() else []
        if len(candidates) == 1:
            source = candidates[0]
        else:
            raise ValueError("请选择包含 TextMap、ExcelOutput、Story 和 Config 的数据目录")
    if not (source / "ExcelOutput/TalkSentenceConfig.json").is_file():
        raise ValueError("数据缺少 TalkSentenceConfig.json")
    if output.exists():
        raise ValueError("输出数据库已存在，请使用新的暂存路径")
    output.parent.mkdir(parents=True, exist_ok=True)

    def report(phase: str, done: int, total: int, message: str) -> None:
        if progress:
            progress({"phase": phase, "done": done, "total": total, "message": message})

    conn = open_db(output, readonly=False)
    try:
        create_schema(conn)
        builder = DatasetBuilder(source, conn)
        report("metadata", 0, 1, "读取中文文字和任务资料")
        builder.load_metadata()
        conn.commit()
        files = sorted(set(source.glob("Config/Level/Mission/*/Act/*.json")) |
                       set(source.glob("Story/Discussion/Mission/*/*.json")) |
                       set(source.glob("Story/Mission/*/*.json")))
        if not files:
            raise ValueError("数据目录中没有找到任务剧情脚本")
        for index, path in enumerate(files, 1):
            builder.process_file(path)
            if index % 200 == 0 or index == len(files):
                conn.commit()
                report("scripts", index, len(files), f"已解析 {index} / {len(files)} 个剧情脚本")
        builder.import_timeline_sentences()
        conn.commit()
        report("unlinked", 0, 1, "统计未关联文字及整理回顾候选")
        unlinked_sentence_count = len(set(builder.sentences) - builder.used_sentences)
        builder.save_recalls()
        from .version_metadata import apply_versions
        verified_versions = apply_versions(conn)
        stats = {
            "missions": conn.execute("SELECT count(*) FROM missions").fetchone()[0],
            "lines": builder.line_count, "chunks": builder.chunk_count,
            "characters": conn.execute("SELECT count(DISTINCT speaker) FROM lines").fetchone()[0],
            "scripts": len(files), "source": source.name, "source_kind": "user-imported snapshot",
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "unresolved_sentences": builder.unresolved_sentences,
            "schema_version": SCHEMA_VERSION,
            "excluded_unlinked_sentences": unlinked_sentence_count,
            "verified_versions": verified_versions,
        }
        stats.update(story_counts(conn))
        conn.executemany("INSERT INTO info(key,value) VALUES(?,?)", [(key, json.dumps(value, ensure_ascii=False)) for key, value in stats.items()])
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        conn.commit()
        conn.execute("ANALYZE")
        conn.commit()
        report("complete", 1, 1, "剧情数据库已生成")
        return stats
    finally:
        conn.close()


def story_counts(conn: sqlite3.Connection) -> dict:
    counts = {}
    for table in ("lines", "chunks"):
        total, unlinked = conn.execute(f"SELECT count(*),coalesce(sum(script='unlinked' OR mission_id='unlinked'),0) FROM {table}").fetchone()
        counts[f"narrative_{table}"] = total - unlinked
        counts[f"unlinked_{table}"] = unlinked
    return counts


def version_sort_key(version: str) -> tuple[int, int]:
    parts = re.findall(r"\d+", str(version or ""))
    if len(parts) >= 2:
        return (int(parts[0]), int(parts[1]))
    if len(parts) == 1:
        return (int(parts[0]), 0)
    return (0, 0)


def get_catalog(path: Path | str) -> dict:
    with open_db(path) as conn:
        info = {row["key"]: json.loads(row["value"]) for row in conn.execute("SELECT * FROM info")}
        info.update(story_counts(conn))
        result = {"stats": info}
        result["missions"] = [dict(row) for row in conn.execute("SELECT m.* FROM missions m JOIN (SELECT DISTINCT mission_id FROM chunks WHERE script!='unlinked' AND mission_id!='unlinked') story ON story.mission_id=m.id ORDER BY m.id")]
        for key, column in (("worlds", "world"), ("types", "type")):
            result[key] = sorted({mission[column] for mission in result["missions"] if mission[column]})
        result["versions"] = sorted({mission["version"] for mission in result["missions"] if mission["version"]}, key=version_sort_key, reverse=True)
        result["characters"] = [row[0] for row in conn.execute("SELECT speaker FROM lines WHERE script!='unlinked' AND mission_id!='unlinked' GROUP BY speaker ORDER BY count(*) DESC LIMIT 400")]
        return result


def _script_sort_key(row: dict) -> tuple:
    stem = str(row.get("script", "")).rsplit("/", 1)[-1].split(".")[0]
    nums = [int(n) for n in re.findall(r"\d+", stem)]
    return (nums[-1] if nums else 0, row.get("script", ""), row.get("sequence", 0), row.get("position", 0))


def get_mission(path: Path | str, mission_id: str, line_id: str | None = None,
                script: str | None = None, offset: int = 0, limit: int = 1000) -> dict:
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    with open_db(path) as conn:
        mission = conn.execute("SELECT * FROM missions WHERE id=?", (str(mission_id),)).fetchone()
        if not mission:
            raise KeyError(mission_id)
        condition = "mission_id=?"
        args: list = [str(mission_id)]
        if script:
            condition += " AND script=?"
            args.append(script)
        all_lines = [dict(row) for row in conn.execute(f"SELECT * FROM lines WHERE {condition}", args)]
        all_lines.sort(key=_script_sort_key)
        total = len(all_lines)
        if line_id:
            for idx, item in enumerate(all_lines):
                if item["id"] == line_id:
                    offset = max(0, idx - limit // 3)
                    break
        lines = all_lines[offset:offset + limit]
        scripts = sorted({row["script"] for row in lines})
        edges = []
        if scripts:
            placeholders = ",".join("?" for _ in scripts)
            edges = [dict(row) for row in conn.execute(f"SELECT DISTINCT * FROM edges WHERE script IN ({placeholders})", scripts)]
        return {"mission": dict(mission), "lines": lines, "edges": edges,
                "recalls": [dict(row) for row in conn.execute("SELECT * FROM recalls WHERE mission_id=?", (str(mission_id),))],
                "scripts": [row[0] for row in conn.execute("SELECT DISTINCT script FROM lines WHERE mission_id=? ORDER BY script", (str(mission_id),))],
                "total": total, "offset": offset, "has_more": offset + len(lines) < total}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python -m backend.data SOURCE OUTPUT")
    def progress_console(event: dict) -> None:
        print(json.dumps(event, ensure_ascii=True), flush=True)
    print(json.dumps(import_data(Path(sys.argv[1]), Path(sys.argv[2]), progress_console), ensure_ascii=True))
