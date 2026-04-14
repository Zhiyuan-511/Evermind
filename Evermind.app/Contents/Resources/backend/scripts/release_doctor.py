#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from preview_validation import diagnostics_snapshot
from release_doctor import format_release_doctor_report
from runtime_paths import resolve_state_dir


def _write_report(payload: dict, report_name: str) -> Path:
    state_dir = resolve_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / report_name
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Evermind release/runtime doctor")
    parser.add_argument("--json", action="store_true", help="Print the release doctor report as JSON")
    parser.add_argument("--full-json", action="store_true", help="Print the full diagnostics snapshot as JSON")
    parser.add_argument("--write-report", action="store_true", help="Write the report JSON into ~/.evermind")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when the release doctor reports failures")
    args = parser.parse_args()

    snapshot = await diagnostics_snapshot()
    report = snapshot.get("release", {}) if isinstance(snapshot, dict) else {}

    if args.write_report:
        target = _write_report(snapshot if args.full_json else report, "release_doctor_report.json")
        print(f"wrote {target}")

    if args.full_json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    elif args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_release_doctor_report(report))

    if args.strict and str(report.get("status") or "").lower() == "fail":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
