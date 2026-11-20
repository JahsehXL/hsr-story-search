"""Build and validate the offline ZIP for Xiaohongshu Creator Platform."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

from export_corpus import ROOT, export


HERE = Path(__file__).resolve().parent
MAX_ZIP = 10 * 1024 * 1024
ASSET_EXTENSIONS = {".html", ".css", ".js", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp",
                    ".svg", ".woff", ".woff2"}
LOCAL_PATH = re.compile(r"^\./[a-zA-Z0-9_./-]+$")
BANNED_JS = re.compile(r"\b(?:eval|Function|importScripts)\s*\(|\bWebAssembly\b|\b(?:fetch|XMLHttpRequest|open)\s*\(\s*['\"]https?://")


class Entry(HTMLParser):
    def __init__(self, names: set[str]):
        super().__init__()
        self.names = names
        self.scripts = []
        self.inside_script = False

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag in {"iframe", "object", "base", "embed"} or any(k.startswith("on") for k in attrs):
            raise ValueError(f"Unsupported HTML element or handler: {tag}")
        for name in ("src", "href"):
            path = attrs.get(name)
            if path is not None and (not LOCAL_PATH.fullmatch(path) or path[2:] not in self.names):
                raise ValueError(f"Missing or nonlocal {name} in {tag}: {path}")
        if tag == "script":
            if "src" not in attrs or attrs.get("type") not in (None, "text/javascript"):
                raise ValueError("Only package-local classic scripts may appear in index.html")
            self.inside_script = True
            self.scripts.append(attrs["src"])

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside_script = False

    def handle_data(self, data):
        if self.inside_script and data.strip():
            raise ValueError("Inline JavaScript is forbidden")


def web_files(web: Path, corpus: Path, shards: int, index_files: list[str]):
    for name in ("index.html", "app.js", "app.css", "fonts/Archivo-latin.woff2"):
        yield name, web / name
    for name in ["catalog.js", *index_files]:
        yield "corpus/" + name, corpus / name
    for index in range(shards):
        name = f"corpus-{index:03}.js"
        yield "corpus/" + name, corpus / name


def pack(files, output: Path) -> int:
    entries = dict(files)
    for name, source in entries.items():
        if PurePosixPath(name).suffix not in ASSET_EXTENSIONS or not LOCAL_PATH.fullmatch("./" + name):
            raise ValueError(f"Unsupported archive member: {name}")
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Missing or unsafe build file: {source}")
    entry = Entry(set(entries))
    entry.feed(entries["index.html"].read_text(encoding="utf-8"))
    if entry.scripts != ["./app.js"]:
        raise ValueError("Expected one external classic app.js")
    bundle = entries["app.js"].read_text(encoding="utf-8")
    if forbidden := BANNED_JS.search(bundle):
        raise ValueError(f"Unsupported operation in app bundle: {forbidden.group()}")
    css = entries["app.css"].read_text(encoding="utf-8")
    for path in re.findall(r"url\(['\"]?([^)'\"\s]+)", css):
        if not LOCAL_PATH.fullmatch(path) or path[2:] not in entries:
            raise ValueError(f"Missing or nonlocal CSS resource: {path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=False) as archive:
            for name, source in sorted(entries.items()):
                info = zipfile.ZipInfo(name, (2000, 1, 1, 0, 0, 0))
                info.external_attr = 0o100644 << 16
                archive.writestr(info, source.read_bytes(), zipfile.ZIP_DEFLATED, compresslevel=9)
        size = temporary.stat().st_size
        if size > MAX_ZIP:
            raise ValueError(f"ZIP is {size:,} bytes, exceeding the {MAX_ZIP:,}-byte upload limit")
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() or set(archive.namelist()) != set(entries):
                raise ValueError("ZIP integrity check failed")
        temporary.replace(output)
        return size
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot")
    parser.add_argument("--output", type=Path, default=HERE / "releases/xinggui-dangan-xiaohongshu.zip")
    args = parser.parse_args()
    snapshot = args.snapshot or json.loads((ROOT / "data/current.json").read_text(encoding="utf-8"))["snapshot"]
    if not re.fullmatch(r"[a-z0-9_-]+", snapshot):
        parser.error("invalid snapshot ID")
    database = ROOT / "data/snapshots" / snapshot / "story.db"
    corpus, web = HERE / "build/corpus", HERE / "build/web"
    stats = export(database, corpus, snapshot)
    print(json.dumps(stats, ensure_ascii=False), flush=True)
    subprocess.run(["node", str(HERE / "build.mjs"), str(web)], check=True)
    size = pack(web_files(web, corpus, stats["shards"], stats["index_files"]), args.output)
    audit = HERE / ".codex/minitool-zip-builder/scripts/audit_artifact.py"
    if audit.is_file():
        subprocess.run([sys.executable, str(audit), str(args.output)], check=True)
    print(f"{size:,} bytes | {args.output.resolve()}")


if __name__ == "__main__":
    main()
