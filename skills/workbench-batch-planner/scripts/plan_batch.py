#!/usr/bin/env python3
"""Build a deterministic read-only BD email batch plan from two SQLite authorities."""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
PUBLIC_RE = re.compile(
    r"(^|[._+-])(admin|accounts?|after[.-]?sales|business|commercial|contact|customer|"
    r"enquiries|enquiry|hello|info|mail|marketing|office|orders?|parts?|reception|"
    r"repairs?|sales|service|servicing|spares?|support|team|warranty|web)([._+-]|$)",
    re.I,
)
EXCLUDED_LOCAL_RE = re.compile(
    r"(^|[._+-])(abuse|bounce|mailer-daemon|noreply|no-reply|postmaster|hire|hiring|"
    r"recruit|recruitment|careers?|jobs?|vacancies|talent|hr)([._+-]|$)",
    re.I,
)
HARD_BOUNCE_RE = re.compile(
    r"\b5\d\d\b|recipient unknown|user unknown|no such user|mailbox.*not found|"
    r"address.*not found|does not exist|invalid recipient|recipient rejected",
    re.I,
)
NO_GO = {
    "blacklist", "blacklisted", "blocked", "no-go", "no_go", "nogo", "paused",
    "replied", "suppressed", "unsubscribed",
}
SUCCESS = {"sent", "delivered", "accepted"}
ACTIVE_DRAFTS = {"pending_review", "approved", "queued"}
SHARED_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk",
    "yahoo.com", "aol.com", "icloud.com", "live.com", "btconnect.com", "orange.fr",
    "wanadoo.fr", "free.fr", "laposte.net", "protonmail.com", "proton.me", "gmx.com",
    "gmx.fr", "mail.com", "btinternet.com", "me.com", "msn.com",
}
RATING_ORDER = {"A+": 0, "A": 1, "A-": 2, "B+": 3, "B": 4, "B-": 5, "C": 6}


def connect_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def norm_name(value: object) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", norm(value))


def norm_domain(value: object) -> str:
    raw = norm(value)
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or parsed.path.split("/", 1)[0]).strip(".")
    return host[4:] if host.startswith("www.") else host


def extract_emails(value: object) -> set[str]:
    return {m.group(0).lower().strip(".,;:") for m in EMAIL_RE.finditer(str(value or ""))}


def valid_outreach_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    return bool(domain and not EXCLUDED_LOCAL_RE.search(local) and not domain.endswith(".invalid"))


def parse_dt(value: object, assume_tz: timezone = timezone.utc) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt.astimezone(timezone.utc)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def blocked_status(value: object) -> bool:
    text = norm(value)
    return text in NO_GO or bool(set(re.split(r"[^a-z0-9_-]+", text)) & NO_GO)


def route_sort(route: dict) -> tuple:
    return (
        1 if route["is_public"] else 0,
        0 if route["roll_ready"] else 1,
        route["rank"] if route["rank"] is not None else 999999,
        route["row_index"],
        route["email"],
    )


def route_history_eligibility(
    route: dict,
    last_success: datetime | None,
    contact_cooldown_cutoff: datetime,
) -> tuple[bool, str]:
    """Return current route eligibility without mutating durable roll metadata."""
    if route.get("roll_status") == "success_locked":
        return False, "source_success_locked"
    if last_success is not None and last_success >= contact_cooldown_cutoff:
        return False, "contact_7d_cooldown"
    if route.get("roll_ready"):
        return True, "source_ready"
    if last_success is None:
        return True, "derived_never_sent"
    return True, "derived_contact_cooldown_elapsed"


def compatible_template_ids(company: dict, selected: dict, templates: dict[int, dict]) -> list[int]:
    country = norm(company.get("country"))
    context = " ".join(norm(company.get(k)) for k in ("name", "company_type", "source", "remark"))
    if any(token in country for token in ("united kingdom", " uk", "england", "scotland", "wales", "ireland")):
        ids = [8, 14, 1, 3]
        if company.get("region"):
            ids.insert(2, 12)
        if "husqvarna" in context:
            ids.insert(0, 7)
        if any(x in context for x in ("line mark", "linemark", "pitch marking", "sports marking")):
            ids.insert(0, 15)
        if "cereals" in context:
            ids.insert(0, 6)
    elif "france" in country:
        ids = [9, 13, 1, 3]
    else:
        ids = [1, 3]
    if selected["is_public"]:
        ids.insert(0, 4)
    return [i for i in dict.fromkeys(ids) if i in templates and templates[i]["type"] == "first_touch"]


def snapshot(records: dict[str, dict], stage: str, previous: dict | None = None) -> dict:
    result = {
        "stage": stage,
        "companies": len(records),
        "route_emails": sum(len(x["routes"]) for x in records.values()),
    }
    result["removed_companies"] = 0 if previous is None else previous["companies"] - result["companies"]
    result["removed_route_emails"] = 0 if previous is None else previous["route_emails"] - result["route_emails"]
    return result


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def resolve_timezone(name: str, reference_utc: datetime | None = None):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        if name == "Europe/London":
            reference = (reference_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
            year = reference.year
            march_last_sunday = 31 - (calendar.weekday(year, 3, 31) + 1) % 7
            october_last_sunday = 31 - (calendar.weekday(year, 10, 31) + 1) % 7
            bst_start = datetime(year, 3, march_last_sunday, 1, tzinfo=timezone.utc)
            bst_end = datetime(year, 10, october_last_sunday, 1, tzinfo=timezone.utc)
            offset = 1 if bst_start <= reference < bst_end else 0
            return timezone(timedelta(hours=offset), name="Europe/London")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bd-db", type=Path, required=True)
    parser.add_argument("--workbench-db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--cooldown-hours", type=int, default=48)
    parser.add_argument("--contact-cooldown-days", type=int, default=7)
    parser.add_argument("--soft-bounce-days", type=int, default=14)
    parser.add_argument("--template-id", type=int, help="Use one explicitly selected active first-touch template for this plan.")
    args = parser.parse_args()

    started = time.perf_counter()
    now_local = datetime.now(resolve_timezone(args.timezone))
    now_utc = now_local.astimezone(timezone.utc)
    cooldown_cutoff = now_utc - timedelta(hours=args.cooldown_hours)
    contact_cooldown_cutoff = now_utc - timedelta(days=args.contact_cooldown_days)
    soft_cutoff = now_utc - timedelta(days=args.soft_bounce_days)
    args.out.mkdir(parents=True, exist_ok=True)

    with connect_ro(args.bd_db) as bd, connect_ro(args.workbench_db) as wb:
        unified_workbench = table_exists(wb, "unified_schema_version")
        companies = {
            r["company_id"]: {
                "id": r["company_id"], "name": r["standard_company_name"],
                "domain": norm_domain(r["domain"]), "country": r["country"], "region": r["primary_business_region"],
                "rating": r["rating"], "company_type": r["company_type"], "source": r["source"],
                "remark": r["company_remark"], "status": r["status"], "is_blacklisted": bool(r["is_blacklisted"]),
            }
            for r in bd.execute("SELECT company_id,standard_company_name,domain,country,primary_business_region,rating,company_type,source,company_remark,status,is_blacklisted FROM companies")
        }

        routes_by_company: dict[str, dict[str, dict]] = defaultdict(dict)
        source_email_owners: dict[str, set[str]] = defaultdict(set)
        for r in bd.execute("SELECT contact_id,company_id,row_index,contact_name,contact_position,contact_route,email_send_status,priority_contact_rank,priority_roll_status,raw_json FROM contacts"):
            if norm(r["email_send_status"]) != "include":
                continue
            excluded = set()
            raw = {}
            try:
                raw = json.loads(r["raw_json"] or "{}")
                excluded |= extract_emails(raw.get("email_excluded_addresses"))
            except (json.JSONDecodeError, TypeError):
                pass
            roll_status = norm(r["priority_roll_status"] or raw.get("priority_roll_status"))
            try:
                rank = int(float(str(r["priority_contact_rank"]))) if str(r["priority_contact_rank"] or "").strip() else None
            except ValueError:
                rank = None
            for email in extract_emails(r["contact_route"]) - excluded:
                if not valid_outreach_email(email):
                    continue
                is_public = bool(PUBLIC_RE.search(email.split("@", 1)[0])) or norm(r["contact_name"]) in {"team", "general", "public", "info", "contact"} or rank == 999
                route = {
                    "email": email, "is_public": is_public, "rank": rank,
                    "roll_ready": roll_status == "ready",
                    "roll_status": roll_status,
                    "row_index": r["row_index"] if r["row_index"] is not None else 999999,
                    "contact_id": r["contact_id"], "contact_name": r["contact_name"] or "",
                    "position": r["contact_position"] or "",
                }
                old = routes_by_company[r["company_id"]].get(email)
                if old is None or route_sort(route) < route_sort(old):
                    routes_by_company[r["company_id"]][email] = route
                source_email_owners[email].add(r["company_id"])

        company_query = (
            "SELECT company_id AS id,standard_company_name AS name,status FROM companies"
            if unified_workbench
            else "SELECT id,name,status FROM company"
        )
        wb_companies = {r["id"]: dict(r) for r in wb.execute(company_query)}
        wb_names: dict[str, set[int]] = defaultdict(set)
        for wid, company in wb_companies.items():
            wb_names[norm_name(company["name"])].add(wid)
        contact_company: dict[int, int] = {}
        contact_status: dict[int, str] = {}
        email_wb_owners: dict[str, set[int]] = defaultdict(set)
        active_route_owner: dict[tuple[int, str], set[int]] = defaultdict(set)
        contact_query = (
            "SELECT contact_id AS id,company_id,primary_email AS email,contact_status AS status FROM contacts"
            if unified_workbench
            else "SELECT id,company_id,email,status FROM contact"
        )
        for r in wb.execute(contact_query):
            contact_company[r["id"]] = r["company_id"]
            contact_status[r["id"]] = norm(r["status"])
            for email in extract_emails(r["email"]):
                email_wb_owners[email].add(r["company_id"])
                if contact_status[r["id"]] == "active":
                    active_route_owner[(r["company_id"], email)].add(r["id"])
        route_table = "contact_routes" if unified_workbench else "contactroute"
        if table_exists(wb, route_table):
            for r in wb.execute(f"SELECT contact_id,route_value,status FROM {route_table} WHERE route_type='email'"):
                wid = contact_company.get(r["contact_id"])
                if wid is None:
                    continue
                for email in extract_emails(r["route_value"]):
                    email_wb_owners[email].add(wid)
                    if norm(r["status"]) == "active" and contact_status.get(r["contact_id"]) == "active":
                        active_route_owner[(wid, email)].add(r["contact_id"])

        stable_links: dict[str, tuple[object, bool]] = {}
        if unified_workbench:
            stable_links = {
                cid: (cid, False)
                for cid in companies
                if cid in wb_companies
            }
        elif table_exists(wb, "companysourcelink"):
            for r in wb.execute("SELECT external_company_id,company_id,is_blocked FROM companysourcelink WHERE source_system='BDdb'"):
                stable_links[r["external_company_id"]] = (r["company_id"], bool(r["is_blocked"]))

        mapping: dict[str, dict] = {}
        for cid, company in companies.items():
            if cid in stable_links:
                wid, blocked = stable_links[cid]
                mapping[cid] = {"status": "blocked_link" if blocked else "mapped_stable", "wb_ids": [wid]}
                continue
            route_emails = set(routes_by_company.get(cid, {}))
            ids = set().union(*(email_wb_owners.get(email, set()) for email in route_emails)) if route_emails else set()
            if len(ids) == 1:
                mapping[cid] = {"status": "mapped_email", "wb_ids": sorted(ids)}
            elif len(ids) > 1:
                mapping[cid] = {"status": "ambiguous", "wb_ids": sorted(ids)}
            else:
                names = wb_names.get(norm_name(company["name"]), set())
                mapping[cid] = (
                    {"status": "mapped_name", "wb_ids": sorted(names)} if len(names) == 1
                    else {"status": "ambiguous", "wb_ids": sorted(names)} if len(names) > 1
                    else {"status": "unmapped", "wb_ids": []}
                )
        reverse: dict[int, list[str]] = defaultdict(list)
        for cid, m in mapping.items():
            if m["status"].startswith("mapped") and len(m["wb_ids"]) == 1:
                reverse[m["wb_ids"][0]].append(cid)
        for wid, cids in reverse.items():
            if len(cids) > 1:
                for cid in cids:
                    mapping[cid] = {"status": "ambiguous", "wb_ids": [wid]}

        # A prior successful exact route is reusable after the company cooldown.
        # Keep the count only as a lower-is-better ranking signal; never filter it.
        route_success_count: Counter[str] = Counter()
        route_success_times: dict[str, list[datetime]] = defaultdict(list)
        bd_company_success: dict[str, list[datetime]] = defaultdict(list)
        wb_company_success: dict[int, list[datetime]] = defaultdict(list)
        used_templates_bd: dict[str, set[int]] = defaultdict(set)
        used_templates_wb: dict[int, set[int]] = defaultdict(set)
        for r in bd.execute("SELECT company_id,recipient_email,smtp_status,sent_at,activity_date,raw_json FROM activity_records"):
            if norm(r["smtp_status"]) not in SUCCESS:
                continue
            emails = extract_emails(r["recipient_email"])
            for email in emails:
                route_success_count[email] += 1
            dt = parse_dt(r["sent_at"] or r["activity_date"])
            if dt:
                bd_company_success[r["company_id"]].append(dt)
                for email in emails:
                    route_success_times[email].append(dt)
            try:
                tid = json.loads(r["raw_json"] or "{}").get("template_id")
                if tid is not None:
                    used_templates_bd[r["company_id"]].add(int(tid))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        wb_history_query = (
            "SELECT company_id,recipient_email,smtp_status,COALESCE(sent_at,activity_date) AS sent_at,template_id FROM activity_records WHERE source_system='bd_email_workbench'"
            if unified_workbench
            else "SELECT company_id,recipient_email,smtp_status,sent_at,template_id FROM sendrecord"
        )
        for r in wb.execute(wb_history_query):
            if norm(r["smtp_status"]) in SUCCESS:
                emails = extract_emails(r["recipient_email"])
                for email in emails:
                    route_success_count[email] += 1
                dt = parse_dt(r["sent_at"])
                if dt:
                    wb_company_success[r["company_id"]].append(dt)
                    for email in emails:
                        route_success_times[email].append(dt)
            if r["template_id"] is not None:
                used_templates_wb[r["company_id"]].add(int(r["template_id"]))

        suppression_table = "suppressions" if unified_workbench else "suppression"
        suppressions = {norm(r[0]) for r in wb.execute(f"SELECT email FROM {suppression_table}") if r[0]}
        bounce_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
        bounce_table = "bounce_records" if unified_workbench else "bouncerecord"
        for r in wb.execute(f"SELECT recipient_email,bounce_reason,detected_at FROM {bounce_table}"):
            if norm(r["recipient_email"]):
                bounce_rows[norm(r["recipient_email"])].append(r)

        def bounce_reason(email: str) -> str | None:
            rows = bounce_rows.get(email, [])
            if not rows:
                return None
            if any(HARD_BOUNCE_RE.search(r["bounce_reason"] or "") for r in rows):
                return "hard_bounce"
            if len(rows) >= 2:
                return "repeated_bounce"
            latest = max((parse_dt(r["detected_at"]) for r in rows if parse_dt(r["detected_at"])), default=None)
            return "recent_soft_bounce" if latest and latest >= soft_cutoff else None

        active_wids = set()
        active_emails = set()
        active_draft_query = (
            "SELECT d.company_id,c.primary_email AS email FROM email_drafts d LEFT JOIN contacts c ON c.contact_id=d.contact_id WHERE d.status IN ('pending_review','approved','queued')"
            if unified_workbench
            else "SELECT d.company_id,c.email FROM emaildraft d LEFT JOIN contact c ON c.id=d.contact_id WHERE d.status IN ('pending_review','approved','queued')"
        )
        for r in wb.execute(active_draft_query):
            active_wids.add(r["company_id"])
            active_emails |= extract_emails(r["email"])
        template_query = (
            "SELECT template_id AS id,name,template_type FROM email_templates WHERE is_active=1"
            if unified_workbench
            else "SELECT id,name,template_type FROM emailtemplate WHERE is_active=1"
        )
        templates = {
            r["id"]: {"name": r["name"], "type": norm(r["template_type"])}
            for r in wb.execute(template_query)
        }
        if args.template_id is not None:
            forced_template = templates.get(args.template_id)
            if forced_template is None or forced_template["type"] != "first_touch":
                raise ValueError(f"--template-id {args.template_id} must name an active first-touch template.")

        records = {
            cid: {**companies[cid], "routes": sorted(route_map.values(), key=route_sort), "mapping": mapping[cid]}
            for cid, route_map in routes_by_company.items() if cid in companies and route_map
        }
        exclusions: list[dict] = []
        stages = [snapshot(records, "include_valid_email")]

        def exclude_company(cid: str, reason: str, details: object = None) -> None:
            record = records.pop(cid)
            exclusions.append({"bd_company_id": cid, "company_name": record["name"], "reason": reason, "details": details})

        previous = snapshot(records, "x")
        for cid in list(records):
            times = list(bd_company_success.get(cid, []))
            for wid in mapping[cid]["wb_ids"]:
                times += wb_company_success.get(wid, [])
            latest = max(times, default=None)
            if latest and latest >= cooldown_cutoff:
                exclude_company(cid, "company_48h_cooldown", latest.isoformat())
        stages.append(snapshot(records, "after_48h_cooldown", previous))

        previous = snapshot(records, "x")
        for cid in list(records):
            rec = records[cid]
            kept = []
            blocked_routes = []
            for route in rec["routes"]:
                route["prior_success_count"] = route_success_count[route["email"]]
                last_success = max(route_success_times.get(route["email"], []), default=None)
                eligible, eligibility_source = route_history_eligibility(
                    route,
                    last_success,
                    contact_cooldown_cutoff,
                )
                route["last_route_success_at"] = last_success.isoformat() if last_success else None
                route["eligibility_source"] = eligibility_source
                if eligible:
                    kept.append(route)
                else:
                    blocked_routes.append({
                        "email": route["email"],
                        "reason": eligibility_source,
                        "last_route_success_at": route["last_route_success_at"],
                    })
            rec["routes"] = kept
            if not kept:
                exclude_company(cid, "all_routes_in_contact_cooldown_or_success_locked", blocked_routes)
                continue
            # Preserve personal/public, roll state, and explicit contact priority.
            # Live route history is the eligibility gate; success count only breaks ties.
            rec["routes"].sort(key=lambda route: (*route_sort(route)[:3], route["prior_success_count"], *route_sort(route)[3:]))
        stages.append(snapshot(records, "after_route_7d_cooldown", previous))

        previous = snapshot(records, "x")
        for cid in list(records):
            rec = records[cid]
            wstatuses = [wb_companies[wid]["status"] for wid in mapping[cid]["wb_ids"] if wid in wb_companies]
            if rec["is_blacklisted"] or blocked_status(rec["status"]) or any(blocked_status(x) for x in wstatuses):
                exclude_company(cid, "no_go_or_protected_company_status", [rec["status"], *wstatuses])
                continue
            blocked_routes = []
            kept = []
            for route in rec["routes"]:
                reason = "suppression" if route["email"] in suppressions else bounce_reason(route["email"])
                if len(source_email_owners[route["email"]]) > 1:
                    reason = reason or "source_email_owner_conflict"
                if reason:
                    blocked_routes.append({"email": route["email"], "reason": reason})
                else:
                    kept.append(route)
            rec["routes"] = kept
            if not kept:
                exclude_company(cid, "all_routes_suppressed_bounced_or_conflicted", blocked_routes)
        stages.append(snapshot(records, "after_no_go_suppression_bounce", previous))

        # Existing drafts/queues are already arranged, not unavailable.  Keep
        # them visible in the plan, but never classify them as candidates for
        # an additional queue handoff.
        previous = snapshot(records, "x")
        for cid, rec in records.items():
            if set(mapping[cid]["wb_ids"]) & active_wids or {r["email"] for r in rec["routes"]} & active_emails:
                rec["already_arranged"] = True
        stages.append(snapshot(records, "after_active_draft", previous))

        previous = snapshot(records, "x")
        for cid in list(records):
            if mapping[cid]["status"] in {"ambiguous", "blocked_link"}:
                exclude_company(cid, "ambiguous_or_blocked_workbench_mapping", mapping[cid])
        stages.append(snapshot(records, "after_mapping_conflict", previous))

        previous = snapshot(records, "x")
        for cid in list(records):
            rec = records[cid]
            selected = rec["routes"][0]
            compatible = [args.template_id] if args.template_id is not None else compatible_template_ids(rec, selected, templates)
            used = set(used_templates_bd.get(cid, set()))
            for wid in mapping[cid]["wb_ids"]:
                used |= used_templates_wb.get(wid, set())
            contacted = bool(bd_company_success.get(cid)) or any(wb_company_success.get(wid) for wid in mapping[cid]["wb_ids"])
            unused = [tid for tid in compatible if tid not in used]
            if rec.get("already_arranged"):
                rec["selected_to"] = selected
                rec["selected_template_id"] = None
                rec["ever_contacted_company"] = contacted
                rec["optional_public_cc"] = None
                continue
            if not compatible or (contacted and not unused):
                exclude_company(cid, "template_rotation_conflict", {"contacted": contacted, "compatible": compatible, "used": sorted(used)})
                continue
            rec["selected_to"] = selected
            rec["selected_template_id"] = unused[0] if contacted else compatible[0]
            rec["ever_contacted_company"] = contacted
            public_cc = [r["email"] for r in rec["routes"] if r["is_public"] and r["email"] != selected["email"]]
            rec["optional_public_cc"] = public_cc[0] if public_cc else None
        stages.append(snapshot(records, "after_template_rotation", previous))

        groups: dict[str, list[str]] = defaultdict(list)
        for cid, rec in records.items():
            email = rec["selected_to"]["email"]
            recipient_domain = email.rsplit("@", 1)[-1]
            if recipient_domain in SHARED_EMAIL_DOMAINS:
                key = f"shared_email:{email}"
            elif rec["domain"]:
                key = f"bd_domain:{rec['domain']}"
            else:
                key = f"recipient_domain:{recipient_domain}"
            rec["enterprise_key"] = key
            groups[key].append(cid)
        domain_alternates = []
        previous = snapshot(records, "x")
        for key, cids in groups.items():
            if len(cids) < 2:
                continue
            ordered = sorted(cids, key=lambda cid: (
                1 if records[cid]["selected_to"]["is_public"] else 0,
                RATING_ORDER.get(str(records[cid]["rating"] or "").upper(), 99),
                route_sort(records[cid]["selected_to"]), cid,
            ))
            winner = ordered[0]
            for cid in ordered[1:]:
                rec = records.pop(cid)
                domain_alternates.append({
                    "bd_company_id": cid, "company_name": rec["name"], "selected_to": rec["selected_to"]["email"],
                    "alternate_reason": "enterprise_duplicate", "enterprise_key": key, "winner_bd_company_id": winner,
                })
        stages.append(snapshot(records, "after_enterprise_dedupe", previous))

        senders = []
        capacity = 0
        sender_query = (
            "SELECT sender_account_id AS id,email,daily_limit,window_start,window_end,send_timezone,send_windows_json FROM sender_accounts WHERE is_active=1 ORDER BY sender_account_id"
            if unified_workbench
            else "SELECT id,email,daily_limit,window_start,window_end,send_timezone,send_windows_json FROM senderaccount WHERE is_active=1 ORDER BY id"
        )
        send_table = "activity_records" if unified_workbench else "sendrecord"
        draft_table = "email_drafts" if unified_workbench else "emaildraft"
        sender_column = "sender_account_id"
        sent_time_column = "COALESCE(sent_at,activity_date)" if unified_workbench else "sent_at"
        for r in wb.execute(sender_query):
            sender_timezone = r["send_timezone"] or args.timezone
            sender_now = now_utc.astimezone(resolve_timezone(sender_timezone, now_utc))
            sender_local_start = sender_now.replace(hour=0, minute=0, second=0, microsecond=0)
            utc_start = sender_local_start.astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
            utc_end = (sender_local_start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
            sent = wb.execute(
                f"SELECT COUNT(*) FROM {send_table} WHERE {sender_column}=? AND lower(smtp_status) IN ('sent','delivered','accepted') AND {sent_time_column}>=? AND {sent_time_column}<?",
                (r["id"], utc_start, utc_end),
            ).fetchone()[0]
            reserved = wb.execute(
                f"SELECT COUNT(*) FROM {draft_table} WHERE {sender_column}=? AND status IN ('approved','queued') AND scheduled_at>=? AND scheduled_at<?",
                (r["id"], utc_start, utc_end),
            ).fetchone()[0]
            remaining = max(int(r["daily_limit"] or 0) - sent - reserved, 0)
            capacity += remaining
            senders.append({"id": r["id"], "email": r["email"], "daily_limit": r["daily_limit"], "sent_today": sent, "reserved_today": reserved, "remaining": remaining, "send_timezone": sender_timezone, "sender_local_date": sender_local_start.date().isoformat(), "window_start": r["window_start"], "window_end": r["window_end"], "send_windows_json": r["send_windows_json"]})

        ordered = sorted(records.values(), key=lambda rec: (
            1 if rec["ever_contacted_company"] else 0,
            RATING_ORDER.get(str(rec["rating"] or "").upper(), 99),
            rec["selected_to"].get("prior_success_count", 0),
            route_sort(rec["selected_to"]), rec["id"],
        ))
        main_rows = []
        capacity_alternates = []
        direct_count = 0
        already_arranged_count = 0
        new_queue_index = 0
        for index, rec in enumerate(ordered):
            mapping_row = rec["mapping"]
            already_arranged = bool(rec.get("already_arranged"))
            direct = (not already_arranged and len(mapping_row["wb_ids"]) == 1 and bool(active_route_owner.get((mapping_row["wb_ids"][0], rec["selected_to"]["email"]))))
            within_today_capacity = already_arranged or new_queue_index < capacity
            row = {
                "priority_order": index + 1, "bd_company_id": rec["id"], "company_name": rec["name"],
                "country": rec["country"], "rating": rec["rating"], "bd_company_domain": rec["domain"],
                "selected_to": rec["selected_to"]["email"], "to_type": "public_fallback" if rec["selected_to"]["is_public"] else "personal",
                "selected_roll_ready": rec["selected_to"]["roll_ready"],
                "source_roll_status": rec["selected_to"]["roll_status"],
                "needs_roll_activation": not rec["selected_to"]["roll_ready"],
                "eligibility_source": rec["selected_to"]["eligibility_source"],
                "last_route_success_at": rec["selected_to"]["last_route_success_at"],
                "prior_success_count": rec["selected_to"].get("prior_success_count", 0),
                "optional_public_cc": rec["optional_public_cc"], "template_id": rec["selected_template_id"],
                "ever_contacted_company": rec["ever_contacted_company"], "enterprise_key": rec["enterprise_key"],
                "mapping_status": mapping_row["status"], "workbench_company_ids": ";".join(map(str, mapping_row["wb_ids"])),
                "already_arranged": already_arranged,
                "newly_queueable": not already_arranged,
                "policy_eligible_today": True,
                "directly_draft_generatable_now": direct,
                "blocked_by_legacy_active_status": not direct and len(mapping_row["wb_ids"]) == 1,
                "within_today_capacity": within_today_capacity,
            }
            if direct:
                direct_count += 1
            if already_arranged:
                already_arranged_count += 1
            else:
                new_queue_index += 1
            main_rows.append(row)
            if not already_arranged and not within_today_capacity:
                capacity_alternates.append({**row, "alternate_reason": "today_sender_capacity_overflow"})

        alternates = domain_alternates + capacity_alternates
        exclusion_counts = Counter(x["reason"] for x in exclusions)
        elapsed = time.perf_counter() - started
        report = {
            "generated_at": now_local.isoformat(timespec="seconds"), "runtime_seconds": round(elapsed, 3), "read_only": True,
            "authority": {"bd_master_sqlite": str(args.bd_db.resolve()), "production_workbench": str(args.workbench_db.resolve())},
            "source_fingerprints": {"bd_sha256": sha256(args.bd_db), "workbench_sha256": sha256(args.workbench_db)},
            "policy": {"cooldown_hours": args.cooldown_hours, "contact_cooldown_days": args.contact_cooldown_days, "soft_bounce_days": args.soft_bounce_days, "forced_template_id": args.template_id, "roll_status_is_advisory": True, "workbench_schema": "unified" if unified_workbench else "legacy", "shared_email_domains": sorted(SHARED_EMAIL_DOMAINS), "enterprise_key_order": "shared recipient provider -> full email; otherwise BD company domain; otherwise recipient domain"},
            "stages": stages, "exclusion_counts": dict(sorted(exclusion_counts.items())), "exclusions": exclusions,
            "sender_capacity": {"total_remaining": capacity, "senders": senders},
            "final": {
                "safe_after_enterprise_dedupe": len(main_rows), "already_arranged": already_arranged_count,
                "newly_queueable": len(main_rows) - already_arranged_count,
                "today_main_count": min(len(main_rows) - already_arranged_count, capacity),
                "capacity_overflow_count": max(len(main_rows) - capacity, 0), "enterprise_alternate_count": len(domain_alternates),
                "personal_to": sum(x["to_type"] == "personal" for x in main_rows), "public_fallback_to": sum(x["to_type"] == "public_fallback" for x in main_rows),
                "selected_roll_ready": sum(bool(x["selected_roll_ready"]) for x in main_rows),
                "needs_roll_activation": sum(bool(x["needs_roll_activation"]) for x in main_rows),
                "optional_public_cc": sum(bool(x["optional_public_cc"]) for x in main_rows),
                "directly_draft_generatable_now": direct_count,
                "requires_sync_or_route_import": sum(not x["directly_draft_generatable_now"] and not x["already_arranged"] for x in main_rows),
            },
            "main_candidates": main_rows, "alternates": alternates,
        }

    fields = ["priority_order", "bd_company_id", "company_name", "country", "rating", "bd_company_domain", "selected_to", "to_type", "selected_roll_ready", "source_roll_status", "needs_roll_activation", "eligibility_source", "last_route_success_at", "prior_success_count", "optional_public_cc", "template_id", "ever_contacted_company", "enterprise_key", "mapping_status", "workbench_company_ids", "already_arranged", "newly_queueable", "policy_eligible_today", "directly_draft_generatable_now", "blocked_by_legacy_active_status", "within_today_capacity"]
    write_csv(args.out / "main_candidates.csv", main_rows, fields)
    write_csv(args.out / "alternates.csv", alternates, ["alternate_reason", "priority_order", "bd_company_id", "company_name", "selected_to", "enterprise_key", "winner_bd_company_id", "within_today_capacity"])
    (args.out / "batch_plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = [
        "# Workbench batch plan", "", f"Generated: `{report['generated_at']}` in **{report['runtime_seconds']}s** (read-only).", "",
        f"- Safe after enterprise dedupe: **{report['final']['safe_after_enterprise_dedupe']}** (already arranged: **{report['final']['already_arranged']}**; newly queueable: **{report['final']['newly_queueable']}**)",
        f"- Today unreserved sender capacity: **{capacity}**; new queue today: **{report['final']['today_main_count']}**",
        f"- Personal To: **{report['final']['personal_to']}**; public fallback To: **{report['final']['public_fallback_to']}**; optional public CC: **{report['final']['optional_public_cc']}**",
        f"- Selected roll-ready: **{report['final']['selected_roll_ready']}**; needs roll activation: **{report['final']['needs_roll_activation']}**",
        f"- Directly draft-generatable now: **{direct_count}**; requires sync/route import: **{report['final']['requires_sync_or_route_import']}**", "",
        "## Stages", "", "| Stage | Companies | Routes | Removed companies | Removed routes |", "|---|---:|---:|---:|---:|",
    ]
    summary += [f"| {s['stage']} | {s['companies']} | {s['route_emails']} | {s['removed_companies']} | {s['removed_route_emails']} |" for s in stages]
    summary += ["", "No draft, queue, send, or production write was performed."]
    (args.out / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out.resolve()), "runtime_seconds": report["runtime_seconds"], "final": report["final"], "sender_capacity": capacity}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
