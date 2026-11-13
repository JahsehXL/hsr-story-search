"""Inspect, rebuild, compare and repair isolated story data candidates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.data_maintenance import (
    audit_database, build_candidate, compare_databases, fetch_upstream, inspect_source, repair_metadata, package_candidate,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write a new JSON report (existing files are preserved)")
    commands = parser.add_subparsers(dest="command", required=True)
    upstream = commands.add_parser("upstream", help="Read GitHub commit title and production game version")
    upstream.add_argument("--repository", default="DimbreathBot/TurnBasedGameData")
    upstream.add_argument("--commit", default="HEAD")
    source = commands.add_parser("inspect-source", help="Validate expanded source files and fingerprint inputs")
    source.add_argument("--source", type=Path, required=True)
    build = commands.add_parser("build", help="Rebuild a new offline candidate using the current importer")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--provenance", type=Path, required=True)
    build.add_argument("--baseline", type=Path)
    audit = commands.add_parser("audit", help="Read-only quality and vector coverage checks")
    audit.add_argument("--database", type=Path, required=True)
    compare = commands.add_parser("compare", help="Read-only old/new coverage and content comparison")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    repair = commands.add_parser("repair-metadata", help="Fill evidence-backed task versions in a new copy")
    repair.add_argument("--database", type=Path, required=True)
    repair.add_argument("--output", type=Path, required=True)
    repair.add_argument("--metadata", type=Path)
    package = commands.add_parser("package", help="Export an audited candidate ZIP for the management review workflow")
    package.add_argument("--database", type=Path, required=True)
    package.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.report and args.report.exists():
        parser.error("Report already exists; choose a new path")
    try:
        if args.command == "upstream":
            result = fetch_upstream(args.repository, args.commit)
        elif args.command == "inspect-source":
            result = inspect_source(args.source)
        elif args.command == "build":
            provenance = json.loads(args.provenance.read_text(encoding="utf-8-sig"))
            result = build_candidate(args.source, args.output, provenance, args.baseline,
                                     lambda event: print(json.dumps(event, ensure_ascii=False), file=sys.stderr))
        elif args.command == "audit":
            result = audit_database(args.database)
        elif args.command == "compare":
            result = compare_databases(args.baseline, args.candidate)
        elif args.command == "package":
            result = package_candidate(args.database, args.output)
        else:
            result = repair_metadata(args.database, args.output, args.metadata)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"{exc}\n")
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    print(rendered)
    audits = [result.get("audit"), result.get("baseline"), result.get("candidate")]
    if result.get("ok") is False or any(isinstance(item, dict) and item.get("ok") is False for item in audits):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
