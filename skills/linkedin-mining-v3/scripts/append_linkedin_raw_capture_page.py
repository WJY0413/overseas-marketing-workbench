#!/usr/bin/env python3
"""Atomically append one captured LinkedIn people-search page to a task directory.

Read a JSON object from stdin with page, source_url, next_visible, and cards.
Cards must retain only result-card fields plus a direct same-card /in/ URL.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path


COUNTRY_CODES = {
    "United Kingdom": "UK",
    "South Africa": "ZA",
    "United States": "US",
    "Ireland": "IE",
    "Australia": "AU",
    "New Zealand": "NZ",
    "Canada": "CA",
}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, newline="\n") as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def country_code(location: str) -> str:
    for country, code in COUNTRY_CODES.items():
        if re.search(rf"(?:^|,\s*){re.escape(country)}$", location):
            return code
    return ""


def contact_stem(source_target: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", source_target.lower().replace("connectionof:", "connectionof_"))


def canonical_profile_url(value: str) -> str:
    base = value.strip().split("?", 1)[0].rstrip("/")
    return base + "/" if base else ""


def validate_terminal_confirmation(payload: dict, cards: list[dict]) -> dict | None:
    """Require two delayed matching observations before accepting any no-Next terminal page."""
    if payload.get("next_visible"):
        return None
    confirmation = payload.get("terminal_confirmation")
    if not isinstance(confirmation, dict):
        raise SystemExit("terminal page requires terminal_confirmation")
    for key in ("initial_wait_ms", "recheck_wait_ms", "first_card_count", "second_card_count"):
        if not isinstance(confirmation.get(key), int):
            raise SystemExit(f"terminal_confirmation.{key} must be an integer")
    if confirmation["initial_wait_ms"] < 5000 or confirmation["recheck_wait_ms"] < 8000:
        raise SystemExit("terminal_confirmation requires waits of at least 5000 ms and 8000 ms")
    if confirmation.get("first_url") != payload.get("source_url") or confirmation.get("second_url") != payload.get("source_url"):
        raise SystemExit("terminal_confirmation URLs must match source_url")
    if confirmation.get("first_next_visible") is not False or confirmation.get("second_next_visible") is not False:
        raise SystemExit("terminal_confirmation must observe Next absent twice")
    if confirmation["first_card_count"] != len(cards) or confirmation["second_card_count"] != len(cards):
        raise SystemExit("terminal_confirmation card counts must match payload cards")
    sequence = "\n".join(canonical_profile_url(str(card.get("linkedin_profile", ""))) for card in cards)
    sequence_sha256 = hashlib.sha256(sequence.encode("utf-8")).hexdigest()
    if confirmation.get("first_profile_sequence_sha256") != sequence_sha256:
        raise SystemExit("terminal_confirmation first profile sequence must match payload cards")
    if confirmation.get("second_profile_sequence_sha256") != sequence_sha256:
        raise SystemExit("terminal_confirmation second profile sequence must match payload cards")
    return confirmation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--payload-b64", help="UTF-8 JSON payload encoded as Base64; otherwise read stdin")
    args = parser.parse_args()

    payload = json.loads(base64.b64decode(args.payload_b64).decode("utf-8")) if args.payload_b64 else json.load(sys.stdin)
    task_dir = args.task_dir
    manifest = json.loads((task_dir / "task_manifest.json").read_text(encoding="utf-8"))
    state_path = task_dir / "workflow_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    page = int(payload["page"])
    expected_page = state.get("next_page")
    if expected_page is not None and page != expected_page:
        raise SystemExit(f"expected page {expected_page}, received {page}")
    cards = payload.get("cards", [])
    if not isinstance(cards, list):
        raise SystemExit("cards must be a list")
    if not cards and payload.get("next_visible"):
        raise SystemExit("empty page cannot report Next")
    terminal_confirmation = validate_terminal_confirmation(payload, cards)
    scope_to_degree = {"F": "1st", "S": "2nd"}
    allowed_degrees = {scope_to_degree[item] for item in manifest.get("network_scope", []) if item in scope_to_degree}
    if not allowed_degrees:
        raise SystemExit("task manifest has no supported LinkedIn network scope")
    seen_page_profiles: set[str] = set()
    for card in cards:
        profile = str(card.get("linkedin_profile", ""))
        if not profile.startswith("https://www.linkedin.com/in/"):
            raise SystemExit("every card requires a direct linkedin.com/in URL")
        canonical = canonical_profile_url(profile)
        if canonical in seen_page_profiles:
            raise SystemExit("duplicate canonical profile URL within one page")
        seen_page_profiles.add(canonical)
        if str(card.get("relationship_degree", "")).strip() not in allowed_degrees:
            raise SystemExit("card relationship_degree is outside the task network scope")
        for field in ("name", "identity", "page_intro"):
            if not str(card.get(field, "")).strip():
                raise SystemExit(f"missing required card field: {field}")

    raw_path = task_dir / "raw_pages" / f"page_{page:03d}.txt"
    if raw_path.exists():
        raise SystemExit(f"raw page already exists: {raw_path}")
    raw_lines = [
        "capture_method: browser_dom_snapshot",
        f"source_target: {manifest['source_target']}",
        f"source_url: {payload['source_url']}",
        f"page: {page}",
        f"next_visible: {str(bool(payload.get('next_visible'))).lower()}",
        "",
    ]
    for card in cards:
        raw_lines.extend([card["page_intro"], f"direct_profile: {card['linkedin_profile']}", ""])

    contacts_path = task_dir / "contacts_master.jsonl"
    links_path = task_dir / "source_profile_links.jsonl"
    duplicate_review_path = task_dir / "review_needed.jsonl"
    contacts = read_jsonl(contacts_path)
    links = read_jsonl(links_path)
    duplicate_reviews = read_jsonl(duplicate_review_path)
    known_profiles: dict[str, list[str]] = {}
    for row in contacts:
        known_profiles.setdefault(row["linkedin_profile"], []).append(row["contact_id"])
    start = len(contacts) + 1
    stem = contact_stem(manifest["source_target"])
    new_contacts = []
    new_links = []
    new_duplicate_reviews = []
    for source_index, card in enumerate(cards, start=1):
        location = str(card.get("location", "")).strip()
        code = country_code(location)
        review_status = "captured" if code else "review_needed"
        note = "direct /in/ URL from the same result card" if code else "direct /in/ URL from the same result card; location does not explicitly establish a country_code"
        mutual = ""
        marker = f"Invite {card['name']} to connect "
        if marker in card["page_intro"]:
            mutual = card["page_intro"].split(marker, 1)[1]
        common = {
            "source_target": manifest["source_target"],
            "source_page": str(page),
            "source_file": str(raw_path.relative_to(task_dir)).replace("\\", "/"),
            "name": card["name"].strip(),
            "identity": card["identity"].strip(),
            "linkedin_profile": card["linkedin_profile"].strip(),
        }
        new_links.append({**common, "profile_match_status": "direct_same_card", "raw_card_text": card["page_intro"].strip()})
        existing_contact_ids = known_profiles.get(card["linkedin_profile"].strip(), [])
        if existing_contact_ids:
            new_duplicate_reviews.append({
                **common,
                "review_status": "duplicate_profile_url",
                "existing_contact_ids": existing_contact_ids,
                "note": "Raw result-card evidence retained; duplicate direct profile URL was not re-added to contacts_master.jsonl.",
            })
            continue
        new_contacts.append({
            "contact_id": f"{stem}_p{page:03d}_{start + source_index - 1:03d}",
            **common,
            "location": location,
            "country_code": code,
            "mutual": mutual,
            "action_label": card.get("action_label", "Connect"),
            "page_intro": card["page_intro"].strip(),
            "raw_chunk": card["page_intro"].strip(),
            "review_status": review_status,
            "review_note": note,
        })

    audit_path = task_dir / "coverage_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {"requested_start_page": page, "pages": []}
    previous = audit["pages"][-1] if audit["pages"] else None
    signature = (cards[0]["name"], cards[-1]["name"]) if cards else ("", "")
    if previous and (previous.get("first_contact"), previous.get("last_contact")) == signature:
        raise SystemExit("repeated first/last signature; page may be stale")
    audit["pages"].append({
        "page": page,
        "raw_file": str(raw_path.relative_to(task_dir)).replace("\\", "/"),
        "valid": bool(cards),
        "result_count": len(cards),
        "new_result_count": len(new_contacts),
        "duplicate_profile_count": len(new_duplicate_reviews),
        "first_contact": signature[0],
        "last_contact": signature[1],
        "next_visible": bool(payload.get("next_visible")),
        "terminal_confirmation": terminal_confirmation,
        "capture_method": "browser_dom_snapshot",
    })
    if cards and not payload.get("next_visible"):
        audit.update({"coverage_status": "complete", "terminal_page": page, "terminal_reason": "final_valid_page_without_next", "note": "Terminal evidence captured."})
        state.update({"status": "complete", "next_page": None, "coverage_status": "complete", "terminal_evidence": "final_valid_page_without_next", "stop_reason": f"Page {page} was valid and had no Next button."})
    elif not cards and not payload.get("next_visible"):
        audit.update({"coverage_status": "complete", "terminal_page": page - 1, "terminal_probe_page": page, "terminal_reason": "explicit_no_results_after_final_valid_page", "note": "Terminal no-results probe captured."})
        state.update({"status": "complete", "next_page": None, "coverage_status": "complete", "terminal_evidence": "explicit_no_results_after_final_valid_page", "stop_reason": f"Page {page} was an explicit no-results terminal probe."})
    else:
        audit.update({"coverage_status": "partial", "terminal_page": None, "terminal_reason": None, "note": f"Page {page} is valid and Next remains visible."})
        state.update({"status": "in_progress", "next_page": page + 1, "coverage_status": "partial", "terminal_evidence": None, "stop_reason": f"Page {page} captured successfully; resume from page {page + 1}."})
    all_contacts = contacts + new_contacts
    all_links = links + new_links
    all_duplicate_reviews = duplicate_reviews + new_duplicate_reviews
    state.update({
        "captured_pages": len([item for item in audit["pages"] if item["result_count"]]),
        "parsed_rows": len(all_contacts),
        "master_rows": len(all_contacts),
        "direct_profile_links_captured": len(all_links),
        "direct_profile_links_joined": len(all_contacts),
        "profile_links_review_needed": len(all_duplicate_reviews),
        "review_count": sum(row["review_status"] == "review_needed" for row in all_contacts),
    })
    write_atomic(raw_path, "\n".join(raw_lines) + "\n")
    write_atomic(contacts_path, "\n".join(json.dumps(row, ensure_ascii=False) for row in all_contacts) + "\n")
    write_atomic(links_path, "\n".join(json.dumps(row, ensure_ascii=False) for row in all_links) + "\n")
    if all_duplicate_reviews:
        write_atomic(duplicate_review_path, "\n".join(json.dumps(row, ensure_ascii=False) for row in all_duplicate_reviews) + "\n")
    write_atomic(audit_path, json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
    write_atomic(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"page": page, "cards": len(cards), "next_visible": bool(payload.get("next_visible")), "status": state["status"], "next_page": state["next_page"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
