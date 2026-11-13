"""Refresh attributed release metadata from exact BWIKI task infoboxes.

Run with --database PATH. --offline uses cached infobox fields only.
The database is opened read-only. The resulting JSON is bundled with the app.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.version_metadata import DEFAULT_METADATA, normalize_name

CACHE = ROOT / "data" / "version-research"
TYPE_ALIASES = {"Gap": "开拓续闻", "支线任务": "冒险任务"}


def compatible_type(game_type: str, wiki_type: str) -> bool:
    # The game's Branch enum includes adventure and event quest lines.
    if game_type == "支线任务":
        return wiki_type in {"冒险任务", "活动任务", "玩法任务"}
    return TYPE_ALIASES.get(game_type, game_type) == wiki_type


class TableFields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.cells = []
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells = []
        if tag in ("td", "th"):
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.cells.append("".join(self.cell).strip())
            self.cell = None
        if tag == "tr" and len(self.cells) >= 2:
            self.rows.append(self.cells[:2])


def parse_infobox(html: str, title: str) -> dict | None:
    parser = TableFields()
    parser.feed(html)
    fields = {}
    for label, value in parser.rows:
        label = label.strip().rstrip("：:")
        if label in ("任务名称", "任务类型", "所属版本") and label not in fields:
            fields[label] = value.strip()
    if normalize_name(fields.get("任务名称", "")) != normalize_name(title):
        return None
    version = fields.get("所属版本", "")
    if not re.fullmatch(r"\d+\.\d+", version) or not fields.get("任务类型"):
        return None
    return {"title": fields["任务名称"], "version": version, "type": fields["任务类型"]}


def fetch_page(title: str, cache: Path, offline: bool, proxy: str | None) -> dict | None:
    cache_file = cache / (hashlib.sha256(title.encode()).hexdigest() + ".json")
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if normalize_name(cached["title"]) == normalize_name(title):
                return cached
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if offline:
        return None
    url = "https://wiki.biligame.com/sr/" + quote(title, safe="")
    with httpx.Client(timeout=20, follow_redirects=True, proxy=proxy,
                      headers={"User-Agent": "HSR-Story-Search/0.1 (mission release metadata)"}) as client:
        for attempt in range(3):
            try:
                response = client.get(url)
                response.raise_for_status()
                fields = parse_infobox(response.text, title)
                if fields is None:
                    return None
                fields.update(source=url, retrieved_at=datetime.now(timezone.utc).date().isoformat(),
                              method="bwiki-task-infobox", status="verified")
                temporary = cache_file.with_suffix(".tmp")
                temporary.write_text(json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(cache_file)
                return fields
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    return None
                time.sleep(attempt + 1)
    return None


def build_mappings(missions: list[dict], pages: list[dict]) -> tuple[list[dict], dict]:
    groups = defaultdict(list)
    for mission in missions:
        if mission["name"] and not mission["name"].startswith("任务 "):
            groups[normalize_name(mission["name"])].append(mission)
    entries = []
    rejected = []
    for page in pages:
        matches = [m for m in groups.get(normalize_name(page["title"]), [])
                   if compatible_type(m["type"], page["type"])]
        identities = {(m["chapter"], m["world"], m["type"]) for m in matches}
        if len(identities) > 1:
            rejected.append(page["title"])
            continue
        for mission in matches:
            entries.append({**mission, **{key: page[key] for key in
                ("version", "source", "retrieved_at", "method", "status")}, "wiki_type": page["type"]})
    by_id = {entry["id"]: entry for entry in entries}
    return sorted(by_id.values(), key=lambda e: e["id"]), {
        "named_missions": sum(len(g) for g in groups.values()),
        "verified_missions": len(by_id),
        "verified_by_type": dict(Counter(e["type"] for e in by_id.values())),
        "ambiguous_names": sorted(set(rejected)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--output", type=Path, default=DEFAULT_METADATA)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--proxy", help="Optional HTTP proxy URL")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        missions = [dict(row) for row in connection.execute("SELECT id,name,chapter,world,type FROM missions")]
    titles = sorted({m["name"] for m in missions if m["name"] and not m["name"].startswith("任务 ")})
    if args.limit is not None:
        titles = titles[:args.limit]
    pages = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_page, title, args.cache, args.offline, args.proxy) for title in titles]
        for index, future in enumerate(as_completed(futures), 1):
            page = future.result()
            if page:
                pages.append(page)
            if index % 20 == 0 or index == len(titles):
                print(f"Checked {index}/{len(titles)} task names, found {len(pages)} explicit versions", flush=True)
    mappings, coverage = build_mappings(missions, pages)
    # A partial/offline refresh retains previously verified records for matching identities.
    if args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8")).get("missions", [])
        current = {m["id"]: m for m in mappings}
        identities = {m["id"]: m for m in missions}
        for entry in previous:
            identity = identities.get(entry["id"])
            if identity and all(identity[k] == entry[k] for k in ("name", "chapter", "world", "type")):
                current.setdefault(entry["id"], entry)
        mappings = sorted(current.values(), key=lambda e: e["id"])
    coverage["verified_missions"] = len(mappings)
    coverage["verified_by_type"] = dict(Counter(e["type"] for e in mappings))
    document = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                "description": "Exact task infobox release versions. Unknown missions remain unclassified.",
                "attribution": "BWIKI contributors, CC BY-NC-SA 4.0. Each record links its source task page.",
                "coverage": coverage, "missions": mappings}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps(coverage, ensure_ascii=False))


if __name__ == "__main__":
    main()
