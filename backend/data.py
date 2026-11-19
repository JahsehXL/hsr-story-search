"""Build a traceable Chinese dialogue dataset from expanded game JSON files."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .version_metadata import load_metadata, normalize_name


SCHEMA_VERSION = 1
IMPORTER_VERSION = 8
MISSION_TRIGGER_PATTERN = "Config/Level/Mission/*/Mission_*.json"
SCRIPT_PATTERNS = (MISSION_TRIGGER_PATTERN, "Config/Level/Mission/*/Act/*.json",
                   "Story/Discussion/Mission/*/*.json", "Story/Mission/*/*.json")
TYPE_NAMES = {"Main": "开拓任务", "Branch": "支线任务", "Companion": "同行任务", "Daily": "每日任务"}
TAG_PATTERN = re.compile(r"</?(?:color|size|b|i|u|s|align|alpha|voffset|font|material|link|noparse|unbreak|unbreakable|icon)(?=[\s=>])[^>]*>", re.I)
RUBY_PATTERN = re.compile(r"\{RUBY_B#([^}]*)\}(.*?)\{RUBY_E#\}")
VERIFIED_SCENES = (
    # 雅利洛-Ⅵ「躲得过初一，躲不过十五」，与布洛妮娅对峙及战后对白。
    # TalkSentenceConfig carries the dialogue, but the exported scripts omit its references.
    ("1010301", "Verified/Mission/1010301/2011106.json", (
        201110501, 201110502, 201110503, 201110504, 201110505, 201110506, 201110507,
        201110508, 201110510, 201110511, 201110512, 201110513, 201110514, 201110515,
        201110601, 201110602, 201110603, 201110604, 201110605,
    )),
)
VERIFIED_SCRIPT_OWNERS = {
    # The game packages these performances under the preceding mission's asset directory.
    "Story/Discussion/Mission/1032801/DS103280108.json": "1032802",
    "Story/Mission/1032801/Story103280110.json": "1032802",
}
VERIFIED_TIMELINES = {
    "1380110": ("1033801", "Story/Discussion/Mission/1033801/DS103380110.json"),
    # 《火车大劫案》共有三段过场。源脚本存在，台词见任务实录。
    "1032302": ("1032301", "Story/Mission/1032301/Story103230102.json"),
    "1032303": ("1032301", "Story/Mission/1032301/Story103230103.json"),
    "1032304": ("1032301", "Story/Discussion/Mission/1032301/DS103230104.json"),
    # 《落水猴》实录可确认任务归属。教孩子唱歌这段的演出位置待核。
    "1034378": ("1034204", ""),
    "1034386": ("1034204", "Story/Discussion/Mission/1034204/DS103430086.json"),
    # These 2.4 scenes have their own performance graphs. Compressed IDs can
    # accidentally match 2.5 performance IDs and point at unrelated graphs.
    "2220305": ("2022003", "Story/Discussion/Mission/2022003/DS202200305.json"),
    "2220401": ("2022004", "Story/Discussion/Mission/2022004/DS202200401.json"),
    "2220501": ("2022005", "Story/Discussion/Mission/2022005/DS202200501.json"),
    "2220502": ("2022005", "Story/Discussion/Mission/2022005/DS202200502.json"),
    "2220503": ("2022005", "Story/Discussion/Mission/2022005/DS202200503.json"),
    "2220504": ("2022005", "Story/Discussion/Mission/2022005/DS202200504.json"),
    "2220505": ("2022005", "Story/Mission/2022005/Story202200505.json"),
    "2220506": ("2022005", "Config/Level/Mission/2022005/Act/Act202200506.json"),
    "2220602": ("2022006", "Story/Mission/2022006/Story202200602.json"),
    "2220604": ("2022006", "Story/Discussion/Mission/2022006/DS202200604.json"),
}
VERIFIED_TIMELINE_INSERTS = {
    # 《异乡异客》：黄泉在谈及「似曾相识」之后的共用收尾。
    "Story/Mission/1034109/Story10341091602.playable": range(103424001, 103424008),
}
VERIFIED_SCENE_LABELS = {
    "Story/Discussion/Mission/2022005/DS202200503.json::timeline-2220503": "比试结果 · 彦卿获胜",
    "Story/Discussion/Mission/2022005/DS202200504.json::timeline-2220504": "比试结果 · 云璃获胜",
    "Config/Level/Mission/2022005/Act/Act202200506.json::timeline-2220506": "比试中的战斗对白",
}
VERIFIED_SCENE_ORDER = {
    "Config/Level/Mission/2022005/Act/Act202200506.json::timeline-2220506": 202200502.5,
    "Story/Discussion/Mission/1032301/DS103230104.json::timeline-1032304": 103230102.5,
    "Story/Discussion/Mission/1032601/DS103260107.json::timeline-1032607": 103260104.5,
}
VERIFIED_BATTLE_OWNERS = {"20332072": "1036105", "20431070": "1040408"}
NARRATIVE_TYPES = {"开拓任务", "Gap", "同行任务"}
SINGLE_SENTENCE_TASKS = {"PlayMultiVoiceTalk", "PerformanceTransition", "PlayScreenTransfer", "PerformanceEndBlackText"}
TALK_LIST_TASKS = {"PlayAndWaitSimpleTalk", "PlaySimpleTalk", "PlayMissionTalk", "PlayAndWaitRogueSimpleTalk", "PlayRogueSimpleTalk"}
OPTION_LIST_TASKS = {"PlayOptionTalk", "PlayRogueOptionTalk"}
EXTRA_DIALOGUE_TASKS = {"PlayFullScreenTransfer", "PlayNPCBubbleTalk", "PlayNPCSingleBubbleTalk", "PlayOrigamiTraceTalk"}
DIALOGUE_TASK_TYPES = SINGLE_SENTENCE_TASKS | TALK_LIST_TASKS | OPTION_LIST_TASKS | EXTRA_DIALOGUE_TASKS
CONTAINER_TASK_KEYS: dict[str, tuple[str, ...]] = {
    "PredicateTaskList": ("SuccessTaskList", "FailedTaskList"),
    "RandomConfig": ("TaskList",),
    "SequenceConfig": ("TaskList",),
    "SelectorConfig": ("TaskList",),
    "ScrTrfActTaskList": ("TaskList",),
    "PlaySequenceDialogue": ("Dialogues",),
    "PropSetupTrigger": ("OnTriggerEnter",),
    "NpcSetupTrigger": ("OnTriggerEnter",),
    "PropSetupUITrigger": ("ButtonCallback",),
    "PropStateExecute": ("Execute",),
    "AddFinishMissionData_ConsumeItem": ("OnSubmitConfirm", "OnSubmitCancel"),
    "ConsumeMissionItemPerformance": ("OnSubmitConfirm", "OnSubmitCancel"),
    "ConsumeOrigamiItem": ("OnSubmitConfirm", "OnSubmitCancel"),
    "WaitGroupEvent": ("OnEvent",),
    "WaitPhotoGraphResult": ("OnSuccess",),
    "WaitMissionCustomValueChange": ("OnChange",),
    "WaitFloorCustomValueChange": ("OnChange",),
    "WaitFloorCustomValueChangeV2": ("OnChange",),
    "WaitFloorSavedValueChangeV2": ("OnChange",),
    "ShowEnvBuffDialog": ("OnCancel",),
    "HeliobusSNSQuickPost": ("OnPost",),
    "NpcToPlayerDistanceTrigger": ("FarTask",),
    "PlayFullScreenTransfer": ("Action",),
}
MUTUALLY_EXCLUSIVE_PAIR_KEYS: dict[str, tuple[str, str]] = {
    "PredicateTaskList": ("SuccessTaskList", "FailedTaskList"),
    "AddFinishMissionData_ConsumeItem": ("OnSubmitConfirm", "OnSubmitCancel"),
    "ConsumeMissionItemPerformance": ("OnSubmitConfirm", "OnSubmitCancel"),
    "ConsumeOrigamiItem": ("OnSubmitConfirm", "OnSubmitCancel"),
}
PERFORMANCE_NAME = re.compile(r"(?:Act|DS|Story)(\d{9})\.json")
PACKED_STORY_MISSION = re.compile(r"([12])0([345])[34](\d{3})")


def normalize_text(text: str) -> str:
    text = RUBY_PATTERN.sub(r"\2（\1）", text)
    text = TAG_PATTERN.sub("", text)
    text = re.sub(r"\{(?:F|M)#([^}]*)\}(?=\{[FM]#)", r"\1 ", text)
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
    def __init__(self, root: Path, conn: sqlite3.Connection, baseline: Path | None = None):
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
        self.verified_missions: dict[str, dict] = {}
        self.unresolved_sentences = 0
        self.chunk_count = 0
        self.line_count = 0
        self.referenced_performance: dict[str, set[str]] = {}
        self.performance_owners: dict[str, str] = {}
        self.performance_paths: dict[str, set[str]] = defaultdict(set)
        self.timeline_conflicts: dict[str, list[str]] = {}
        self.unverified_timeline_groups: dict[str, dict] = {}
        self.timeline_scripts: dict[str, str] = {}
        self.mission_perf_sentences: dict[str, set[str]] = defaultdict(set)
        self.baseline_inputs: dict[tuple[str, str], str | None] = {}
        self.preserved_embedding_inputs = 0
        if baseline is not None:
            with open_db(baseline) as previous:
                if previous.execute("SELECT 1 FROM info WHERE key='embedding'").fetchone():
                    for row in previous.execute("SELECT mission_id,text,input_text FROM chunks"):
                        key = (row["mission_id"], row["text"])
                        if self.baseline_inputs.setdefault(key, row["input_text"]) != row["input_text"]:
                            self.baseline_inputs[key] = None

    def resolve(self, value: object) -> str:
        if isinstance(value, dict):
            value = value.get("Hash", "")
        return self.texts.get(str(value), "")

    def load_metadata(self) -> None:
        self.verified_missions = {entry["id"]: entry for entry in load_metadata()}
        chapters = {str(row["ID"]): row for row in read_records(self.root / "ExcelOutput/MissionChapterConfig.json")}
        worlds = {str(row["ID"]): self.resolve(row.get("WorldName")) for row in read_records(self.root / "ExcelOutput/WorldDataConfig.json")}
        for row in read_records(self.root / "ExcelOutput/MainMission.json"):
            mission_id = str(row["MainMissionID"])
            chapter = chapters.get(str(row.get("ChapterID")), {})
            details = {
                "name": self.resolve(row.get("Name")),
                "chapter": self.resolve(chapter.get("ChapterName")),
                "world": worlds.get(str(row.get("WorldID")), "") or self.resolve(chapter.get("StageName")),
                "type": TYPE_NAMES.get(row.get("Type", ""), row.get("Type", "其他任务")),
            }
            for key in ("name", "chapter", "world"):
                details[key] = TAG_PATTERN.sub("", details[key])
            if mission_id in {"2021801", "2021802", "2021803"}:  # 《假面双人舞》
                details["world"] = "匹诺康尼"
            self.add_mission(mission_id, details)
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
        trigger_owners: dict[tuple[str, str], set[str]] = defaultdict(set)
        for mission_script in sorted(self.root.glob(MISSION_TRIGGER_PATTERN)):
            mid = mission_script.parent.name
            if mid not in self.missions:
                continue
            graph = json.loads(mission_script.read_text(encoding="utf-8-sig"))
            for sequence in graph.get("OnStartSequece", []):
                for task in sequence.get("TaskList", []):
                    if str(task.get("$type", "")).endswith("TriggerPerformance") and task.get("PerformanceID"):
                        key = (str(task.get("PerformanceType", "")), str(task["PerformanceID"]))
                        trigger_owners[key].add(mid)
        direct_owners: dict[str, set[str]] = defaultdict(set)
        for performance_type in ("A", "C", "CG", "D", "DS", "E"):
            name = f"Performance{performance_type}.json"
            for row in read_records(self.root / "ExcelOutput" / name):
                pid = str(row.get("PerformanceID", ""))
                path = str(row.get("PerformancePath", ""))
                if len(pid) == 9 and pid.isdigit() and "/Mission/" in path:
                    self.performance_paths[path].add(pid)
                    direct_owners[path].update(trigger_owners.get((performance_type, pid), ()))
        self.performance_owners = {path: next(iter(owners)) for path, owners in direct_owners.items() if len(owners) == 1}

    def add_mission(self, mission_id: str, details: dict | None = None) -> None:
        if mission_id in self.missions:
            return
        item = {"id": mission_id, "name": "", "chapter": "", "world": "", "type": "其他任务"}
        if details:
            item.update(details)
        entry = self.verified_missions.get(mission_id)
        fields = ("name", "chapter", "world", "type")
        if entry and all(not item[key] or normalize_name(item[key]) == normalize_name(entry.get(key, "")) for key in fields):
            for key in fields:
                item[key] = item[key] or entry.get(key, "")
        item["name"] = item["name"] or f"任务 {mission_id}"
        self.missions[mission_id] = item
        self.conn.execute("INSERT INTO missions(id,name,chapter,world,type) VALUES(:id,:name,:chapter,:world,:type)", item)

    def make_line(self, sentence_id: object, mission_id: str, script: str, sequence: int,
                  position: int | float, branch: str, kind: str, trigger: str = "") -> dict | None:
        sentence_id = str(sentence_id)
        sentence = self.sentences.get(sentence_id)
        if not sentence:
            return None
        if isinstance(position, float) and position.is_integer():
            position = int(position)
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
        search_input = f"{context}\n{text}".strip()
        input_text = search_input
        previous_input = self.baseline_inputs.get((first["mission_id"], text))
        if previous_input and previous_input != search_input:
            previous_context, _, previous_body = previous_input.partition("\n")
            if previous_input == text:
                previous_context, previous_body = "", text
            known_context = iter(normalize_text(part) for part in context.split(" · "))
            placeholder = f"任务 {first['mission_id']}"
            previous_parts = [normalize_text(part) for part in previous_context.split(" · ") if part and part != placeholder]
            if previous_body == text and all(any(part == known for known in known_context) for part in previous_parts):
                # Keep the exact previous embedding input if only presentation or missing metadata changed.
                input_text = previous_input
                self.preserved_embedding_inputs += 1
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
        self.conn.execute("INSERT INTO chunks_fts(rowid,terms) VALUES(?,?)", (chunk_id, search_tokens(search_input)))
        self.chunk_count += 1
        self.line_count += len(lines)

    def save_lines(self, lines: list[dict]) -> None:
        buffer: list[dict] = []
        size = 0
        for line in lines:
            if buffer and buffer[-1]["branch"] != line["branch"]:
                self.save_chunk(buffer)
                buffer, size = [], 0
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
                cs = value.get("CustomString")
                text = cs.get("Value") if isinstance(cs, dict) else None
                if text:
                    found.add(str(text))
            for child in value.values():
                found.update(DatasetBuilder.nested_triggers(child))
        elif isinstance(value, list):
            for child in value:
                found.update(DatasetBuilder.nested_triggers(child))
        return found

    @staticmethod
    def inline_transitions(sequences: list[dict]) -> tuple[dict[tuple[int, int], int], set[int]]:
        """Inline a caption when one sequence calls it and waits for its completion."""
        captions = defaultdict(list)
        callers = defaultdict(list)
        for sequence_index, sequence in enumerate(sequences):
            tasks = sequence.get("TaskList", [])
            names = [str(task.get("$type", "")).rsplit(".", 1)[-1] for task in tasks]
            signals = [value.get("Value") if isinstance(value, dict) else None
                       for value in (task.get("CustomString") for task in tasks)]
            if names == ["WaitCustomString", "PerformanceTransition", "TriggerCustomString"]:
                caption = tasks[1]
                if caption.get("TextEnabled", True) and caption.get("TalkSentenceID") and signals[0] and signals[2]:
                    captions[signals[0], signals[2]].append((sequence_index, caption["TalkSentenceID"]))
            for index, (name, following) in enumerate(zip(names, names[1:])):
                if name == "TriggerCustomString" and following == "WaitCustomString":
                    callers[signals[index], signals[index + 1]].append((sequence_index, index))
        placements = {}
        inlined = set()
        for key, entries in captions.items():
            if len(entries) == 1 and len(callers[key]) == 1:
                sequence_index, sid = entries[0]
                placements[callers[key][0]] = sid
                inlined.add(sequence_index)
        return placements, inlined

    def _task_has_valid_lines(self, task: dict) -> bool:
        _, extracted = self._extract_task_items(task)
        return any(sid and str(sid) in self.sentences for sid, _, _ in extracted)

    @staticmethod
    def expand_actionable_tasks(task: dict) -> list[dict]:
        """Flatten container control-flow tasks into ordered actionable tasks."""
        result: list[dict] = []

        def visit(node: object) -> None:
            if isinstance(node, list):
                for item in node:
                    visit(item)
                return
            if not isinstance(node, dict):
                return
            task_type = str(node.get("$type", ""))
            short_type = task_type.rsplit(".", 1)[-1]
            if (short_type in DIALOGUE_TASK_TYPES
                    or task_type.endswith(("PlayTimeline", "PlayVideo"))
                    or bool(node.get("PerformanceID") or node.get("StoryPerformanceID"))):
                result.append(node)
            if short_type == "SwitchCase":
                for case in node.get("TaskList", []):
                    if isinstance(case, dict):
                        visit(case.get("SuccessTaskList", []))
                visit(node.get("DefaultTask", []))
                return
            if child_keys := CONTAINER_TASK_KEYS.get(short_type):
                for key in child_keys:
                    visit(node.get(key))

        visit(task)
        return result

    def expand_task_branches(
        self, task: dict
    ) -> tuple[list[tuple[tuple[tuple[str, int], ...], dict]], list[tuple[tuple[tuple[str, int], ...], str]]]:
        """Expand containers while tagging mutually exclusive condition/random branches."""
        AltPath = tuple[tuple[str, int], ...]

        def visit(node: object) -> tuple[list[tuple[AltPath, dict]], list[tuple[AltPath, str]]]:
            tasks_out: list[tuple[AltPath, dict]] = []
            triggers_out: list[tuple[AltPath, str]] = []
            if isinstance(node, list):
                for item in node:
                    sub_t, sub_g = visit(item)
                    tasks_out.extend(sub_t)
                    triggers_out.extend(sub_g)
                return tasks_out, triggers_out
            if not isinstance(node, dict):
                return tasks_out, triggers_out

            task_type = str(node.get("$type", ""))
            short_type = task_type.rsplit(".", 1)[-1]
            if (short_type in DIALOGUE_TASK_TYPES
                    or task_type.endswith(("PlayTimeline", "PlayVideo"))
                    or bool(node.get("PerformanceID") or node.get("StoryPerformanceID"))):
                tasks_out.append(((), node))
            if task_type.endswith("TriggerCustomString"):
                cs = node.get("CustomString")
                text = cs.get("Value") if isinstance(cs, dict) else None
                if text:
                    triggers_out.append(((), str(text)))

            handled_keys: set[str] = set()
            if short_type in MUTUALLY_EXCLUSIVE_PAIR_KEYS:
                pair_keys = MUTUALLY_EXCLUSIVE_PAIR_KEYS[short_type]
                handled_keys.update(pair_keys)
                branches = [visit(node.get(key)) for key in pair_keys]
                valid_branches = [any(self._task_has_valid_lines(t) for _, t in b_tasks) for b_tasks, _ in branches]
                if sum(valid_branches) >= 2:
                    idx = 0
                    for is_valid, (b_tasks, b_trigs) in zip(valid_branches, branches):
                        if is_valid:
                            idx += 1
                            prefix = (("cond", idx),)
                            tasks_out.extend((prefix + p, t) for p, t in b_tasks)
                            triggers_out.extend((prefix + p, g) for p, g in b_trigs)
                        else:
                            tasks_out.extend(b_tasks)
                            triggers_out.extend(b_trigs)
                else:
                    for b_tasks, b_trigs in branches:
                        tasks_out.extend(b_tasks)
                        triggers_out.extend(b_trigs)
            elif short_type == "SwitchCase":
                handled_keys.update(("TaskList", "DefaultTask"))
                raw_branches: list[tuple[list[tuple[AltPath, dict]], list[tuple[AltPath, str]]]] = []
                extra_case_trigs: list[tuple[AltPath, str]] = []
                for case in node.get("TaskList", []):
                    if isinstance(case, dict):
                        raw_branches.append(visit(case.get("SuccessTaskList", [])))
                        for ck, cv in case.items():
                            if ck != "SuccessTaskList":
                                extra_case_trigs.extend(((), g) for g in sorted(self.nested_triggers(cv)))
                    else:
                        raw_branches.append(visit(case))
                default_task = node.get("DefaultTask", [])
                if default_task:
                    raw_branches.append(visit(default_task))
                valid_branches = [any(self._task_has_valid_lines(t) for _, t in b_tasks) for b_tasks, _ in raw_branches]
                if sum(valid_branches) >= 2:
                    idx = 0
                    for is_valid, (b_tasks, b_trigs) in zip(valid_branches, raw_branches):
                        if is_valid:
                            idx += 1
                            prefix = (("cond", idx),)
                            tasks_out.extend((prefix + p, t) for p, t in b_tasks)
                            triggers_out.extend((prefix + p, g) for p, g in b_trigs)
                        else:
                            tasks_out.extend(b_tasks)
                            triggers_out.extend(b_trigs)
                else:
                    for b_tasks, b_trigs in raw_branches:
                        tasks_out.extend(b_tasks)
                        triggers_out.extend(b_trigs)
                triggers_out.extend(extra_case_trigs)
            elif short_type == "RandomConfig":
                handled_keys.add("TaskList")
                raw_branches = [visit(item) for item in node.get("TaskList", [])]
                valid_branches = [any(self._task_has_valid_lines(t) for _, t in b_tasks) for b_tasks, _ in raw_branches]
                if sum(valid_branches) >= 2:
                    idx = 0
                    for is_valid, (b_tasks, b_trigs) in zip(valid_branches, raw_branches):
                        if is_valid:
                            idx += 1
                            prefix = (("rand", idx),)
                            tasks_out.extend((prefix + p, t) for p, t in b_tasks)
                            triggers_out.extend((prefix + p, g) for p, g in b_trigs)
                        else:
                            tasks_out.extend(b_tasks)
                            triggers_out.extend(b_trigs)
                else:
                    for b_tasks, b_trigs in raw_branches:
                        tasks_out.extend(b_tasks)
                        triggers_out.extend(b_trigs)
            elif child_keys := CONTAINER_TASK_KEYS.get(short_type):
                handled_keys.update(child_keys)
                for key in child_keys:
                    sub_t, sub_g = visit(node.get(key))
                    tasks_out.extend(sub_t)
                    triggers_out.extend(sub_g)

            for key, value in node.items():
                if key not in handled_keys:
                    for g in sorted(self.nested_triggers(value)):
                        triggers_out.append(((), g))
            return tasks_out, triggers_out

        return visit(task)

    @staticmethod
    def _extract_task_items(task: dict) -> tuple[str, list[tuple[object, str, str]]]:
        """Return (short_type, [(sentence_id, kind, trigger), ...]) for one leaf talk task."""
        short_type = str(task.get("$type", "")).rsplit(".", 1)[-1]
        if short_type in TALK_LIST_TASKS:
            items = [(item.get("TalkSentenceID"), "dialogue", "")
                     for item in task.get("SimpleTalkList", []) if isinstance(item, dict)]
            return short_type, items
        if short_type in SINGLE_SENTENCE_TASKS:
            if task.get("TextEnabled", True) and task.get("TalkSentenceID"):
                return short_type, [(task.get("TalkSentenceID"), "dialogue", "")]
            return short_type, []
        if short_type in OPTION_LIST_TASKS:
            items = [(item.get("TalkSentenceID"), "option", str(item.get("TriggerCustomString") or ""))
                     for item in task.get("OptionList", []) if isinstance(item, dict)]
            return short_type, items
        if short_type == "PlayFullScreenTransfer":
            text_info = task.get("TextInfo")
            if task.get("TextEnabled", True) and isinstance(text_info, dict):
                items = [(item.get("TalkSentenceID"), "dialogue", "")
                         for item in text_info.get("TextList", []) if isinstance(item, dict)]
                return short_type, items
            return short_type, []
        if short_type == "PlayNPCBubbleTalk":
            items = [(item.get("TalkSentenceID"), "dialogue", "")
                     for item in task.get("BubbleTalkInfoList", []) if isinstance(item, dict)]
            return short_type, items
        if short_type == "PlayNPCSingleBubbleTalk":
            sid = task.get("TalkSentenceID")
            if isinstance(sid, dict):
                sid = sid.get("FixedValue", {}).get("Value")
            return short_type, [(sid, "dialogue", "")] if sid else []
        if short_type == "PlayOrigamiTraceTalk":
            items = [
                (item.get("TalkSentenceID"), "dialogue", "")
                for entry in task.get("TalkList", [])
                if isinstance(entry, dict) and isinstance(entry.get("TalkInfo"), dict)
                for item in entry["TalkInfo"].get("SimpleTalkList", [])
                if isinstance(item, dict)
            ]
            return short_type, items
        return short_type, []

    def process_file(self, path: Path) -> None:
        script = path.relative_to(self.root).as_posix()
        parts = path.relative_to(self.root).parts
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        sequences = data.get("OnStartSequece", [])
        is_mission_script = path.name.startswith("Mission_") and parts[:3] == ("Config", "Level", "Mission")
        sequence_entries: list[list[tuple[int, int, int, tuple[tuple[str, int], ...], dict]]] = []
        sequence_triggers: list[list[tuple[int, tuple[tuple[str, int], ...], str]]] = []
        has_dialogue = False
        has_options = False
        regular_ids: set[str] = set()
        for sequence in sequences:
            entries: list[tuple[int, int, int, tuple[tuple[str, int], ...], dict]] = []
            seq_triggers: list[tuple[int, tuple[tuple[str, int], ...], str]] = []
            for task_index, top_task in enumerate(sequence.get("TaskList", [])):
                short_top = str(top_task.get("$type", "")).rsplit(".", 1)[-1]
                if short_top in CONTAINER_TASK_KEYS or short_top == "SwitchCase":
                    expanded, branch_triggers = self.expand_task_branches(top_task)
                    for trig_alt_path, trigger in branch_triggers:
                        seq_triggers.append((task_index, trig_alt_path, trigger))
                    if not expanded:
                        continue
                    counts = [max(1, len(self._extract_task_items(subtask)[1])) for _, subtask in expanded]
                    if len(expanded) <= 99 and max(counts) <= 99:
                        for sub_idx, (alt_path, subtask) in enumerate(expanded):
                            base_pos = task_index * 10000 + sub_idx * 100
                            orig_idx = task_index if (sub_idx == 0 and subtask is top_task) else -1
                            entries.append((base_pos, orig_idx, task_index, alt_path, subtask))
                    else:
                        cursor = 0
                        for sub_idx, ((alt_path, subtask), count) in enumerate(zip(expanded, counts)):
                            base_pos = task_index * 10000 + min(cursor, 9999)
                            orig_idx = task_index if (sub_idx == 0 and subtask is top_task) else -1
                            entries.append((base_pos, orig_idx, task_index, alt_path, subtask))
                            cursor = min(cursor + count, 9999)
                else:
                    entries.append((task_index * 10000, task_index, task_index, (), top_task))
                    for trigger in sorted(self.nested_triggers(top_task)):
                        seq_triggers.append((task_index, (), trigger))
            for _, _, _, _, subtask in entries:
                short_sub, extracted = self._extract_task_items(subtask)
                if short_sub in DIALOGUE_TASK_TYPES:
                    has_dialogue = True
                if short_sub in OPTION_LIST_TASKS:
                    has_options = True
                for sid, _, _ in extracted:
                    if sid:
                        regular_ids.add(str(sid))
            sequence_entries.append(entries)
            sequence_triggers.append(seq_triggers)
        if is_mission_script and not has_dialogue:
            return
        battle = parts[:2] == ("Story", "BattlePerformance")
        if battle:
            mission_id = VERIFIED_BATTLE_OWNERS[parts[2]]
        else:
            mission_id = VERIFIED_SCRIPT_OWNERS.get(script, self.performance_owners.get(script, parts[parts.index("Mission") + 1]))
        if is_mission_script and mission_id not in self.missions:
            return
        self.add_mission(mission_id)
        inline_transitions, inlined_sequences = self.inline_transitions(sequences)
        resolved_script_sids = {sid for sid in regular_ids if sid in self.sentences}
        for (_, _), sid in inline_transitions.items():
            if str(sid) in self.sentences:
                resolved_script_sids.add(str(sid))
        for sequence in sequences:
            for task in sequence.get("TaskList", []):
                if str(task.get("$type", "")).endswith("WaitCustomString"):
                    val = str(task.get("CustomString", {}).get("Value", ""))
                    if val.startswith("TalkSentence_") and val.removeprefix("TalkSentence_") in self.sentences:
                        resolved_script_sids.add(val.removeprefix("TalkSentence_"))
                for trigger in self.nested_triggers(task):
                    if m := re.fullmatch(r"TalkSentence_(\d+)", trigger):
                        if m[1] in self.sentences:
                            resolved_script_sids.add(m[1])
        skip_duplicates = (
            is_mission_script
            and not has_options
            and bool(resolved_script_sids)
            and resolved_script_sids.issubset(self.mission_perf_sentences[mission_id])
        )
        references: set[str] = set()
        for sequence_index, sequence in enumerate(sequences):
            tasks = sequence.get("TaskList", [])
            waits = [str(task.get("CustomString", {}).get("Value", "")) for task in tasks
                     if str(task.get("$type", "")).endswith("WaitCustomString")]
            branch = next((value for value in waits if value), "")
            node = branch or f"sequence:{sequence_index}"
            lines: list[dict] = []

            emitted_alt_set: set[str] = set()
            emitted_alt_order: list[str] = []

            def append_line(line: dict | None, alt_branch: str = "") -> bool:
                if line is None:
                    return False
                sid = line["sentence_id"]
                if skip_duplicates and sid in self.mission_perf_sentences[mission_id]:
                    return False
                if not is_mission_script:
                    self.mission_perf_sentences[mission_id].add(sid)
                lines.append(line)
                if alt_branch and alt_branch not in emitted_alt_set:
                    emitted_alt_set.add(alt_branch)
                    emitted_alt_order.append(alt_branch)
                return True

            for source_position, orig_task_index, task_index, alt_path, task in sequence_entries[sequence_index]:
                task_type = str(task.get("$type", ""))
                short_type, extracted = self._extract_task_items(task)
                if alt_path:
                    alt_suffix = "".join(f"#{alt_kind}:{alt_idx}" for alt_kind, alt_idx in alt_path)
                    line_branch = f"{node}#alt:{task_index}{alt_suffix}"
                    alt_label = line_branch
                    edge_source = line_branch
                else:
                    line_branch = branch
                    alt_label = ""
                    edge_source = node
                if orig_task_index >= 0 and task_type.endswith("WaitCustomString") and branch.startswith("TalkSentence_"):
                    sentence_id = branch.removeprefix("TalkSentence_")
                    if sentence_id not in regular_ids:
                        append_line(self.make_line(sentence_id, mission_id, script, sequence_index, source_position, line_branch, "dialogue"), alt_label)
                if extracted and not (short_type in SINGLE_SENTENCE_TASKS and sequence_index in inlined_sequences):
                    for item_index, (sid, kind, trigger) in enumerate(extracted):
                        append_line(self.make_line(sid, mission_id, script, sequence_index, source_position + item_index, line_branch, kind, trigger), alt_label)
                        if kind == "option" and trigger:
                            references.add(trigger)
                            self.conn.execute("INSERT INTO edges VALUES(?,?,?,?)", (script, edge_source, trigger, "option"))
                if orig_task_index >= 0 and (inlined_sid := inline_transitions.get((sequence_index, orig_task_index))):
                    append_line(self.make_line(inlined_sid, mission_id, script, sequence_index, source_position, line_branch, "dialogue"), alt_label)
                if task_type.endswith(("PlayTimeline", "PlayVideo")):
                    self.timeline_scripts[script] = mission_id
                if task_type.endswith("PlayTimeline"):
                    for index, sentence_id in enumerate(VERIFIED_TIMELINE_INSERTS.get(task.get("TimelineName"), ())):
                        if str(sentence_id) in regular_ids:
                            continue
                        append_line(self.make_line(sentence_id, mission_id, script, sequence_index,
                                                   source_position + index, line_branch, "dialogue"), alt_label)
                # Explicit performance IDs are evidence for a script-level recall association.
                for field in ("PerformanceID", "StoryPerformanceID"):
                    if task.get(field):
                        self.referenced_performance.setdefault(str(task[field]), set()).add(mission_id)
            seen_cont_edges: set[tuple[str, str]] = set()
            for alt_branch in emitted_alt_order:
                if (node, alt_branch) not in seen_cont_edges:
                    seen_cont_edges.add((node, alt_branch))
                    self.conn.execute("INSERT INTO edges VALUES(?,?,?,?)", (script, node, alt_branch, "continuation"))
            for trig_task_index, trig_alt_path, trigger in sequence_triggers[sequence_index]:
                references.add(trigger)
                if trig_alt_path:
                    alt_suffix = "".join(f"#{alt_kind}:{alt_idx}" for alt_kind, alt_idx in trig_alt_path)
                    cand_source = f"{node}#alt:{trig_task_index}{alt_suffix}"
                    src = cand_source if cand_source in emitted_alt_set else node
                else:
                    src = node
                if (src, trigger) not in seen_cont_edges:
                    seen_cont_edges.add((src, trigger))
                    self.conn.execute("INSERT INTO edges VALUES(?,?,?,?)", (script, src, trigger, "continuation"))
                if battle:
                    match = re.search(r"(?:^|_)TalkSentence_(\d+)(?:_|$)", trigger)
                    if match and match[1] not in regular_ids:
                        bat_branch = src if trig_alt_path and src in emitted_alt_set else branch
                        append_line(self.make_line(match[1], mission_id, script, sequence_index, trig_task_index * 10000, bat_branch, "dialogue"))
            references.update(waits)
            self.save_lines(lines)
        for index, reference in enumerate(sorted(references)):
            match = re.fullmatch(r"TalkSentence_(\d+)", reference)
            if match and match[1] not in regular_ids:
                if skip_duplicates and match[1] in self.mission_perf_sentences[mission_id]:
                    continue
                exists = self.conn.execute("SELECT 1 FROM lines WHERE script=? AND sentence_id=? LIMIT 1", (script, match[1])).fetchone()
                if not exists:
                    line = self.make_line(match[1], mission_id, script, len(sequences) + index, 0, reference, "reference")
                    if line:
                        if not is_mission_script:
                            self.mission_perf_sentences[mission_id].add(line["sentence_id"])
                        self.save_lines([line])

    def import_timeline_sentences(self) -> None:
        def _accepts_anchored_recovery(mid: str) -> bool:
            m = self.missions.get(mid)
            if not m:
                return False
            if re.fullmatch(r"\d0\d4\d{3}", mid):
                return True
            return mid.startswith(("103", "104", "105", "202", "203", "204", "205")) and m.get("type") in NARRATIVE_TYPES

        # This packed namespace occurs in main story and companion/side story
        # assets. Map, NPC and event IDs cannot be decoded with the same layout.
        namespace_owners: dict[str, set[str]] = defaultdict(set)
        legacy_owners: dict[str, set[str]] = defaultdict(set)
        for mid in self.missions:
            if match := PACKED_STORY_MISSION.fullmatch(mid):
                namespace_owners["".join(match.groups())].add(mid)
            if re.fullmatch(r"103\d{4}", mid):
                legacy_owners[mid[:5]].add(mid)

        prefix_missions: dict[str, set[str]] = defaultdict(set)
        perf_prefix_missions: dict[str, set[str]] = defaultdict(set)
        mission_script_p7_counts: dict[tuple[str, str], int] = defaultdict(int)
        script_prefix_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
        for p7, mid, script, cnt in self.conn.execute(
            "SELECT substr(sentence_id,1,7),mission_id,script,count(DISTINCT sentence_id) FROM lines WHERE length(sentence_id)=9 GROUP BY 1,2,3"
        ):
            prefix_missions[p7].add(mid)
            if script.startswith("Config/Level/Mission/") and script.rsplit("/", 1)[-1].startswith("Mission_"):
                mission_script_p7_counts[p7, mid] += cnt
            elif not script.startswith(("Verified/", "Recovered/")) and "::timeline-" not in script:
                perf_prefix_missions[p7].add(mid)
            if p7.isdigit() and p7 != "1000000":
                script_prefix_counts[mid, script][p7] = cnt

        def dominant_prefix(counts: dict[str, int]) -> str | None:
            if not counts:
                return None
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            if len(ranked) == 1 or ranked[0][1] >= ranked[1][1] * 2:
                return ranked[0][0]
            return None

        # Learn numbering from real references within the same mission. For
        # example, the 2.2 hotel mission uses a different offset than the
        # immediately adjacent Robin mission, despite sharing a prefix.
        rules: dict[tuple[str, str, str, int], set[str]] = defaultdict(set)
        for (mid, script), counts in script_prefix_counts.items():
            name = PERFORMANCE_NAME.fullmatch(Path(script).name)
            prefix = dominant_prefix(counts)
            if name and prefix:
                offset = int(prefix[-2:]) - int(name[1][-2:])
                rules[mid, name[1][:5], prefix[:5], offset].add(prefix)
        patterns: dict[tuple[str, str], list[tuple[str, int, int, int]]] = defaultdict(list)
        for (mid, script_family, family, offset), prefixes in rules.items():
            if mid.startswith("103") and mid[:5] == family and len(prefixes) >= 2:
                patterns[mid, script_family].append((family, offset, 0, 99))
            elif (mid.startswith("103") and script_family == family and _accepts_anchored_recovery(mid)) or (
                not _accepts_anchored_recovery(mid) and self.missions[mid]["type"] in {"支线任务", "活动任务"}
            ):
                first, last = int(min(prefixes)[-2:]), int(max(prefixes)[-2:])
                if len(prefixes) >= 3 and len(prefixes) * 3 >= (last - first + 1) * 2:
                    patterns[mid, script_family].append((family, offset, first, last))
        paths_by_prefix: dict[str, set[tuple[str, str]]] = defaultdict(set)
        calibrated: set[tuple[str, str]] = set()
        for path, mid in self.timeline_scripts.items():
            name = PERFORMANCE_NAME.fullmatch(Path(path).name)
            if not name:
                continue
            counts = script_prefix_counts.get((mid, path), {})
            dom = dominant_prefix(counts)
            for family, offset, first, last in patterns.get((mid, name[1][:5]), ()):
                slot = int(name[1][-2:]) + offset
                if first <= slot <= last:
                    prefix = f"{family}{slot:02}"
                    if not counts or dom == prefix:
                        paths_by_prefix[prefix].add((mid, path))
                        calibrated.add((mid, prefix))
            legacy_prefix = name[1][:5] + name[1][-2:]
            if (legacy_owners.get(legacy_prefix[:5]) == {mid} and self.missions[mid]["type"] in NARRATIVE_TYPES
                    and name[1][:7] == mid and name[1] in self.performance_paths.get(path, ())
                    and (not counts or dom == legacy_prefix)):
                paths_by_prefix[legacy_prefix].add((mid, path))
            # A full performance table entry must carry the same owning task
            # namespace. A same-suffix performance in another quest cannot win.
            if PACKED_STORY_MISSION.fullmatch(mid) or (
                mid.startswith("1040") and self.missions[mid]["type"] in NARRATIVE_TYPES
            ):
                for pid in self.performance_paths.get(path, ()):
                    if pid[:7] == mid:
                        prefix = pid[0] + pid[2] + pid[4:]
                        paths_by_prefix[prefix].add((mid, path))
        # Older chapters use a different numbering scheme. Recover only gaps
        # bounded by multiple explicit sentence references in a timeline script.
        # A reused prefix cannot establish which mission owns the missing text.
        anchored_ranges: dict[str, tuple[str, str, int, int]] = {}
        timeline_anchors: dict[str, list[dict]] = {}
        ambiguous_prefixes: set[str] = set()
        for row in self.conn.execute("""SELECT substr(sentence_id,1,7) prefix,mission_id,script
                FROM lines WHERE length(sentence_id)=9
                GROUP BY prefix,mission_id,script HAVING count(DISTINCT sentence_id)>=2"""):
            p7, mid, script = row
            mission = self.missions.get(mid, {})
            if _accepts_anchored_recovery(mid) or mission.get("type") not in NARRATIVE_TYPES:
                continue
            if not p7.isdigit() or prefix_missions[p7] != {mid} or p7 in ambiguous_prefixes:
                continue
            path = (self.root / script).resolve()
            if not path.is_relative_to(self.root.resolve()) or not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            has_timeline = any(
                str(task.get("$type", "")).endswith("PlayTimeline")
                for sequence in data.get("OnStartSequece", []) for task in sequence.get("TaskList", [])
            )
            if has_timeline:
                if p7 in anchored_ranges:
                    del anchored_ranges[p7]
                    ambiguous_prefixes.add(p7)
                    continue
                anchors = []
                for sequence_index, sequence in enumerate(data.get("OnStartSequece", [])):
                    for task_index, task in enumerate(sequence.get("TaskList", [])):
                        branch = str(task.get("CustomString", {}).get("Value", ""))
                        match = re.fullmatch(r"TalkSentence_(\d{9})", branch)
                        if (str(task.get("$type", "")).endswith("WaitCustomString")
                                and match and match[1].startswith(p7) and match[1] in self.sentences):
                            anchors.append({"sentence_id": int(match[1]), "sequence": sequence_index,
                                            "position": task_index * 10000, "branch": branch})
                anchor_ids = [anchor["sentence_id"] for anchor in anchors]
                if len(anchor_ids) < 2 or len(set(anchor_ids)) != len(anchor_ids):
                    continue
                anchors.sort(key=lambda anchor: anchor["sentence_id"])
                timeline_anchors[p7] = anchors
                anchored_ranges[p7] = (mid, script, anchors[0]["sentence_id"], anchors[-1]["sentence_id"])
        unused_by_p7: dict[str, list[str]] = defaultdict(list)
        for sid in sorted(self.sentences, key=lambda s: int(s) if s.isdigit() else 0):
            if sid not in self.used_sentences and len(sid) == 9 and sid.isdigit():
                p7 = sid[:7]
                if p7 in ambiguous_prefixes:
                    continue
                if p7 in anchored_ranges and not anchored_ranges[p7][2] < int(sid) < anchored_ranges[p7][3]:
                    continue
                if p7 in prefix_missions or p7[:5] in namespace_owners or p7 in paths_by_prefix or p7 in VERIFIED_TIMELINES:
                    unused_by_p7[p7].append(sid)
        occupied_positions: dict[tuple[str, str, int], list[int | float]] = {}

        def reserve(mid: str, script: str, sequence: int, anchor: int | float, count: int, *, before: bool = False) -> list[int | float]:
            key = (mid, script, sequence)
            if key not in occupied_positions:
                occupied_positions[key] = [row[0] for row in self.conn.execute(
                    "SELECT DISTINCT position FROM lines WHERE mission_id=? AND script=? AND sequence=? ORDER BY position", key
                )]
            positions = occupied_positions[key]
            index = bisect_right(positions, anchor)
            if before:
                left, right = (positions[index - 2] if index >= 2 else anchor - count - 1), anchor
            else:
                left, right = anchor, positions[index] if index < len(positions) else anchor + count + 1
            # Keep direct anchors stable and insert recovered rows strictly between existing rows.
            step = min(1, (right - left) / (count + 1))
            new = [left + step * i for i in range(1, count + 1)]
            positions[index - int(before):index - int(before)] = new
            return new

        def fetch_anchors(p7: str, target_mids: set[str]) -> list[sqlite3.Row]:
            placeholders = ",".join("?" for _ in target_mids)
            return self.conn.execute(
                f"""SELECT mission_id, sentence_id, script, sequence, position, branch, kind
                    FROM lines WHERE sentence_id LIKE ? AND mission_id IN ({placeholders})
                    ORDER BY CAST(sentence_id AS INTEGER),
                             CASE WHEN script LIKE 'Config/Level/Mission/%/Mission\\_%.json' ESCAPE '\\' THEN 1 ELSE 0 END,
                             mission_id, sequence, position""",
                (f"{p7}%", *sorted(target_mids)),
            ).fetchall()

        def insert_anchored(existing_rows: list[sqlite3.Row], target_sids: list[str]) -> None:
            first_sid = int(existing_rows[0]["sentence_id"])
            before = existing_rows[0]
            before_sids = [sid for sid in target_sids if int(sid) < first_sid]
            if before_sids:
                positions = reserve(before["mission_id"], before["script"], before["sequence"], before["position"], len(before_sids), before=True)
                self.save_lines([self.make_line(sid, before["mission_id"], before["script"], before["sequence"], pos, before["branch"], "dialogue")
                                 for sid, pos in zip(before_sids, positions)])
            anchors = [int(row["sentence_id"]) for row in existing_rows]
            batches: dict[int, list[str]] = defaultdict(list)
            for sid in target_sids:
                if int(sid) >= first_sid:
                    batches[bisect_right(anchors, int(sid)) - 1].append(sid)
            for index, batch in batches.items():
                anchor = existing_rows[index]
                positions = reserve(anchor["mission_id"], anchor["script"], anchor["sequence"], anchor["position"], len(batch))
                self.save_lines([self.make_line(sid, anchor["mission_id"], anchor["script"], anchor["sequence"], pos, anchor["branch"], "dialogue")
                                 for sid, pos in zip(batch, positions)])

        def is_narrative_story(mission_id: str | None) -> bool:
            return bool(
                mission_id
                and mission_id.startswith(("103", "104", "105", "202", "203", "204", "205"))
                and self.missions.get(mission_id, {}).get("type") in NARRATIVE_TYPES
            )

        for p7 in sorted(unused_by_p7):
            sids = unused_by_p7[p7]
            explicit_missions = prefix_missions.get(p7, set())
            paths = paths_by_prefix.get(p7, set())
            mid, perf_script = VERIFIED_TIMELINES.get(p7, ("", ""))
            if mid and mid not in self.missions:
                continue
            if not mid:
                owners = explicit_missions or {owner for owner, _ in paths} or namespace_owners.get(p7[:5], set())
                if len(owners) > 1 and explicit_missions:
                    # Tier 1: unrelated 1-line Mission_*.json reuse should not override a unique performance owner.
                    perf_owners = perf_prefix_missions.get(p7, set())
                    if len(perf_owners) == 1:
                        perf_mid = next(iter(perf_owners))
                        others = explicit_missions - {perf_mid}
                        perf_info = self.missions.get(perf_mid, {})
                        perf_identity = (perf_info.get("name"), perf_info.get("chapter"))
                        if others and all(
                            mission_script_p7_counts.get((p7, other), 0) <= 1
                            and other[:5] != perf_mid[:5]
                            and (self.missions.get(other, {}).get("name"), self.missions.get(other, {}).get("chapter")) != perf_identity
                            for other in others
                        ):
                            owners = {perf_mid}
                if len(owners) > 1:
                    if explicit_missions:
                        conflict_anchors = fetch_anchors(p7, explicit_missions)
                        identities = {
                            (
                                self.missions.get(m, {}).get("name", ""),
                                self.missions.get(m, {}).get("chapter", ""),
                                self.missions.get(m, {}).get("world", ""),
                                self.missions.get(m, {}).get("type", ""),
                            )
                            for m in explicit_missions
                        }
                        same_quest_split = (
                            conflict_anchors
                            and len(explicit_missions) >= 2
                            and len({m[:5] for m in explicit_missions}) == 1
                            and all(_accepts_anchored_recovery(m) for m in explicit_missions)
                            and len(identities) == 1
                            and bool(next(iter(identities))[0])
                        )
                        if same_quest_split:
                            # Case 2B: sub-missions sharing one quest identity and [:5] prefix recover to nearest anchors.
                            insert_anchored(conflict_anchors, sids)
                            continue
                        # Case 2A: recover only gaps strictly sandwiched between two anchors of the same narrative mission.
                        by_sid: dict[int, list[sqlite3.Row]] = defaultdict(list)
                        for row in conflict_anchors:
                            by_sid[int(row["sentence_id"])].append(row)
                        unique_ids = sorted(by_sid)
                        unique_anchors: list[tuple[str | None, sqlite3.Row]] = []
                        for int_sid in unique_ids:
                            rows_for_sid = by_sid[int_sid]
                            mids_for_sid = {row["mission_id"] for row in rows_for_sid}
                            anchor_mid = next(iter(mids_for_sid)) if len(mids_for_sid) == 1 else None
                            unique_anchors.append((anchor_mid, rows_for_sid[0]))
                        sandwiched_batches: dict[int, list[str]] = defaultdict(list)
                        remaining_sids: list[str] = []
                        for sid in sids:
                            pos = bisect_right(unique_ids, int(sid))
                            if 0 < pos < len(unique_anchors):
                                left_mid, _ = unique_anchors[pos - 1]
                                right_mid, _ = unique_anchors[pos]
                                if left_mid is not None and left_mid == right_mid and is_narrative_story(left_mid):
                                    sandwiched_batches[pos - 1].append(sid)
                                    continue
                            remaining_sids.append(sid)
                        for anchor_idx, batch in sandwiched_batches.items():
                            anchor = unique_anchors[anchor_idx][1]
                            positions = reserve(anchor["mission_id"], anchor["script"], anchor["sequence"], anchor["position"], len(batch))
                            self.save_lines([self.make_line(sid, anchor["mission_id"], anchor["script"], anchor["sequence"], pos, anchor["branch"], "dialogue")
                                             for sid, pos in zip(batch, positions)])
                        if not remaining_sids:
                            continue
                        sids = remaining_sids
                    self.timeline_conflicts[p7] = sorted(owners)
                    self.unverified_timeline_groups[p7] = {"sentences": len(sids), "missions": sorted(owners)}
                    continue
                mid = next(iter(owners))
                assets = {path for owner, path in paths if owner == mid}
                if len(assets) == 1:
                    perf_script = next(iter(assets))
            if not (_accepts_anchored_recovery(mid) or (mid, p7) in calibrated or
                    mid in namespace_owners.get(p7[:5], ()) or p7 in anchored_ranges or p7 in VERIFIED_TIMELINES):
                continue
            if p7 in anchored_ranges:
                script = anchored_ranges[p7][1]
                by_anchor: dict[int, list[str]] = defaultdict(list)
                for sid in sids:
                    anchors = timeline_anchors[p7]
                    index = bisect_right([anchor["sentence_id"] for anchor in anchors], int(sid)) - 1
                    by_anchor[index].append(sid)
                for index, batch in by_anchor.items():
                    anchor = timeline_anchors[p7][index]
                    positions = reserve(mid, script, anchor["sequence"], anchor["position"], len(batch))
                    lines = [self.make_line(sid, mid, script, anchor["sequence"], position, anchor["branch"], "dialogue")
                             for sid, position in zip(batch, positions)]
                    self.save_lines(lines)
                continue
            existing = fetch_anchors(p7, {mid})
            if existing:
                insert_anchored(existing, sids)
            else:
                # A recovered group without any anchors stays separate from other scenes.
                # Keep an evidenced performance's place in the mission without sharing its positions.
                script = f"{perf_script}::timeline-{p7}" if perf_script else f"Recovered/Mission/{mid}/{p7}.json"
                lines = []
                for idx, sid in enumerate(sids):
                    line = self.make_line(sid, mid, script, 0, idx, "", "dialogue")
                    if line:
                        lines.append(line)
                self.save_lines(lines)

    def import_verified_scenes(self) -> None:
        for mission_id, script, sentence_ids in VERIFIED_SCENES:
            ids = [str(identifier) for identifier in sentence_ids]
            if mission_id not in self.missions or not all(identifier in self.sentences for identifier in ids):
                continue
            # Leave explicit associations in newer exports intact, including partial scenes.
            placeholders = ",".join("?" for _ in ids)
            if self.conn.execute(
                f"SELECT 1 FROM lines WHERE sentence_id IN ({placeholders}) LIMIT 1", ids
            ).fetchone():
                continue
            lines = [
                self.make_line(identifier, mission_id, script, 0, position, "", "dialogue")
                for position, identifier in enumerate(ids)
            ]
            self.save_lines(lines)

    def save_recalls(self) -> None:
        for row in read_records(self.root / "ExcelOutput/PerformanceRecallData.json"):
            performance_id = str(row.get("PerformanceID", ""))
            # Unlock conditions alone do not prove that the recalled scene belongs to a mission.
            for mission_id in sorted(self.referenced_performance.get(performance_id, set())):
                self.conn.execute("INSERT OR IGNORE INTO recalls VALUES(?,?,?,?,0)", (
                    f"{row['ID']}:{mission_id}", mission_id, normalize_text(self.resolve(row.get("Name"))), performance_id,
                ))


def source_root(source: Path | str) -> Path:
    """Resolve one raw snapshot, including the wrapper directory in GitHub ZIPs."""
    source = Path(source).resolve()
    def has_textmap(path: Path) -> bool:
        return any((path / "TextMap" / name).is_file() for name in ("TextMapCHS.json", "TextMapMainCHS.json"))
    if not has_textmap(source):
        candidates = [child for child in source.iterdir() if child.is_dir() and has_textmap(child)] if source.is_dir() else []
        if len(candidates) == 1:
            source = candidates[0]
        else:
            raise ValueError("请选择包含 TextMap、ExcelOutput、Story 和 Config 的数据目录")
    for name in ("TalkSentenceConfig.json", "MainMission.json"):
        if not (source / "ExcelOutput" / name).is_file():
            raise ValueError(f"数据缺少 {name}")
    return source


def source_scripts(source: Path) -> list[Path]:
    scripts = {path for pattern in SCRIPT_PATTERNS for path in source.glob(pattern)}
    scripts.update(path for stage in VERIFIED_BATTLE_OWNERS for path in
                   source.glob(f"Story/BattlePerformance/{stage}/BattlePerform*.json"))
    return sorted(path for path in scripts if not path.name.endswith((".layout.json", ".partial.json")))


def import_data(source: Path, output: Path, progress: Callable[[dict], None] | None = None,
                *, baseline: Path | None = None) -> dict:
    source, output = source_root(source), Path(output).resolve()
    if output.exists():
        raise ValueError("输出数据库已存在，请使用新的暂存路径")
    output.parent.mkdir(parents=True, exist_ok=True)

    def report(phase: str, done: int, total: int, message: str) -> None:
        if progress:
            progress({"phase": phase, "done": done, "total": total, "message": message})

    conn = open_db(output, readonly=False)
    try:
        create_schema(conn)
        builder = DatasetBuilder(source, conn, baseline)
        report("metadata", 0, 1, "读取中文文字和任务资料")
        builder.load_metadata()
        conn.commit()
        files = source_scripts(source)
        if not files:
            raise ValueError("数据目录中没有找到任务剧情脚本")
        files = sorted(
            files,
            key=lambda p: (
                p.name.startswith("Mission_") and p.relative_to(source).parts[:3] == ("Config", "Level", "Mission"),
                p.as_posix(),
            ),
        )
        for index, path in enumerate(files, 1):
            builder.process_file(path)
            if index % 200 == 0 or index == len(files):
                conn.commit()
                report("scripts", index, len(files), f"已解析 {index} / {len(files)} 个剧情脚本")
        direct_lines = builder.line_count
        builder.import_verified_scenes()
        verified_scene_lines = builder.line_count - direct_lines
        builder.import_timeline_sentences()
        timeline_recovered_lines = builder.line_count - direct_lines - verified_scene_lines
        if builder.line_count == 0:
            raise ValueError("没有解析出任何剧情台词，请检查文字映射、任务表和脚本格式")
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
            "importer_version": IMPORTER_VERSION,
            "verified_scene_lines": verified_scene_lines,
            "timeline_recovered_lines": timeline_recovered_lines,
            "timeline_conflicts": builder.timeline_conflicts,
            "unverified_timeline_groups": builder.unverified_timeline_groups,
            "preserved_embedding_inputs": builder.preserved_embedding_inputs,
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
    script = row.get("script", "")
    supplemental = script.startswith(("Verified/", "Recovered/", "Story/BattlePerformance/"))
    order = VERIFIED_SCENE_ORDER.get(script, nums[-1] if nums else 0)
    return (supplemental, order, script, row.get("sequence", 0), row.get("position", 0))


def performance_script_id(script: str) -> str:
    match = re.fullmatch(r"(?:Config/Level/Mission/\d+/Act/Act|Story/Mission/\d+/Story)(\d+)\.json", script)
    return match[1] if match else ""


def _collapse_mirrored_scripts(lines: list[dict]) -> tuple[list[dict], dict[str, str]]:
    performances: dict[str, dict[str, list[dict]]] = {}
    for line in lines:
        script = line["script"]
        if performance := performance_script_id(script):
            performances.setdefault(performance, {}).setdefault(script, []).append(line)

    def signature(line: dict) -> tuple:
        return tuple(line[field] for field in ("sentence_id", "speaker", "text", "kind", "trigger"))

    aliases: dict[str, str] = {}
    for scripts in performances.values():
        if len(scripts) != 2:
            continue
        (act_script, act), (story_script, story) = sorted(scripts.items())
        if not (act_script.startswith("Config/") and story_script.startswith("Story/")):
            continue
        positions = {signature(line): index for index, line in enumerate(act)}
        if len(positions) != len(act):
            continue
        matches = [positions.get(signature(line), -1) for line in story]
        # The shorter script must match in order, including all option destinations.
        if matches[0] < 0 or any(left >= right for left, right in zip(matches, matches[1:])):
            continue
        aliases.update({line["id"]: act[index]["id"] for line, index in zip(story, matches)})
    return [line for line in lines if line["id"] not in aliases], aliases


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
        if not script:
            all_lines, aliases = _collapse_mirrored_scripts(all_lines)
            line_id = aliases.get(line_id, line_id)
        total = len(all_lines)
        if line_id:
            for idx, item in enumerate(all_lines):
                if item["id"] == line_id:
                    offset = max(0, idx - limit // 3)
                    break
        lines = all_lines[offset:offset + limit]
        for item in lines:
            if label := VERIFIED_SCENE_LABELS.get(item["script"]):
                item["scene_label"] = label
        scripts = sorted({row["script"] for row in lines})
        edges = []
        if scripts:
            placeholders = ",".join("?" for _ in scripts)
            edges = [dict(row) for row in conn.execute(f"SELECT DISTINCT * FROM edges WHERE script IN ({placeholders})", scripts)]
        return {"mission": dict(mission), "lines": lines, "edges": edges, "anchor_line_id": line_id,
                "recalls": [dict(row) for row in conn.execute("SELECT * FROM recalls WHERE mission_id=?", (str(mission_id),))],
                "scripts": [row[0] for row in conn.execute("SELECT DISTINCT script FROM lines WHERE mission_id=? ORDER BY script", (str(mission_id),))],
                "total": total, "offset": offset, "has_more": offset + len(lines) < total}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python -m backend.data SOURCE OUTPUT")
    def progress_console(event: dict) -> None:
        print(json.dumps(event, ensure_ascii=True), flush=True)
    print(json.dumps(import_data(Path(sys.argv[1]), Path(sys.argv[2]), progress_console), ensure_ascii=True))
