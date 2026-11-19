"""Compute and store official HSR mission workflow DAG ordering across dialogue scripts."""
from __future__ import annotations

import gzip
import json
import re
import sqlite3
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


VALID_PERF_TYPES = frozenset({"A", "C", "CG", "D", "DS", "E"})
LEVEL_GROUP_PATTERN = re.compile(
    r"Config/LevelOutput/(?:SharedRuntimeGroup|RuntimeGroup)/Groups_P\d+_F(\d+)/LevelGroup_P\d+_F\1_G(\d+)\.json$"
)
GROUP_GRAPH_PATTERN = re.compile(r"Config/Level/GroupGraph/F(\d+)/Group_F\1_G(\d+)\.json$")
MISSION_INFO_PATTERN = re.compile(r"Config/Level/Mission/(\d+)/MissionInfo_\1(?:\.partial)?\.json$")
STAGE_BG_TALK_PATTERN = re.compile(r"Config/Level/SubLevelGraph/StageBgTalk_(\d+)\.json$")
BATTLE_PERFORM_PATTERN = re.compile(r"Story/BattlePerformance/(\d+)/BattlePerform[^/]+\.json$")
TALK_SENTENCE_SIGNAL = re.compile(r"TalkSentence_(\d+)")
PERFORMANCE_FILE_PID = re.compile(r"(?:Act|DS|Story)(\d{9})\.json$")
NESTED_GRAPH_KEYS = ("DialoguePath", "LevelGraph", "TaskListTemplatePath", "InitLevelGraph", "JsonPath", "BattlePerformPath")


def ensure_script_order_schema(conn: sqlite3.Connection) -> None:
    """Create the optional cross-script ordering table if missing."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS script_order(
            mission_id TEXT NOT NULL,
            script TEXT NOT NULL,
            rank REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'mission_graph',
            PRIMARY KEY(mission_id, script)
        )"""
    )


def _extract_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, dict):
        fixed = value.get("FixedValue")
        if isinstance(fixed, dict):
            return _extract_int(fixed.get("Value"))
        if "Value" in value:
            return _extract_int(value.get("Value"))
    return None


def _read_records(value: object) -> list[dict]:
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return value
    return []


class _SourceReader:
    """Read raw game JSON configs uniformly from an unpacked directory or ZIP archive."""

    def __init__(self, source: Path | str):
        self.source = Path(source).resolve()
        self._zip: zipfile.ZipFile | None = None
        self._prefix = ""
        self._zip_names: set[str] = set()
        self._dir_root: Path | None = None
        self._cache: dict[str, dict | list | None] = {}

    def __enter__(self) -> _SourceReader:
        if self.source.is_file() and zipfile.is_zipfile(self.source):
            self._zip = zipfile.ZipFile(self.source)
            namelist = self._zip.namelist()
            self._zip_names = set(namelist)
            top_dirs = {name.split("/", 1)[0] for name in namelist if "/" in name}
            if len(top_dirs) == 1:
                candidate = next(iter(top_dirs)) + "/"
                if any(
                    f"{candidate}{marker}/" in self._zip_names or any(n.startswith(f"{candidate}{marker}/") for n in namelist[:200])
                    for marker in ("ExcelOutput", "Config")
                ):
                    self._prefix = candidate
                else:
                    # Fallback check across full namelist when top entries are other folders
                    if any(n.startswith(f"{candidate}ExcelOutput/") or n.startswith(f"{candidate}Config/") for n in namelist):
                        self._prefix = candidate
        elif self.source.is_dir():
            root = self.source
            if not (root / "ExcelOutput").is_dir() and not (root / "Config").is_dir():
                children = [
                    child for child in root.iterdir()
                    if child.is_dir() and ((child / "ExcelOutput").is_dir() or (child / "Config").is_dir())
                ]
                if len(children) == 1:
                    root = children[0]
            self._dir_root = root
        else:
            raise ValueError(f"找不到有效的数据源目录或 ZIP 压缩包：{self.source}")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    @staticmethod
    def _norm(rel: str) -> str:
        return str(rel).lstrip("/").replace("\\", "/")

    def exists(self, rel: str) -> bool:
        norm = self._norm(rel)
        if not norm:
            return False
        if self._zip is not None:
            return (self._prefix + norm) in self._zip_names
        assert self._dir_root is not None
        return (self._dir_root / norm).is_file()

    def read_json(self, rel: str) -> dict | list | None:
        norm = self._norm(rel)
        if not norm:
            return None
        if norm in self._cache:
            return self._cache[norm]
        raw_text: str | None = None
        if self._zip is not None:
            key = self._prefix + norm
            if key in self._zip_names:
                try:
                    raw_text = self._zip.read(key).decode("utf-8-sig")
                except (KeyError, UnicodeDecodeError, zipfile.BadZipFile):
                    raw_text = None
        else:
            assert self._dir_root is not None
            target = (self._dir_root / norm).resolve()
            if target.is_relative_to(self._dir_root.resolve()) and target.is_file():
                try:
                    raw_text = target.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeDecodeError):
                    raw_text = None
        parsed: dict | list | None = None
        if raw_text is not None:
            try:
                value = json.loads(raw_text)
                if isinstance(value, (dict, list)):
                    parsed = value
            except ValueError:
                parsed = None
        self._cache[norm] = parsed
        return parsed

    def iter_config_paths(self) -> Iterator[str]:
        if self._zip is not None:
            plen = len(self._prefix)
            for full in self._zip_names:
                rel = full[plen:] if self._prefix and full.startswith(self._prefix) else full
                if rel.endswith(".json") and (
                    rel.startswith(("Config/Level/", "Config/LevelOutput/", "Story/BattlePerformance/"))
                ):
                    yield rel
            return
        assert self._dir_root is not None
        for sub in (
            "Config/Level/Mission",
            "Config/Level/GroupGraph",
            "Config/Level/SubLevelGraph",
            "Config/LevelOutput",
            "Story/BattlePerformance",
        ):
            base = self._dir_root / sub
            if not base.is_dir():
                continue
            for path in base.rglob("*.json"):
                if path.is_file():
                    yield path.relative_to(self._dir_root).as_posix()


class _MissionOrderBuilder:
    def __init__(self, reader: _SourceReader):
        self.reader = reader
        self.perf_map: dict[tuple[str, str], str] = {}
        self.pid_to_paths: dict[str, set[str]] = defaultdict(set)
        self.raid_to_mids: dict[int, list[str]] = defaultdict(list)
        self.mission_chapters: dict[str, int] = {}
        self.stage_graphs: dict[int, list[str]] = defaultdict(list)
        self.lg_index: dict[tuple[int, int], list[str]] = defaultdict(list)
        self.gg_index: dict[tuple[int, int], str] = {}
        self.mission_infos: dict[str, dict] = {}
        self._load_indexes()

    def _load_indexes(self) -> None:
        for row in _read_records(self.reader.read_json("ExcelOutput/MainMission.json")):
            if not isinstance(row, dict):
                continue
            mid_val = row.get("MainMissionID")
            cid = _extract_int(row.get("ChapterID"))
            if mid_val is not None and cid:
                self.mission_chapters[str(mid_val)] = cid

        for ptype in VALID_PERF_TYPES:
            for row in _read_records(self.reader.read_json(f"ExcelOutput/Performance{ptype}.json")):
                if not isinstance(row, dict):
                    continue
                pid = str(row.get("PerformanceID", ""))
                pp = str(row.get("PerformancePath", ""))
                if pid and pp:
                    self.perf_map[(ptype, pid)] = pp
                    self.pid_to_paths[pid].add(pp)

        for row in _read_records(self.reader.read_json("ExcelOutput/RaidConfig.json")):
            if not isinstance(row, dict):
                continue
            rid = _extract_int(row.get("RaidID"))
            if rid is None:
                continue
            for mid in row.get("MainMissionIDList") or ():
                mid_str = str(mid)
                if mid_str not in self.raid_to_mids[rid]:
                    self.raid_to_mids[rid].append(mid_str)

        mission_info_files: dict[str, list[str]] = defaultdict(list)
        for rel in self.reader.iter_config_paths():
            if match := LEVEL_GROUP_PATTERN.fullmatch(rel):
                self.lg_index[(int(match[1]), int(match[2]))].append(rel)
            elif match := GROUP_GRAPH_PATTERN.fullmatch(rel):
                self.gg_index[(int(match[1]), int(match[2]))] = rel
            elif match := MISSION_INFO_PATTERN.fullmatch(rel):
                mission_info_files[match[1]].append(rel)
            elif match := STAGE_BG_TALK_PATTERN.fullmatch(rel):
                self.stage_graphs[int(match[1])].append(rel)
            elif match := BATTLE_PERFORM_PATTERN.fullmatch(rel):
                self.stage_graphs[int(match[1])].append(rel)

        for rels in self.lg_index.values():
            rels.sort()
        for rels in self.stage_graphs.values():
            rels.sort()

        for mid, rels in mission_info_files.items():
            start_ids: list[int] = []
            finish_ids: list[int] = []
            sm_by_id: dict[int, dict] = {}
            for rel in sorted(rels, key=lambda item: item.endswith(".partial.json")):
                data = self.reader.read_json(rel)
                if not isinstance(data, dict):
                    continue
                for raw in data.get("StartSubMissionList") or ():
                    val = _extract_int(raw)
                    if val is not None and val not in start_ids:
                        start_ids.append(val)
                for raw in data.get("FinishSubMissionList") or ():
                    val = _extract_int(raw)
                    if val is not None and val not in finish_ids:
                        finish_ids.append(val)
                for sm in data.get("SubMissionList") or ():
                    if isinstance(sm, dict):
                        smid = _extract_int(sm.get("ID"))
                        if smid is not None:
                            sm_by_id[smid] = sm
            if not sm_by_id:
                continue
            self.mission_infos[mid] = {"start": start_ids, "finish": finish_ids, "sms": sm_by_id}
            for sm in sm_by_id.values():
                floor = _extract_int(sm.get("LevelFloorID"))
                if floor is not None and floor >= 40000000:
                    raid_id = floor // 1000
                    if mid not in self.raid_to_mids[raid_id]:
                        self.raid_to_mids[raid_id].append(mid)

    @staticmethod
    def _topo_order_submissions(mi: dict) -> tuple[list[int], dict[int, set[int]], dict[int, set[int]]]:
        sms: dict[int, dict] = mi["sms"]
        sm_ids = set(sms.keys())
        start_set = set(mi["start"]) & sm_ids
        preds: dict[int, set[int]] = defaultdict(set)
        succs: dict[int, set[int]] = defaultdict(set)
        for smid, sm in sms.items():
            if sm.get("TakeType") in ("AnySequence", "MultiSequence"):
                for raw in sm.get("TakeParamIntList") or ():
                    pred = _extract_int(raw)
                    if pred is not None and pred in sm_ids and pred != smid:
                        preds[smid].add(pred)
                        succs[pred].add(smid)

        indeg = {smid: len(preds[smid]) for smid in sm_ids}
        depth: dict[int, int] = {}
        queue: deque[int] = deque()
        for smid in sorted(sm_ids):
            if indeg[smid] == 0:
                depth[smid] = 0
                queue.append(smid)
        topo_seq: list[int] = []
        while queue:
            u = queue.popleft()
            topo_seq.append(u)
            for v in sorted(succs[u]):
                depth[v] = max(depth.get(v, 0), depth[u] + 1)
                indeg[v] -= 1
                if indeg[v] == 0:
                    queue.append(v)

        max_depth = max(depth.values(), default=0)
        for smid in sorted(sm_ids):
            if smid not in depth:
                depth[smid] = max_depth + 1
                topo_seq.append(smid)

        must_finished: dict[int, set[int]] = {}
        may_finished: dict[int, set[int]] = {}
        for u in topo_seq:
            ps = preds[u]
            if not ps:
                must_finished[u] = set()
                may_finished[u] = set()
                continue
            may_set: set[int] = set()
            pred_must_sets: list[set[int]] = []
            for p in ps:
                may_set.add(p)
                may_set.update(may_finished.get(p, ()))
                pred_must_sets.append({p} | must_finished.get(p, set()))
            may_finished[u] = may_set
            if sms[u].get("TakeType") == "AnySequence" and len(pred_must_sets) > 1:
                # AnySequence with multiple predecessors only requires one to finish.
                must_finished[u] = set.intersection(*pred_must_sets)
            else:
                must_finished[u] = set.union(*pred_must_sets)

        def sort_key(smid: int) -> tuple[int, int, int]:
            if smid in start_set:
                tier = 0
            elif preds[smid] or succs[smid] or sms[smid].get("TakeType") == "Auto":
                tier = 1
            else:
                tier = 2
            return (depth[smid], tier, smid)

        return sorted(sm_ids, key=sort_key), must_finished, may_finished

    @staticmethod
    def _eval_enable_cond(
        cond_obj: object,
        active_sm: int,
        must_finished: dict[int, set[int]],
        mission_sm_ids: set[int],
    ) -> bool:
        if not isinstance(cond_obj, dict):
            return True
        conds = cond_obj.get("Conditions")
        if not conds:
            return True
        op = str(cond_obj.get("Operation", "And"))
        results: list[bool] = []
        for cond in conds:
            if not isinstance(cond, dict):
                continue
            if cond.get("Type") == "SubMission":
                cid = _extract_int(cond.get("ID"))
                if cid is None or cid not in mission_sm_ids:
                    results.append(True)
                    continue
                phase = str(cond.get("Phase", "Accept"))
                if phase in ("Accept", "Started"):
                    results.append(cid == active_sm)
                elif phase == "Finish":
                    results.append(cid in must_finished[active_sm])
                else:
                    results.append(True)
            else:
                results.append(True)
        if not results:
            return True
        return any(results) if op == "Or" else all(results)

    @classmethod
    def _eval_predicate(
        cls,
        pred: object,
        active_sm: int,
        must_finished: dict[int, set[int]],
        may_finished: dict[int, set[int]],
        mission_sm_ids: set[int],
    ) -> bool | None:
        if not isinstance(pred, dict):
            return None
        ptype = str(pred.get("$type", "")).rsplit(".", 1)[-1]
        if ptype == "ByCompareSubMissionState":
            cid = _extract_int(pred.get("SubMissionID"))
            if cid is None or cid not in mission_sm_ids:
                return None
            state = str(pred.get("SubMissionState", ""))
            inverse = bool(pred.get("Inverse", False))
            if state in ("Started", "Accept"):
                res: bool | None = cid == active_sm
            elif state == "Finish":
                if cid in must_finished[active_sm]:
                    res = True
                elif cid == active_sm or active_sm in may_finished[cid]:
                    res = False
                else:
                    return None
            elif state == "None":
                if cid == active_sm or cid in must_finished[active_sm]:
                    res = False
                elif active_sm in may_finished[cid]:
                    res = True
                else:
                    return None
            else:
                return None
            return (not res) if inverse else res
        if ptype == "ByAnd":
            subs = [
                cls._eval_predicate(sub, active_sm, must_finished, may_finished, mission_sm_ids)
                for sub in (pred.get("PredicateList") or ())
            ]
            if any(item is False for item in subs):
                return False
            if subs and all(item is True for item in subs):
                return True
            return None
        if ptype == "ByAny":
            subs = [
                cls._eval_predicate(sub, active_sm, must_finished, may_finished, mission_sm_ids)
                for sub in (pred.get("PredicateList") or ())
            ]
            if any(item is True for item in subs):
                return True
            if subs and all(item is False for item in subs):
                return False
            return None
        if ptype == "ByNot":
            sub = cls._eval_predicate(pred.get("Predicate"), active_sm, must_finished, may_finished, mission_sm_ids)
            return None if sub is None else (not sub)
        return None

    def _collect_events_from_graph(
        self,
        rel: object,
        mid: str,
        active_sm: int,
        must_finished: dict[int, set[int]],
        may_finished: dict[int, set[int]],
        mission_sm_ids: set[int],
        events: list[tuple[str, str]],
        visited_graphs: set[tuple[str, int]],
    ) -> None:
        if not rel or not isinstance(rel, str):
            return
        norm = _SourceReader._norm(rel)
        visit_key = (norm, active_sm)
        if visit_key in visited_graphs:
            return
        visited_graphs.add(visit_key)
        data = self.reader.read_json(norm)
        if not isinstance(data, dict):
            return

        def walk(node: object) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return
            node_type = str(node.get("$type", "")).rsplit(".", 1)[-1]
            if node_type == "PredicateTaskList":
                verdict = self._eval_predicate(
                    node.get("Predicate"), active_sm, must_finished, may_finished, mission_sm_ids
                )
                if verdict is True:
                    walk(node.get("SuccessTaskList"))
                elif verdict is False:
                    walk(node.get("FailedTaskList"))
                else:
                    walk(node.get("SuccessTaskList"))
                    walk(node.get("FailedTaskList"))
                return
            if node_type == "SwitchCase":
                matched_definite = False
                for case in node.get("TaskList") or ():
                    if not isinstance(case, dict):
                        continue
                    verdict = self._eval_predicate(
                        case.get("Predicate"), active_sm, must_finished, may_finished, mission_sm_ids
                    )
                    if verdict is True:
                        walk(case.get("SuccessTaskList"))
                        matched_definite = True
                        break
                    if verdict is None:
                        walk(case.get("SuccessTaskList"))
                if not matched_definite:
                    walk(node.get("DefaultTask"))
                return

            if node_type == "TriggerPerformance":
                pid_int = _extract_int(node.get("PerformanceID"))
                ptype = str(node.get("PerformanceType", ""))
                # Ignore PlayVideo or other non-dialogue performance types so video IDs never collide with Act IDs.
                if pid_int is not None and (not ptype or ptype in VALID_PERF_TYPES):
                    pid = str(pid_int)
                    pp = self.perf_map.get((ptype, pid))
                    if pp:
                        events.append(("perf_path", pp))
                    else:
                        for candidate in sorted(self.pid_to_paths.get(pid, ())):
                            events.append(("perf_path", candidate))
                    events.append(("perf_id", pid))
            elif node_type in ("PlayMissionTalk", "PlayAndWaitSimpleTalk", "PlaySimpleTalk"):
                for item in node.get("SimpleTalkList") or ():
                    if isinstance(item, dict):
                        sid_int = _extract_int(item.get("TalkSentenceID"))
                        if sid_int:
                            events.append(("sid", str(sid_int)))
                s_start = _extract_int(node.get("StartSentenceID"))
                s_end = _extract_int(node.get("EndSentenceID"))
                if s_start and s_start > 0:
                    if s_end and s_end >= s_start and (s_end - s_start) <= 200:
                        for sid_int in range(s_start, s_end + 1):
                            events.append(("sid", str(sid_int)))
                    else:
                        events.append(("sid", str(s_start)))
            elif node_type == "PlayNPCBubbleTalk":
                for item in node.get("BubbleTalkInfoList") or ():
                    if isinstance(item, dict):
                        sid_int = _extract_int(item.get("TalkSentenceID"))
                        if sid_int:
                            events.append(("sid", str(sid_int)))
            elif node_type == "PlayNPCSingleBubbleTalk" or node_type == "PlayMultiVoiceTalk":
                sid_int = _extract_int(node.get("TalkSentenceID"))
                if sid_int:
                    events.append(("sid", str(sid_int)))
            elif node_type == "PerformanceTransition":
                if node.get("TextEnabled", True):
                    sid_int = _extract_int(node.get("TalkSentenceID"))
                    if sid_int:
                        events.append(("sid", str(sid_int)))
            elif node_type == "PlayVoice_Sequence":
                for vid in node.get("VoiceIDs") or ():
                    vid_int = _extract_int(vid)
                    if vid_int:
                        events.append(("sid", str(vid_int)))
            elif node_type == "PlayVoice":
                vid_int = _extract_int(node.get("VoiceID"))
                if vid_int:
                    events.append(("sid", str(vid_int)))
            elif node_type in ("WaitCustomString", "TriggerCustomString"):
                cs = node.get("CustomString")
                val = cs.get("Value") if isinstance(cs, dict) else None
                if val and isinstance(val, str):
                    if match := TALK_SENTENCE_SIGNAL.search(val):
                        events.append(("sid", match[1]))
                    if node_type == "WaitCustomString":
                        talk_rel = f"Config/Level/Mission/{mid}/Talk/{val}.json"
                        if self.reader.exists(talk_rel):
                            self._collect_events_from_graph(
                                talk_rel, mid, active_sm, must_finished, may_finished, mission_sm_ids, events, visited_graphs
                            )

            for key in NESTED_GRAPH_KEYS:
                nested = node.get(key)
                if isinstance(nested, str) and nested.endswith(".json"):
                    self._collect_events_from_graph(
                        nested, mid, active_sm, must_finished, may_finished, mission_sm_ids, events, visited_graphs
                    )

            for key, value in node.items():
                if key == "Predicate" or key in NESTED_GRAPH_KEYS:
                    continue
                if isinstance(value, (dict, list)):
                    walk(value)

        walk(data.get("OnInitSequece"))
        walk(data.get("OnStartSequece"))

    def _collect_mission_events(self, mid: str, visited_mids: set[str] | None = None) -> list[tuple[str, str]]:
        if visited_mids is None:
            visited_mids = set()
        if mid in visited_mids or mid not in self.mission_infos:
            return []
        visited_mids.add(mid)
        mi = self.mission_infos[mid]
        ordered_sms, must_finished, may_finished = self._topo_order_submissions(mi)
        sm_ids = set(mi["sms"].keys())

        sm_group_pairs: dict[int, list[tuple[int, int]]] = {}
        group_ref_count: dict[tuple[int, int], int] = defaultdict(int)
        for smid in ordered_sms:
            sm = mi["sms"][smid]
            pairs: list[tuple[int, int]] = []
            floor = _extract_int(sm.get("LevelFloorID"))
            if floor is not None:
                for raw_gid in sm.get("GroupIDList") or ():
                    gid = _extract_int(raw_gid)
                    if gid is not None:
                        pair = (floor, gid)
                        if pair not in pairs:
                            pairs.append(pair)
            if not pairs:
                wp_gid = _extract_int(sm.get("WayPointGroupID"))
                wp_floor = _extract_int(sm.get("WayPointFloorID")) or floor
                if wp_floor is not None and wp_gid is not None:
                    pairs.append((wp_floor, wp_gid))
            sm_group_pairs[smid] = pairs
            for pair in pairs:
                group_ref_count[pair] += 1

        events: list[tuple[str, str]] = []
        visited_graphs: set[tuple[str, int]] = set()

        def emit_graph(rel: object, active_sm: int) -> None:
            self._collect_events_from_graph(
                rel, mid, active_sm, must_finished, may_finished, sm_ids, events, visited_graphs
            )

        def walk_pairs(target_pairs: list[tuple[int, int]], active_sm: int) -> None:
            for fl, gid in target_pairs:
                for lg_rel in self.lg_index.get((fl, gid), ()):
                    lg = self.reader.read_json(lg_rel)
                    if not isinstance(lg, dict):
                        continue
                    for gkey in ("LevelGraph", "InitLevelGraph"):
                        emit_graph(lg.get(gkey), active_sm)
                    for coll_key in ("NPCList", "PropList", "AnchorList", "MonsterList"):
                        for obj in lg.get(coll_key) or ():
                            if not isinstance(obj, dict):
                                continue
                            dlg = obj.get("Dialog")
                            if isinstance(dlg, dict) and self._eval_enable_cond(
                                dlg.get("EnableCondition"), active_sm, must_finished, sm_ids
                            ):
                                emit_graph(dlg.get("LevelGraph"), active_sm)
                                for pack in dlg.get("PackList") or ():
                                    if isinstance(pack, dict) and self._eval_enable_cond(
                                        pack.get("EnableCondition"), active_sm, must_finished, sm_ids
                                    ):
                                        emit_graph(pack.get("InitLevelGraph"), active_sm)
                            emit_graph(obj.get("InitLevelGraph"), active_sm)
                emit_graph(self.gg_index.get((fl, gid)), active_sm)

        for smid in ordered_sms:
            sm = mi["sms"][smid]
            pairs = sm_group_pairs[smid]
            unique_pairs = [pair for pair in pairs if group_ref_count[pair] == 1]
            shared_pairs = [pair for pair in pairs if group_ref_count[pair] > 1]

            mj = sm.get("MissionJsonPath")
            mj_waits_talk = False
            if isinstance(mj, str) and mj:
                mj_data = self.reader.read_json(mj)
                if isinstance(mj_data, dict):
                    seqs = [
                        seq for seq in (mj_data.get("OnStartSequece") or ())
                        if isinstance(seq, dict) and seq.get("TaskList")
                    ]
                    if seqs and all(
                        any(
                            isinstance(task, dict) and str(task.get("$type", "")).endswith("WaitCustomString")
                            for task in seq["TaskList"]
                        )
                        for seq in seqs
                    ):
                        mj_waits_talk = True

            if mj and not mj_waits_talk:
                emit_graph(mj, smid)
            if sm.get("FinishType") == "RaidFinishedAndTransfer":
                param_int = _extract_int(sm.get("ParamInt1"))
                if param_int:
                    for rmid in self.raid_to_mids.get(param_int, ()):
                        events.extend(self._collect_mission_events(rmid, visited_mids))
                    if param_int >= 40000000:
                        for rmid in self.raid_to_mids.get(param_int // 1000, ()):
                            events.extend(self._collect_mission_events(rmid, visited_mids))
            walk_pairs(unique_pairs, smid)
            if sm.get("FinishType") in ("StageWin", "BattleWinWithCustomValue"):
                stage_id = _extract_int(sm.get("ParamInt1"))
                if stage_id:
                    for stage_rel in self.stage_graphs.get(stage_id, ()):
                        emit_graph(stage_rel, smid)
            if mj and mj_waits_talk:
                emit_graph(mj, smid)
            walk_pairs(shared_pairs, smid)
        return events

    def _is_sibling(self, mid: str, src_mid: str) -> bool:
        if mid == src_mid:
            return True
        chap = self.mission_chapters.get(mid)
        if chap and chap == self.mission_chapters.get(src_mid):
            return True
        return len(mid) >= 5 and len(src_mid) >= 5 and mid[:5] == src_mid[:5]

    @staticmethod
    def _mid_sort_val(mid: str) -> tuple[int, str]:
        return (int(mid), mid) if mid.isdigit() else (0, mid)

    def compute(self, conn: sqlite3.Connection) -> tuple[dict[str, dict[str, float]], int, int]:
        mission_scripts: dict[str, list[str]] = defaultdict(list)
        script_sids: dict[tuple[str, str], set[str]] = defaultdict(set)
        for mid, script, sid in conn.execute(
            "SELECT mission_id, script, sentence_id FROM lines WHERE mission_id!='unlinked' AND script!='unlinked'"
        ):
            script_sids[(mid, script)].add(sid)
        for mid, script in conn.execute(
            "SELECT DISTINCT mission_id, script FROM lines WHERE mission_id!='unlinked' AND script!='unlinked'"
        ):
            mission_scripts[mid].append(script)

        own_maps: dict[str, tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int]]] = {}
        global_path_hits: dict[str, list[tuple[str, int]]] = defaultdict(list)
        global_pid_hits: dict[str, list[tuple[str, int]]] = defaultdict(list)
        global_sid_hits: dict[str, list[tuple[str, int]]] = defaultdict(list)

        for src_mid in sorted(self.mission_infos):
            events = self._collect_mission_events(src_mid)
            if not events:
                continue
            path_rank: dict[str, int] = {}
            pid_rank: dict[str, int] = {}
            sid_rank: dict[str, int] = {}
            p7_rank: dict[str, int] = {}
            for idx, (kind, val) in enumerate(events, 1):
                if kind == "perf_path":
                    if val not in path_rank:
                        path_rank[val] = idx
                        global_path_hits[val].append((src_mid, idx))
                elif kind == "perf_id":
                    if val not in pid_rank:
                        pid_rank[val] = idx
                        global_pid_hits[val].append((src_mid, idx))
                elif kind == "sid":
                    if val not in sid_rank:
                        sid_rank[val] = idx
                        if not val.startswith("1000000"):
                            global_sid_hits[val].append((src_mid, idx))
                    if len(val) == 9 and val.isdigit() and not val.startswith("1000000"):
                        p7_rank.setdefault(val[:7], idx)
            own_maps[src_mid] = (path_rank, pid_rank, sid_rank, p7_rank)

        total_missions = len(mission_scripts)
        total_scripts = sum(len(items) for items in mission_scripts.values())
        orders: dict[str, dict[str, float]] = {}

        for mid, scripts in mission_scripts.items():
            raw_ranks: dict[str, tuple[int, tuple[int, str], int]] = {}
            own = own_maps.get(mid)
            mid_key = self._mid_sort_val(mid)

            for sc in scripts:
                base_sc = sc.split("::timeline-")[0]
                file_pid_match = PERFORMANCE_FILE_PID.search(base_sc)
                file_pid = file_pid_match[1] if file_pid_match else ""
                sids = script_sids[(mid, sc)]

                own_rank: int | None = None
                if own is not None:
                    path_rank, pid_rank, sid_rank, p7_rank = own
                    own_rank = path_rank.get(base_sc)
                    if own_rank is None and file_pid:
                        own_rank = pid_rank.get(file_pid)
                    if own_rank is None:
                        hits = [sid_rank[s] for s in sids if s in sid_rank]
                        if hits:
                            own_rank = min(hits)
                    if own_rank is None:
                        p7_hits = [p7_rank[s[:7]] for s in sids if len(s) == 9 and s.isdigit() and s[:7] in p7_rank]
                        if p7_hits:
                            own_rank = min(p7_hits)

                if own_rank is not None:
                    raw_ranks[sc] = (0, (0, ""), own_rank)
                    continue

                sibling_cands = [item for item in global_path_hits.get(base_sc, ()) if self._is_sibling(mid, item[0])]
                if not sibling_cands and file_pid:
                    sibling_cands = [item for item in global_pid_hits.get(file_pid, ()) if self._is_sibling(mid, item[0])]
                if not sibling_cands:
                    for s in sids:
                        if s in global_sid_hits:
                            sibling_cands.extend(item for item in global_sid_hits[s] if self._is_sibling(mid, item[0]))
                if sibling_cands:
                    best_src, best_rank = min(sibling_cands, key=lambda item: (self._mid_sort_val(item[0]), item[1]))
                    tier = -1 if self._mid_sort_val(best_src) < mid_key else 1
                    raw_ranks[sc] = (tier, self._mid_sort_val(best_src), best_rank)

            if raw_ranks:
                ordered = sorted(raw_ranks.items(), key=lambda item: (item[1], item[0]))
                orders[mid] = {sc: float(index) for index, (sc, _) in enumerate(ordered, 1)}
        return orders, total_missions, total_scripts


def compute_mission_script_orders(
    source: Path | str, conn: sqlite3.Connection
) -> tuple[dict[str, dict[str, float]], int, int]:
    """Compute per-mission script order ranks from raw source dir or archive ZIP."""
    with _SourceReader(source) as reader:
        return _MissionOrderBuilder(reader).compute(conn)


def _write_script_order_rows(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, float, str]],
    stats: dict,
) -> dict:
    ensure_script_order_schema(conn)
    # Callers may pass a DatasetConnection whose __exit__ closes the connection; commit explicitly instead of `with conn:`.
    try:
        conn.execute("DELETE FROM script_order")
        conn.executemany(
            "INSERT INTO script_order(mission_id, script, rank, source) VALUES(?,?,?,?)",
            rows,
        )
        conn.execute(
            "INSERT OR REPLACE INTO info(key,value) VALUES('mission_script_order_stats',?)",
            (json.dumps(stats, ensure_ascii=False),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats


def apply_mission_script_orders(source: Path | str, conn: sqlite3.Connection) -> dict:
    """Compute and persist per-mission script ranks in `script_order` without altering lines/vectors."""
    orders, total_missions, total_scripts = compute_mission_script_orders(source, conn)
    rows = [
        (mid, script, rank, "mission_graph")
        for mid in sorted(orders)
        for script, rank in sorted(orders[mid].items(), key=lambda item: (item[1], item[0]))
    ]
    stats = {
        "missions": len(orders),
        "scripts": len(rows),
        "total_missions": total_missions,
        "total_scripts": total_scripts,
        "source": "mission_graph",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return _write_script_order_rows(conn, rows, stats)


def export_script_orders(conn: sqlite3.Connection, output: Path | str) -> dict:
    """Export stored `script_order` rows as compact JSON or `.json.gz`."""
    ensure_script_order_schema(conn)
    rows = [
        [row[0], row[1], float(row[2]), row[3]]
        for row in conn.execute("SELECT mission_id, script, rank, source FROM script_order ORDER BY mission_id, rank, script")
    ]
    info_row = conn.execute("SELECT value FROM info WHERE key='mission_script_order_stats'").fetchone()
    stats = json.loads(info_row[0]) if info_row else {
        "missions": len({r[0] for r in rows}),
        "scripts": len(rows),
        "source": "mission_graph",
    }
    payload = json.dumps({"stats": stats, "rows": rows}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    target = Path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix == ".gz":
        with gzip.open(target, "wb") as stream:
            stream.write(payload)
    else:
        target.write_bytes(payload)
    return {"output": str(target), "bytes": target.stat().st_size, "rows": len(rows), "stats": stats}


def import_script_orders(conn: sqlite3.Connection, payload_path: Path | str) -> dict:
    """Import an exported `.json` or `.json.gz` `script_order` payload into `conn`."""
    source = Path(payload_path).resolve()
    if not source.is_file():
        raise ValueError(f"找不到脚本顺序数据文件：{source}")
    if source.suffix == ".gz":
        with gzip.open(source, "rb") as stream:
            data = json.loads(stream.read().decode("utf-8"))
    else:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError("脚本顺序数据格式无效")
    rows = [
        (str(item[0]), str(item[1]), float(item[2]), str(item[3]) if len(item) > 3 else "mission_graph")
        for item in data["rows"]
    ]
    base_stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    total_missions = conn.execute(
        "SELECT count(DISTINCT mission_id) FROM lines WHERE mission_id!='unlinked' AND script!='unlinked'"
    ).fetchone()[0]
    total_scripts = conn.execute(
        "SELECT count(DISTINCT mission_id || ':' || script) FROM lines WHERE mission_id!='unlinked' AND script!='unlinked'"
    ).fetchone()[0]
    stats = {
        **base_stats,
        "missions": len({row[0] for row in rows}),
        "scripts": len(rows),
        "total_missions": total_missions,
        "total_scripts": total_scripts,
        "source": base_stats.get("source", "mission_graph"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return _write_script_order_rows(conn, rows, stats)
