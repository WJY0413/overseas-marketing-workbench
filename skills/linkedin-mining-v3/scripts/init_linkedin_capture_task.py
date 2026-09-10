#!/usr/bin/env python3
"""Create a non-destructive task tree for a native LinkedIn people-search capture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--source-target", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--network-scope", default="S")
    args = parser.parse_args()

    task_dir = args.task_dir
    if task_dir.exists():
        raise SystemExit(f"task directory already exists: {task_dir}")
    task_dir.mkdir(parents=True)
    (task_dir / "raw_pages").mkdir()
    scope = [part.strip() for part in args.network_scope.split(",") if part.strip()]
    write_json(task_dir / "task_manifest.json", {
        "task_name": task_dir.name,
        "source_target": args.source_target,
        "source_url": args.source_url,
        "network_scope": scope,
        "capture_scope": "LinkedIn people-search result cards only; preserve raw person data and direct same-card /in/ profile URLs.",
        "exclusions": [
            "No external search or constructed profile URLs",
            "No profile opening or contact action",
            "No company/title parsing, scoring, or outreach",
            "No write to linkedin_people_master.sqlite"
        ]
    })
    write_json(task_dir / "workflow_state.json", {
        "status": "in_progress",
        "next_page": 1,
        "captured_pages": 0,
        "parsed_rows": 0,
        "master_rows": 0,
        "direct_profile_links_captured": 0,
        "direct_profile_links_joined": 0,
        "profile_links_review_needed": 0,
        "review_count": 0,
        "coverage_status": "partial",
        "terminal_evidence": None,
        "stop_reason": "Task initialized; capture page 1."
    })
    write_json(task_dir / "coverage_audit.json", {
        "requested_start_page": 1,
        "pages": [],
        "coverage_status": "partial",
        "terminal_page": None,
        "terminal_reason": None,
        "note": "Task initialized."
    })
    print(json.dumps({"task_dir": str(task_dir), "next_page": 1, "network_scope": scope}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
