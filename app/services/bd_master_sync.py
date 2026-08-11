from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import Engine, func
from sqlmodel import Session, select

from app.models import (
    BdMasterSyncConflict,
    BdMasterSyncRun,
    BounceRecord,
    Company,
    CompanySourceLink,
    Contact,
    ContactRoute,
    Suppression,
)
from app.time_utils import utc_now
from app.services.suppression import is_active_suppression


SOURCE_SYSTEM = "BDdb"
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
COMPANY_NO_GO_STATUSES = {
    "blacklist",
    "blacklisted",
    "blocked",
    "no-go",
    "no_go",
    "replied",
    "suppressed",
    "unsubscribed",
}
CONTACT_PROTECTED_STATUSES = COMPANY_NO_GO_STATUSES | {"bounce", "bounced", "hard_bounce"}
SUCCESS_STATUSES = {"completed", "completed_with_conflicts", "unchanged"}
FATAL_CONFLICT_TYPES = {
    "bd_company_id_reuse",
    "domain_ambiguity",
    "orphan_source_link",
    "unbounded_conflict_scope",
}
COMPANY_QUARANTINE_CONFLICT_TYPES = {
    "source_email_owner_conflict",
    "ambiguous_email_bootstrap",
    "workbench_company_link_conflict",
    "ambiguous_legacy_name",
    "workbench_email_owner_conflict",
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _normalized_name(value: object) -> str:
    return " ".join(_text(value).casefold().split())


def _normalized_domain(value: object) -> str:
    raw = _text(value).casefold()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or parsed.path.split("/", 1)[0]).strip(".")
    return host[4:] if host.startswith("www.") else host


def _emails(value: object) -> list[str]:
    return list(dict.fromkeys(match.group(0).casefold() for match in EMAIL_RE.finditer(_text(value))))


def _rank(value: object) -> int | None:
    try:
        return int(_text(value))
    except (TypeError, ValueError):
        return None


def _is_company_no_go(company: dict[str, Any]) -> bool:
    blacklist = company.get("blacklist") or {}
    return bool(blacklist.get("is_blacklisted")) or _text(company.get("status")).casefold() in COMPANY_NO_GO_STATUSES


def _source_region(company: dict[str, Any]) -> str:
    return _text(company.get("business_region_label") or company.get("primary_business_region"))


def _candidate_for_row(row: dict[str, Any], email: str) -> dict[str, Any]:
    rank = _rank(row.get("priority_contact_rank"))
    ready = _text(row.get("priority_roll_status")).casefold() == "ready"
    return {
        "email": email,
        "full_name": _text(row.get("联系人")) or "Team",
        "position": _text(row.get("联系人职位")) or None,
        "rank": rank,
        "ready": ready,
        "is_primary": ready and rank is not None and rank < 999,
    }


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, str]:
    return (
        0 if candidate["ready"] else 1,
        candidate["rank"] if candidate["rank"] is not None else 10000,
        _normalized_name(candidate["full_name"]),
    )


def _prepare_source(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    companies = data.get("companies")
    if not isinstance(companies, dict):
        raise ValueError("BD source JSON must contain a companies object.")

    prepared: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    for source_key, raw_company in companies.items():
        if not isinstance(raw_company, dict):
            rejects.append({"reason": "invalid_company_object", "source_key": _text(source_key)})
            continue
        external_id = _text(raw_company.get("company_id"))
        name = _text(raw_company.get("standard_company_name"))
        if not external_id or not name:
            rejects.append(
                {
                    "reason": "missing_company_id_or_name",
                    "source_key": _text(source_key),
                    "external_company_id": external_id,
                }
            )
            continue

        domain = _normalized_domain(raw_company.get("domain") or source_key)
        company_no_go = _is_company_no_go(raw_company)
        qualified_candidates: dict[str, dict[str, Any]] = {}
        all_emails: set[str] = set()
        disqualified_emails: set[str] = set()
        rows = raw_company.get("contact_rows") or []
        if not isinstance(rows, list):
            rejects.append({"reason": "invalid_contact_rows", "external_company_id": external_id})
            rows = []

        for row in rows:
            if not isinstance(row, dict):
                rejects.append({"reason": "invalid_contact_object", "external_company_id": external_id})
                continue
            route_emails = set(_emails(row.get("联系方式")))
            excluded_emails = set(_emails(row.get("email_excluded_addresses")))
            all_emails.update(route_emails)
            all_emails.update(excluded_emails)
            send_status = _text(row.get("email_send_status")).casefold()
            if company_no_go or send_status != "include":
                disqualified_emails.update(route_emails)
                disqualified_emails.update(excluded_emails)
                continue
            disqualified_emails.update(excluded_emails)
            for email in sorted(route_emails - excluded_emails):
                candidate = _candidate_for_row(row, email)
                current = qualified_candidates.get(email)
                if current is None or _candidate_sort_key(candidate) < _candidate_sort_key(current):
                    qualified_candidates[email] = candidate

        prepared.append(
            {
                "raw": raw_company,
                "external_id": external_id,
                "name": name,
                "normalized_name": _normalized_name(name),
                "domain": domain,
                "company_no_go": company_no_go,
                "qualified": qualified_candidates,
                "all_emails": all_emails,
                "disqualified": disqualified_emails - set(qualified_candidates),
            }
        )
    return prepared, len(companies), rejects


def _json_object(value: object, *, label: str) -> dict[str, Any]:
    try:
        data = json.loads(_text(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"BD source SQLite contains invalid {label} raw_json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"BD source SQLite {label} raw_json must be an object.")
    return data


def _sqlite_source_data(source_path: Path) -> tuple[dict[str, Any], str, int]:
    uri = f"file:{source_path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise OSError(f"BD source SQLite database is not readable: {source_path}: {exc}") from exc

    company_columns = {
        "company_id", "domain", "standard_company_name", "website", "country", "rating",
        "company_type", "source", "status", "is_blacklisted", "primary_business_region", "raw_json",
    }
    contact_columns = {
        "company_id", "row_index", "contact_name", "contact_position", "contact_route",
        "email_send_status", "priority_roll_status", "priority_contact_rank", "raw_json",
    }
    try:
        available_companies = {row[1] for row in connection.execute("PRAGMA table_info(companies)")}
        available_contacts = {row[1] for row in connection.execute("PRAGMA table_info(contacts)")}
        missing = sorted(company_columns - available_companies)
        missing += [f"contacts.{name}" for name in sorted(contact_columns - available_contacts)]
        if missing:
            raise ValueError(f"BD source SQLite schema is missing columns: {', '.join(missing)}")

        digest = hashlib.sha256()
        companies: dict[str, dict[str, Any]] = {}
        by_external_id: dict[str, dict[str, Any]] = {}
        company_rows = connection.execute(
            """
            SELECT company_id, domain, standard_company_name, website, country, rating,
                   company_type, source, status, is_blacklisted, primary_business_region, raw_json
              FROM companies
             ORDER BY company_id
            """
        )
        for ordinal, row in enumerate(company_rows):
            digest.update("\x1f".join(_text(row[column]) for column in row.keys()).encode("utf-8"))
            digest.update(b"\n")
            company_id = _text(row["company_id"])
            company = _json_object(row["raw_json"], label=f"company {company_id or ordinal}")
            # Explicit normalized columns are authoritative; do not inherit stale nested contacts.
            company.update(
                {
                    "company_id": company_id,
                    "domain": _text(row["domain"]),
                    "standard_company_name": _text(row["standard_company_name"]),
                    "website": _text(row["website"]),
                    "country": _text(row["country"]),
                    "rating": _text(row["rating"]),
                    "company_type": _text(row["company_type"]),
                    "source": _text(row["source"]),
                    "status": _text(row["status"]),
                    "primary_business_region": _text(row["primary_business_region"]),
                    "blacklist": {"is_blacklisted": bool(row["is_blacklisted"])},
                    "contact_rows": [],
                }
            )
            companies[f"{company_id}\x1f{ordinal}"] = company
            by_external_id[company_id] = company

        contact_rows = connection.execute(
            """
            SELECT company_id, row_index, contact_name, contact_position, contact_route,
                   email_send_status, priority_roll_status, priority_contact_rank, raw_json
              FROM contacts
             ORDER BY company_id, row_index
            """
        )
        for row in contact_rows:
            digest.update("\x1f".join(_text(row[column]) for column in row.keys()).encode("utf-8"))
            digest.update(b"\n")
            company_id = _text(row["company_id"])
            company = by_external_id.get(company_id)
            if company is None:
                raise ValueError(f"BD source SQLite contact references unknown company_id: {company_id}")
            contact = _json_object(row["raw_json"], label=f"contact {company_id}/{row['row_index']}")
            # These aliases preserve the established Workbench sync contract.
            contact.update(
                {
                    "联系人": _text(row["contact_name"]) or _text(contact.get("联系人")),
                    "联系人职位": _text(row["contact_position"]) or _text(contact.get("联系人职位")),
                    "联系方式": _text(row["contact_route"]) or _text(contact.get("联系方式")),
                    "email_send_status": _text(row["email_send_status"]),
                    "priority_roll_status": _text(row["priority_roll_status"]),
                    "priority_contact_rank": _text(row["priority_contact_rank"]),
                }
            )
            company["contact_rows"].append(contact)
    except sqlite3.Error as exc:
        raise ValueError(f"Unable to read BD source SQLite database: {exc}") from exc
    finally:
        connection.close()

    return {"schema": "bddb-sqlite-v1", "companies": companies}, digest.hexdigest(), source_path.stat().st_mtime_ns


def _load_source(source_path: Path | None) -> tuple[dict[str, Any], str, int]:
    if source_path is None:
        raise ValueError("BD_DATABASE_SQLITE_PATH is not configured.")
    if not source_path.exists():
        raise FileNotFoundError(f"BD source path does not exist: {source_path}")
    if not source_path.is_file():
        raise ValueError(f"BD source path is not a file: {source_path}")
    if source_path.suffix.casefold() in {".sqlite", ".db", ".sqlite3"}:
        return _sqlite_source_data(source_path)
    try:
        payload = source_path.read_bytes()
    except OSError as exc:
        raise OSError(f"BD source path is not readable: {source_path}: {exc}") from exc
    try:
        data = json.loads(payload.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"Unable to parse legacy BD source JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("BD source JSON root must be an object.")
    return data, hashlib.sha256(payload).hexdigest(), source_path.stat().st_mtime_ns


def _conflict(
    conflicts: list[dict[str, Any]],
    conflict_type: str,
    *,
    external_company_id: str | None = None,
    domain: str | None = None,
    email: str | None = None,
    **details: Any,
) -> None:
    conflicts.append(
        {
            "conflict_type": conflict_type,
            "external_company_id": external_company_id,
            "domain": domain,
            "email": email,
            "details": details,
        }
    )


def _apply_company_fields(company: Company, source: dict[str, Any]) -> bool:
    authoritative = {
        "name": source["name"],
        "country": _text(source["raw"].get("country")) or None,
        "region": _source_region(source["raw"]) or None,
        "company_type": _text(source["raw"].get("company_type")) or None,
        "source": _text(source["raw"].get("source")) or "BDdb",
        "priority": _text(source["raw"].get("rating")) or company.priority or "B",
    }
    changed = False
    for field_name, incoming in authoritative.items():
        if getattr(company, field_name) != incoming:
            setattr(company, field_name, incoming)
            changed = True
    current_status = _text(company.status).casefold()
    if source["company_no_go"] and current_status not in COMPANY_NO_GO_STATUSES:
        company.status = "blacklisted"
        changed = True
    if changed:
        company.updated_at = utc_now()
    return changed


def _sync_prepared(
    session: Session,
    prepared: list[dict[str, Any]],
    fingerprint: str,
    initial_rejects: list[dict[str, Any]],
) -> tuple[Counter[str], list[dict[str, Any]], list[dict[str, Any]]]:
    counts: Counter[str] = Counter()
    conflicts: list[dict[str, Any]] = []
    rejects = list(initial_rejects)
    now = utc_now()

    id_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    domain_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    name_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    email_groups: dict[str, set[str]] = defaultdict(set)
    for source in prepared:
        id_groups[source["external_id"]].append(source)
        if source["domain"]:
            domain_groups[source["domain"]].append(source)
        name_groups[source["normalized_name"]].append(source)
        for email in source["qualified"]:
            email_groups[email].add(source["external_id"])

    blocked_ids: set[str] = set()
    blocked_emails: set[str] = set()
    quarantine_company_ids: set[str] = set()
    quarantine_emails: set[str] = set()
    for external_id, items in id_groups.items():
        if len(items) > 1:
            blocked_ids.add(external_id)
            _conflict(
                conflicts,
                "bd_company_id_reuse",
                external_company_id=external_id,
                domains=sorted({item["domain"] for item in items}),
            )
    for domain, items in domain_groups.items():
        ids = sorted({item["external_id"] for item in items})
        if len(ids) > 1:
            blocked_ids.update(ids)
            _conflict(conflicts, "domain_ambiguity", domain=domain, external_company_ids=ids)
    for email, ids in email_groups.items():
        if len(ids) > 1:
            blocked_emails.add(email)
            quarantine_emails.add(email)
            quarantine_company_ids.update(ids)
            blocked_ids.update(ids)
            _conflict(
                conflicts,
                "source_email_owner_conflict",
                email=email,
                external_company_ids=sorted(ids),
                quarantine_scope="companies_and_email",
            )

    # Source identity conflicts have global scope. Do not begin any business-row work.
    if any(item["conflict_type"] in FATAL_CONFLICT_TYPES for item in conflicts):
        counts["reject_count"] = len(rejects)
        counts["conflict_count"] = len(conflicts)
        counts["quarantine_company_count"] = len(quarantine_company_ids)
        counts["quarantine_email_count"] = len(quarantine_emails)
        counts["fatal_conflict_count"] = sum(
            item["conflict_type"] in FATAL_CONFLICT_TYPES for item in conflicts
        )
        return counts, conflicts, rejects

    companies = list(session.exec(select(Company)))
    companies_by_id = {company.id: company for company in companies if company.id is not None}
    legacy_names: dict[str, list[Company]] = defaultdict(list)
    for company in companies:
        legacy_names[_normalized_name(company.name)].append(company)

    contacts = list(session.exec(select(Contact)))
    contacts_by_email = {_text(contact.email).casefold(): contact for contact in contacts}
    suppressed = {
        _text(item.email).casefold()
        for item in session.exec(select(Suppression))
        if _text(item.email) and is_active_suppression(item)
    }
    bounced = {
        _text(email).casefold()
        for email in session.exec(select(BounceRecord.recipient_email))
        if _text(email)
    }
    routes = list(session.exec(select(ContactRoute).where(ContactRoute.route_type == "email")))
    email_routes = {
        (route.contact_id, _text(route.route_value).casefold()): route
        for route in routes
    }

    links = list(session.exec(select(CompanySourceLink).where(CompanySourceLink.source_system == SOURCE_SYSTEM)))
    links_by_external = {link.external_company_id: link for link in links}
    links_by_company = {link.company_id: link for link in links}

    for source in prepared:
        external_id = source["external_id"]
        link = links_by_external.get(external_id)
        inserted_company_this_run = False
        if external_id in blocked_ids:
            if link:
                link.is_blocked = True
                link.updated_at = now
                session.add(link)
            continue

        company: Company | None = None
        if link is not None:
            if link.source_domain and source["domain"] and _normalized_domain(link.source_domain) != source["domain"]:
                link.is_blocked = True
                link.updated_at = now
                session.add(link)
                blocked_ids.add(external_id)
                _conflict(
                    conflicts,
                    "bd_company_id_reuse",
                    external_company_id=external_id,
                    domain=source["domain"],
                    previous_domain=link.source_domain,
                    company_id=link.company_id,
                )
                continue
            company = companies_by_id.get(link.company_id)
            if company is None:
                link.is_blocked = True
                link.updated_at = now
                session.add(link)
                _conflict(
                    conflicts,
                    "orphan_source_link",
                    external_company_id=external_id,
                    domain=source["domain"],
                    company_id=link.company_id,
                )
                continue
        else:
            owner_company_ids = {
                contacts_by_email[email].company_id
                for email in source["all_emails"] - blocked_emails
                if email in contacts_by_email
            }
            if len(owner_company_ids) > 1:
                _conflict(
                    conflicts,
                    "ambiguous_email_bootstrap",
                    external_company_id=external_id,
                    domain=source["domain"],
                    company_ids=sorted(owner_company_ids),
                )
                blocked_ids.add(external_id)
                quarantine_company_ids.add(external_id)
                quarantine_emails.update(source["all_emails"] & set(contacts_by_email))
                continue
            if len(owner_company_ids) == 1:
                owner_id = next(iter(owner_company_ids))
                existing_owner_link = links_by_company.get(owner_id)
                if existing_owner_link and existing_owner_link.external_company_id != external_id:
                    _conflict(
                        conflicts,
                        "workbench_company_link_conflict",
                        external_company_id=external_id,
                        domain=source["domain"],
                        company_id=owner_id,
                        linked_external_company_id=existing_owner_link.external_company_id,
                    )
                    blocked_ids.add(external_id)
                    quarantine_company_ids.add(external_id)
                    quarantine_emails.update(source["all_emails"] & set(contacts_by_email))
                    continue
                company = companies_by_id.get(owner_id)
                match_method = "email_overlap"
            else:
                name_candidates = legacy_names.get(source["normalized_name"], [])
                unlinked_name_candidates = [candidate for candidate in name_candidates if candidate.id not in links_by_company]
                if len(name_groups[source["normalized_name"]]) == 1 and len(unlinked_name_candidates) == 1:
                    company = unlinked_name_candidates[0]
                    match_method = "legacy_unique_name"
                elif len(name_groups[source["normalized_name"]]) == 1 and len(unlinked_name_candidates) > 1:
                    _conflict(
                        conflicts,
                        "ambiguous_legacy_name",
                        external_company_id=external_id,
                        domain=source["domain"],
                        company_ids=sorted(candidate.id for candidate in unlinked_name_candidates if candidate.id is not None),
                    )
                    blocked_ids.add(external_id)
                    quarantine_company_ids.add(external_id)
                    continue
                else:
                    company = Company(
                        name=source["name"],
                        country=_text(source["raw"].get("country")) or None,
                        region=_source_region(source["raw"]) or None,
                        company_type=_text(source["raw"].get("company_type")) or None,
                        source=_text(source["raw"].get("source")) or "BDdb",
                        priority=_text(source["raw"].get("rating")) or "B",
                        status="blacklisted" if source["company_no_go"] else "new",
                    )
                    session.add(company)
                    session.flush()
                    companies_by_id[company.id] = company
                    match_method = "inserted"
                    inserted_company_this_run = True
                    counts["inserted_companies"] += 1

            if company is None or company.id is None:
                rejects.append({"reason": "unable_to_resolve_company", "external_company_id": external_id})
                continue
            link = CompanySourceLink(
                source_system=SOURCE_SYSTEM,
                external_company_id=external_id,
                company_id=company.id,
                source_domain=source["domain"] or None,
                match_method=match_method,
                last_seen_fingerprint=fingerprint,
                last_synced_at=now,
            )
            session.add(link)
            session.flush()
            links_by_external[external_id] = link
            links_by_company[company.id] = link
            counts["inserted_links"] += 1

        if company is None or company.id is None or link is None:
            continue

        owner_conflicts = [
            email
            for email in source["qualified"]
            if email in contacts_by_email and contacts_by_email[email].company_id != company.id
        ]
        if owner_conflicts:
            quarantine_company_ids.add(external_id)
            quarantine_emails.update(owner_conflicts)
            link.is_blocked = True
            link.updated_at = now
            session.add(link)
            for email in owner_conflicts:
                _conflict(
                    conflicts,
                    "workbench_email_owner_conflict",
                    external_company_id=external_id,
                    domain=source["domain"],
                    email=email,
                    existing_company_id=contacts_by_email[email].company_id,
                    incoming_company_id=company.id,
                    quarantine_scope="company_and_email",
                )
            continue
        company_changed = _apply_company_fields(company, source)
        session.add(company)
        if not inserted_company_this_run:
            counts["updated_companies" if company_changed else "noop_companies"] += 1
        link.source_domain = source["domain"] or link.source_domain
        link.last_seen_fingerprint = fingerprint
        link.last_synced_at = now
        link.is_blocked = False
        link.updated_at = now
        session.add(link)

        for email, candidate in sorted(source["qualified"].items()):
            if email in blocked_emails:
                continue
            contact = contacts_by_email.get(email)
            if contact is not None and contact.company_id != company.id:
                raise RuntimeError("preflight owner-conflict quarantine was bypassed")

            route_status = (
                "suppressed"
                if email in suppressed or email in bounced
                else "active" if candidate["ready"] else "inactive"
            )
            if contact is None:
                contact = Contact(
                    company_id=company.id,
                    full_name=candidate["full_name"],
                    position=candidate["position"],
                    email=email,
                    priority_contact_rank=candidate["rank"],
                    is_primary=candidate["is_primary"],
                    status=route_status,
                )
                session.add(contact)
                session.flush()
                contacts_by_email[email] = contact
                counts["inserted_contacts"] += 1
                contact_changed = True
            else:
                contact_changed = False
                desired = {
                    "full_name": candidate["full_name"],
                    "position": candidate["position"],
                    "priority_contact_rank": candidate["rank"],
                    "is_primary": candidate["is_primary"],
                }
                for field_name, incoming in desired.items():
                    if incoming is not None and getattr(contact, field_name) != incoming:
                        setattr(contact, field_name, incoming)
                        contact_changed = True
                current_contact_status = _text(contact.status).casefold()
                if (
                    current_contact_status not in CONTACT_PROTECTED_STATUSES
                    and email not in suppressed
                    and email not in bounced
                    and contact.status != route_status
                ):
                    contact.status = route_status
                    contact_changed = True
                if contact_changed:
                    contact.updated_at = now
                    session.add(contact)
                    counts["updated_contacts"] += 1
                else:
                    counts["noop_contacts"] += 1

            route_key = (contact.id, email)
            route = email_routes.get(route_key)
            if route is None:
                route = ContactRoute(
                        contact_id=contact.id,
                        route_type="email",
                        route_value=email,
                        source=f"{SOURCE_SYSTEM}:{external_id}",
                        is_primary=candidate["is_primary"],
                        status=route_status,
                    )
                session.add(route)
                email_routes[route_key] = route
                counts["inserted_routes"] += 1
            elif _text(route.status).casefold() not in CONTACT_PROTECTED_STATUSES and route.status != route_status:
                route.status = route_status
                route.updated_at = now
                session.add(route)

        for email in sorted(source["disqualified"]):
            contact = contacts_by_email.get(email)
            if contact is None or contact.company_id != company.id:
                continue
            if _text(contact.status).casefold() in CONTACT_PROTECTED_STATUSES:
                continue
            if contact.status != "inactive":
                contact.status = "inactive"
                contact.updated_at = now
                session.add(contact)
                counts["updated_contacts"] += 1
            route = email_routes.get((contact.id, email))
            if route is not None and _text(route.status).casefold() not in CONTACT_PROTECTED_STATUSES and route.status != "inactive":
                route.status = "inactive"
                route.updated_at = now
                session.add(route)

    counts["reject_count"] = len(rejects)
    counts["conflict_count"] = len(conflicts)
    counts["quarantine_company_count"] = len(quarantine_company_ids)
    counts["quarantine_email_count"] = len(quarantine_emails)
    counts["fatal_conflict_count"] = sum(
        item["conflict_type"] in FATAL_CONFLICT_TYPES for item in conflicts
    )
    return counts, conflicts, rejects


def _run_to_dict(run: BdMasterSyncRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "source_system": run.source_system,
        "source_path": run.source_path,
        "source_fingerprint": run.source_fingerprint,
        "source_mtime_ns": run.source_mtime_ns,
        "dry_run": run.dry_run,
        "status": run.status,
        "source_company_count": run.source_company_count,
        "inserted_companies": run.inserted_companies,
        "updated_companies": run.updated_companies,
        "noop_companies": run.noop_companies,
        "inserted_contacts": run.inserted_contacts,
        "updated_contacts": run.updated_contacts,
        "noop_contacts": run.noop_contacts,
        "inserted_routes": run.inserted_routes,
        "inserted_links": run.inserted_links,
        "reject_count": run.reject_count,
        "conflict_count": run.conflict_count,
        "quarantine_company_count": run.quarantine_company_count,
        "quarantine_email_count": run.quarantine_email_count,
        "fatal_conflict_count": run.fatal_conflict_count,
        "error": run.error_text,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "scheduler_safe": (
            run.status in SUCCESS_STATUSES
            and run.reject_count == 0
            and run.fatal_conflict_count == 0
        ),
    }


def _persist_failed_run(
    target_engine: Engine,
    run_id: str,
    source_path: Path | None,
    started_at: Any,
    error_text: str,
) -> dict[str, Any]:
    run = BdMasterSyncRun(
        id=run_id,
        source_path=str(source_path or ""),
        source_fingerprint="",
        dry_run=False,
        status="failed",
        error_text=error_text,
        started_at=started_at,
        finished_at=utc_now(),
    )
    with Session(target_engine) as session:
        session.add(run)
        session.commit()
        session.refresh(run)
    return _run_to_dict(run)


def run_bd_master_sync(
    target_engine: Engine,
    source_path: Path | None,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    run_id = str(uuid4())
    started_at = utc_now()
    try:
        data, fingerprint, source_mtime_ns = _load_source(source_path)
    except Exception as exc:
        if dry_run:
            return {
                "run_id": run_id,
                "status": "failed",
                "dry_run": True,
                "error": str(exc),
                "scheduler_safe": False,
            }
        return _persist_failed_run(target_engine, run_id, source_path, started_at, str(exc))

    if not dry_run and not force:
        with Session(target_engine) as session:
            previous = session.exec(
                select(BdMasterSyncRun)
                .where(
                    BdMasterSyncRun.source_system == SOURCE_SYSTEM,
                    BdMasterSyncRun.source_fingerprint == fingerprint,
                    BdMasterSyncRun.status.in_(["completed", "completed_with_conflicts"]),
                    BdMasterSyncRun.dry_run == False,
                )
                .order_by(BdMasterSyncRun.finished_at.desc())
            ).first()
            if previous is not None:
                run = BdMasterSyncRun(
                    id=run_id,
                    source_path=str(source_path),
                    source_fingerprint=fingerprint,
                    source_mtime_ns=source_mtime_ns,
                    dry_run=False,
                    status="unchanged",
                    source_company_count=previous.source_company_count,
                    noop_companies=previous.source_company_count,
                    conflict_count=previous.conflict_count,
                    quarantine_company_count=previous.quarantine_company_count,
                    quarantine_email_count=previous.quarantine_email_count,
                    fatal_conflict_count=previous.fatal_conflict_count,
                    started_at=started_at,
                    finished_at=utc_now(),
                    details_json=json.dumps({"previous_run_id": previous.id}, ensure_ascii=False),
                )
                session.add(run)
                session.commit()
                session.refresh(run)
                return _run_to_dict(run)

    try:
        prepared, source_company_count, initial_rejects = _prepare_source(data)
        with Session(target_engine) as session:
            counts, conflicts, rejects = _sync_prepared(session, prepared, fingerprint, initial_rejects)
            fatal_conflict_count = counts["fatal_conflict_count"]
            if fatal_conflict_count or counts["reject_count"]:
                status = "blocked"
            elif counts["conflict_count"]:
                status = "completed_with_conflicts"
            else:
                status = "completed"
            run_values = dict(
                id=run_id,
                source_path=str(source_path),
                source_fingerprint=fingerprint,
                source_mtime_ns=source_mtime_ns,
                dry_run=dry_run,
                status=status,
                source_company_count=source_company_count,
                inserted_companies=counts["inserted_companies"],
                updated_companies=counts["updated_companies"],
                noop_companies=counts["noop_companies"],
                inserted_contacts=counts["inserted_contacts"],
                updated_contacts=counts["updated_contacts"],
                noop_contacts=counts["noop_contacts"],
                inserted_routes=counts["inserted_routes"],
                inserted_links=counts["inserted_links"],
                reject_count=counts["reject_count"],
                conflict_count=counts["conflict_count"],
                quarantine_company_count=counts["quarantine_company_count"],
                quarantine_email_count=counts["quarantine_email_count"],
                fatal_conflict_count=fatal_conflict_count,
                details_json=json.dumps(
                    {
                        "rejects": rejects,
                        "business_changes_rolled_back": status == "blocked" or dry_run,
                    },
                    ensure_ascii=False,
                ),
                started_at=started_at,
                finished_at=utc_now(),
            )
            run = BdMasterSyncRun(**run_values)
            session.add(run)
            for item in conflicts:
                session.add(
                    BdMasterSyncConflict(
                        run_id=run_id,
                        conflict_type=item["conflict_type"],
                        external_company_id=item.get("external_company_id"),
                        domain=item.get("domain"),
                        email=item.get("email"),
                        status=(
                            "blocked"
                            if item["conflict_type"] in FATAL_CONFLICT_TYPES
                            else "quarantined"
                        ),
                        details_json=json.dumps(item.get("details") or {}, ensure_ascii=False),
                    )
                )
            session.flush()
            result = _run_to_dict(run)
            result["conflicts"] = conflicts
            result["rejects"] = rejects
            if dry_run:
                session.rollback()
            elif status == "blocked":
                session.rollback()
                with Session(target_engine) as audit_session:
                    persisted_run = BdMasterSyncRun(**run_values)
                    audit_session.add(persisted_run)
                    for item in conflicts:
                        audit_session.add(
                            BdMasterSyncConflict(
                                run_id=run_id,
                                conflict_type=item["conflict_type"],
                                external_company_id=item.get("external_company_id"),
                                domain=item.get("domain"),
                                email=item.get("email"),
                                status=(
                                    "blocked"
                                    if item["conflict_type"] in FATAL_CONFLICT_TYPES
                                    else "quarantined"
                                ),
                                details_json=json.dumps(item.get("details") or {}, ensure_ascii=False),
                            )
                        )
                    audit_session.commit()
            else:
                session.commit()
            return result
    except Exception as exc:
        if dry_run:
            return {
                "run_id": run_id,
                "status": "failed",
                "dry_run": True,
                "source_fingerprint": fingerprint,
                "error": str(exc),
                "scheduler_safe": False,
            }
        return _persist_failed_run(target_engine, run_id, source_path, started_at, str(exc))


def get_bd_master_sync_status(session: Session, source_path: Path | None) -> dict[str, Any]:
    last_run = session.exec(
        select(BdMasterSyncRun).order_by(BdMasterSyncRun.started_at.desc())
    ).first()
    link_count = session.exec(select(func.count()).select_from(CompanySourceLink)).one()
    blocked_link_count = session.exec(
        select(func.count()).select_from(CompanySourceLink).where(CompanySourceLink.is_blocked == True)
    ).one()
    unresolved_conflict_count = session.exec(
        select(func.count())
        .select_from(BdMasterSyncConflict)
        .where(BdMasterSyncConflict.status == "blocked")
    ).one()
    quarantined_conflict_count = session.exec(
        select(func.count())
        .select_from(BdMasterSyncConflict)
        .where(BdMasterSyncConflict.status == "quarantined")
    ).one()
    return {
        "configured_source_path": str(source_path or ""),
        "source_configured": source_path is not None,
        "source_readable": bool(source_path and source_path.is_file()),
        "company_source_link_count": link_count,
        "blocked_link_count": blocked_link_count,
        "blocked_conflict_count": unresolved_conflict_count,
        "quarantined_conflict_count": quarantined_conflict_count,
        "last_run": _run_to_dict(last_run) if last_run else None,
    }
