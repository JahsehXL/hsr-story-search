"""Compare the JavaScript runtime with the web snapshot and search rules."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.data import get_mission, open_db, performance_script_id  # noqa: E402
from backend.search import lexical_search  # noqa: E402

QUERIES = [
    ("生命因何而沉睡", {}), ("生命 因何而 沉睡", {}), ("生命，因何而沉睡？", {}),
    ("老杨", {}), ("三月七 星穹列车", {}), ("流萤", {}), ("4.7", {}),
    ("二相乐园", {}), ("丹恒 景元 幽囚狱", {}), ("卡芙卡唤醒开拓者", {}),
    ("艾利欧", {}), ("开拓者", {}), ("！", {}), ("嗯", {}), ("zzznotfound", {}),
    (",%_@", {}), ("A", {}), ("NPC", {}), ("星核猎手", {}),
    ("", {"version": "4.7"}), ("", {"version": "unknown"}), ("", {"character": "老杨"}),
    ("流萤", {"world": "匹诺康尼"}), ("你", {"character": "三月七"}),
    ("", {"type": "开拓任务"}), ("我", {"version": "4.7", "character": "开拓者"}),
    ("这个", {"world": "雅利洛-VI"}), ("什么", {"type": "同行任务"}),
    ("constructor", {}), ("toString", {}), ("", {"character": "constructor"}),
    ("卢卡,别喝不该喝的「茶」哦!", {}), ("波提欧 你可不许欺负他", {}),
    ("Ｂ１２", {}), ("4_7", {}), ("　她...说？　", {}),
    ("恐怖片 幕后黑手", {}), ("全程没有一只小奇美拉", {}), ("票已经卖光了", {}),
    ("还是先去确认一下时刻表吧", {}), ("我还以为你们老早就启程去摘星星了呢", {}), ("哟,又见面了,开拓者!", {}),
]


def expected(path, query, filters):
    raw = lexical_search(path, query, filters, limit=500)
    seen = set()
    copies = {}
    rows = []
    for rank, item in enumerate(raw, 1):
        script = item["script"]
        performance = performance_script_id(script)
        if performance and item["sentence_id"]:
            key = (item["mission_id"], performance, item["sentence_id"])
            if copies.setdefault(key, script) != script:
                continue
        if len(raw) > 5:
            key = (item["mission_id"], item["speaker"], item["text"])
            if key in seen:
                continue
            seen.add(key)
        rows.append([*[item[k] for k in ("chunk_id", "line_id", "text", "match_type", "matched_field")],
                     round(1 / (60 + rank), 6), item["mission_id"], item["mission_name"], item["version"],
                     item.get("dialogue_origin")])
    return {"rows": rows, "truncated": len(raw) >= 500}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--previous", help="Verify old favorites against this previous snapshot")
    parser.add_argument("--query")
    args = parser.parse_args()
    snapshot = args.snapshot or json.loads((ROOT / "data/current.json").read_text(encoding="utf-8"))["snapshot"]
    database = ROOT / "data/snapshots" / snapshot / "story.db"
    cases = [(args.query, {})] if args.query is not None else QUERIES
    payload = {"queries": [{"query": query, "filters": filters, **expected(database, query, filters)} for query, filters in cases]}
    mission_examples = {}
    with open_db(database) as conn:
        for test in payload["queries"]:
            for _, line_id, *_ in test["rows"][:1]:
                row = conn.execute("SELECT mission_id FROM lines WHERE id=?", (line_id,)).fetchone()
                mission_examples.setdefault(row[0], line_id)
        option = conn.execute(
            "SELECT mission_id,id FROM lines WHERE kind='option' AND trigger!='' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if option:
            mission_examples.setdefault(option[0], option[1])
        long_mission = conn.execute(
            "SELECT mission_id FROM lines GROUP BY mission_id ORDER BY count(*) DESC,mission_id LIMIT 1"
        ).fetchone()[0]
        payload["bookmarks"] = []
        if args.previous:
            previous = ROOT / "data/snapshots" / args.previous / "story.db"
            conn.execute("ATTACH DATABASE ? AS previous", (previous.resolve().as_uri() + "?mode=ro",))
            payload["bookmarks"] = [dict(row) for row in conn.execute("""
                SELECT old.mission_id AS old_mission, current.mission_id AS new_mission,
                       current.script, min(current.id) AS line
                FROM main.lines AS current JOIN previous.lines AS old USING(id)
                WHERE current.mission_id != old.mission_id
                GROUP BY current.script, old.mission_id, current.mission_id
                ORDER BY current.script, old.mission_id, current.mission_id
            """)]
    late_page = get_mission(database, long_mission, offset=1000, limit=1000)
    if late_page["lines"]:
        mission_examples[long_mission] = late_page["lines"][-1]["id"]
    payload["missions"] = []
    for mission_id, line_id in mission_examples.items():
        for offset, limit, anchor in ((0, 40, line_id), (30, 10, None)):
            detail = get_mission(database, mission_id, anchor, offset=offset, limit=limit)
            payload["missions"].append({
                "id": mission_id, "anchor": anchor, "offset": offset, "limit": limit,
                "expected": {k: detail[k] for k in ("mission", "lines", "edges", "recalls", "ordered_scripts",
                             "optional_scripts", "anchor_line_id", "anchor_missing", "offset", "has_more", "total")},
            })
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("verify.mjs")), str(args.corpus.resolve())],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
