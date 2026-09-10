"""Local Workbench adapter for the authoritative LinkedIn SQLite master."""
from __future__ import annotations

import hashlib
import json
from threading import Lock
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WORKBENCH_CAMPAIGN_ID = "workbench_local_linkedin_v1"
WORKBENCH_TAG = "linkedin_connection_confirmed"
PRIORITY_CAMPAIGN_ID = "linkedin_person_priority_v3_20260818"
ACTIVE = {"connection_attempted", "invitation_sent", "connected", "message_sent"}
_BACKUP_LOCK = Lock()
CONNECTION_FIELDS = ("outreach_status", "last_connection_attempt_at", "suppressed_until", "suppression_reason", "suppression_policy")


class LinkedInConnectionError(RuntimeError):
    pass


def _now() -> datetime:
    try:
        return datetime.now(ZoneInfo("Asia/Shanghai")).replace(microsecond=0)
    except ZoneInfoNotFoundError:
        return datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise LinkedInConnectionError(f"LinkedIn master not found: {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _raw(row: sqlite3.Row) -> dict:
    return json.loads(row["raw_json"])


def _company_and_rating(connection: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    optional_tables = {"linkedin_company_domain_stage_person_links", "linkedin_company_domain_stage", "linkedin_company_domain_stage_ratings"}
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not optional_tables.issubset(tables):
        return result
    rows = connection.execute(
        """SELECT link.person_id, stage.canonical_company_name, rating.fit_rating
           FROM linkedin_company_domain_stage_person_links link
           JOIN linkedin_company_domain_stage stage ON stage.stage_company_id=link.stage_company_id
           LEFT JOIN linkedin_company_domain_stage_ratings rating ON rating.stage_company_id=stage.stage_company_id"""
    )
    for row in rows:
        if row["person_id"] not in result:
            result[row["person_id"]] = (row["canonical_company_name"] or "-", row["fit_rating"] or "-")
    return result


def _priority_tags(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Read the two production priority labels installed in the LinkedIn master."""
    result: dict[str, dict[str, object]] = {}
    try:
        rows = connection.execute(
        "SELECT person_id,tag FROM person_tags WHERE source_campaign_id=? AND active=1",
        (PRIORITY_CAMPAIGN_ID,),
        )
    except sqlite3.OperationalError as exc:
        raise LinkedInConnectionError(f"无法读取领英优先级数据：{exc}") from exc
    for row in rows:
        fields = str(row["tag"]).split(":")
        if len(fields) < 3:
            continue
        values = result.setdefault(row["person_id"], {})
        try:
            if fields[0] == "position_rank" and len(fields) == 4:
                values.update({"position_rank": fields[1], "position_score": int(fields[2]), "company_core_rank": 0 if fields[3] == "company_none" else int(fields[3].removeprefix("company_"))})
            elif fields[0] == "composite_rank":
                values.update({"composite_rank": fields[1], "composite_score": int(fields[2])})
        except ValueError:
            continue
    return result


def list_candidates(path: Path, *, query: str = "", batch_size: int = 10) -> list[dict]:
    if batch_size not in {10, 15, 20}:
        raise LinkedInConnectionError("batch size must be 10, 15, or 20")
    needle = query.strip().lower()
    connection = _connect(path)
    try:
        companies = _company_and_rating(connection)
        priorities = _priority_tags(connection)
        suppressed_people = {
            row[0]
            for row in connection.execute(
                "SELECT person_id FROM linkedin_connection_suppression_state WHERE suppression_status='active' AND datetime(suppressed_until)>datetime('now')"
            )
        }
        rows: list[dict] = []
        seen_people: set[str] = set()
        for row in connection.execute(
            """SELECT source_record_id,person_id,source_target,raw_name,raw_linkedin_profile,raw_json
               FROM person_source_records
               WHERE lower(trim(coalesce(json_extract(raw_json, '$.outreach_status'), ''))) NOT IN ('connection_attempted','invitation_sent','connected','message_sent')
                 AND coalesce(json_extract(raw_json, '$.last_connection_attempt_at'), '') = ''
               ORDER BY source_record_id"""
        ):
            raw = _raw(row)
            person_id = row["person_id"]
            priority = priorities.get(person_id, {})
            imported_draft = row["source_target"] == "workbench_greeting_import"
            if not imported_draft and (priority.get("position_rank") not in {"P1", "P2"} or priority.get("company_core_rank") != 1):
                continue
            profile = str(raw.get("linkedin_profile") or row["raw_linkedin_profile"] or "").strip()
            if person_id in seen_people or person_id in suppressed_people or "linkedin.com/in/" not in profile.lower():
                continue
            if str(raw.get("outreach_status") or "").strip().lower() in ACTIVE or raw.get("last_connection_attempt_at"):
                continue
            greeting = str(raw.get("greeting_note") or "").strip()
            if len(greeting) < 20 or greeting in {"-", "N/A"}:
                continue
            company, rating = companies.get(person_id, (str(raw.get("resolved_company") or raw.get("company_name") or raw.get("current_company") or "-").strip() or "-", "-"))
            item = {
                "source_record_id": row["source_record_id"], "person_id": person_id,
                "name": str(raw.get("name") or row["raw_name"] or "-").strip() or "-", "position": str(raw.get("identity") or raw.get("role_title") or raw.get("role_value") or "-").strip() or "-",
                "source": str(raw.get("source_target") or row["source_target"] or "-").strip() or "-",
                "company": company, "rating": rating, "linkedin_profile": profile,
                "position_rank": priority.get("position_rank", "待审核"), "position_score": priority.get("position_score", 0),
                "composite_rank": priority.get("composite_rank", "待审核"), "composite_score": priority.get("composite_score", 0),
                "greeting": greeting,
            }
            searchable = " ".join(str(item[key]).lower() for key in ("name", "source", "company"))
            if needle and needle not in searchable:
                continue
            rows.append(item)
            seen_people.add(person_id)
        rows.sort(key=lambda item: (-item["composite_score"], -item["position_score"], item["source_record_id"]))
        return rows[:batch_size]
    finally:
        connection.close()


def _backup(path: Path, backup_dir: Path) -> Path:
    """One consistent baseline per database/day; per-record events support undo."""
    identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
    target = backup_dir / f"linkedin_master_{identity}_{_now():%Y%m%d}.sqlite"
    with _BACKUP_LOCK:
        if target.is_file():
            return target
        backup_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LinkedInConnectionError("LinkedIn backup validation failed")
        finally:
            destination.close()
            source.close()
        temporary.replace(target)
    return target


def _campaign(connection: sqlite3.Connection, now: str) -> None:
    connection.execute(
        """INSERT INTO linkedin_connection_campaigns(
             campaign_id,campaign_name,owner,purpose,source_path,email_nogo_scope,linkedin_policy,
             monitoring_cadence,campaign_status,created_at,updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(campaign_id) DO UPDATE SET updated_at=excluded.updated_at""",
        (WORKBENCH_CAMPAIGN_ID, "Workbench local LinkedIn confirmations", "operator",
         "Local operator-confirmed LinkedIn connection requests; no browser automation.",
         "bd-email-workbench-lite", "none", "manual_confirm_only", "none", "active", now, now),
    )


def confirm(path: Path, source_record_ids: list[str], *, mode: str, backup_dir: Path) -> dict:
    ids = [value.strip() for value in source_record_ids if value.strip()]
    if not ids or len(ids) != len(set(ids)) or mode not in {"single", "batch"}:
        raise LinkedInConnectionError("invalid confirmation selection")
    _backup(path, backup_dir)
    connection = _connect(path)
    now = _now(); now_text = _iso(now); batch_id = f"wb_{uuid.uuid4().hex}"
    try:
        connection.execute("BEGIN IMMEDIATE"); _campaign(connection, now_text)
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(f"SELECT source_record_id,person_id,raw_json FROM person_source_records WHERE source_record_id IN ({placeholders})", ids).fetchall()
        if len(rows) != len(ids): raise LinkedInConnectionError("one or more source records are missing")
        by_id = {row["source_record_id"]: row for row in rows}
        for source_record_id in ids:
            raw = _raw(by_id[source_record_id])
            if str(raw.get("outreach_status") or "").lower() in ACTIVE or raw.get("last_connection_attempt_at"):
                raise LinkedInConnectionError(f"already marked: {source_record_id}")
        changed=[]
        try:
            until = now.replace(year=now.year + 99)
        except ValueError:  # February 29 in a non-leap target year.
            until = now.replace(year=now.year + 99, day=28)
        policy="connection_hold_99_years"
        for source_record_id in ids:
            row=by_id[source_record_id]; raw=_raw(row); snapshot=dict(raw)
            contact_id=str(raw.get("contact_id") or source_record_id)
            raw.update({"outreach_status":"connection_attempted","last_connection_attempt_at":now_text,"suppressed_until":_iso(until),"suppression_reason":"Workbench operator-confirmed connection request","suppression_policy":policy})
            raw_json=json.dumps(raw,ensure_ascii=False,separators=(",",":"))
            connection.execute("UPDATE person_source_records SET raw_json=?,record_sha256=? WHERE source_record_id=?",(raw_json,_hash(raw_json),source_record_id))
            connection.execute("""INSERT INTO linkedin_connection_suppression_state(source_record_id,person_id,contact_id,suppression_status,outreach_status,marked_at,suppressed_until,policy_code,policy_years,reason,created_at,updated_at)
                VALUES (?,?,?,'active','connection_attempted',?,?,?,?,?,?,?)
                ON CONFLICT(source_record_id) DO UPDATE SET suppression_status='active',outreach_status='connection_attempted',marked_at=excluded.marked_at,suppressed_until=excluded.suppressed_until,policy_code=excluded.policy_code,policy_years=excluded.policy_years,reason=excluded.reason,updated_at=excluded.updated_at""",(source_record_id,row["person_id"],contact_id,now_text,_iso(until),policy,99,"Workbench operator-confirmed connection request",now_text,now_text))
            details={"origin":"workbench","batch_id":batch_id,"confirmation_mode":mode,"pre_confirm_raw_json":snapshot}
            connection.execute("INSERT INTO linkedin_connection_suppression_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(f"lcs_{uuid.uuid4().hex}",source_record_id,row["person_id"],contact_id,"connection_marked",now_text,"operator",None,_iso(until),policy,99,"Workbench operator-confirmed connection request",json.dumps(details,ensure_ascii=False),now_text))
            connection.execute("INSERT INTO person_tags(person_id,tag,source_campaign_id,active,applied_at,removed_at) VALUES (?,?,?,1,?,NULL) ON CONFLICT(person_id,tag,source_campaign_id) DO UPDATE SET active=1,applied_at=excluded.applied_at,removed_at=NULL",(row["person_id"],WORKBENCH_TAG,WORKBENCH_CAMPAIGN_ID,now_text))
            changed.append(source_record_id)
        connection.commit(); return {"changed":changed,"batch_id":batch_id,"backup":str(backup_dir)}
    except Exception:
        connection.rollback(); raise
    finally: connection.close()


def today_metrics(path: Path) -> dict:
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    people=set(); batches=set(); connection=_connect(path)
    try:
        for row in connection.execute(
            """SELECT event.person_id,event.event_at,event.details_json
               FROM linkedin_connection_suppression_events event
               JOIN linkedin_connection_suppression_state state ON state.source_record_id=event.source_record_id
               WHERE event.event_type='connection_marked' AND state.suppression_status='active'
                 AND event.rowid=(SELECT max(latest.rowid) FROM linkedin_connection_suppression_events latest
                                  WHERE latest.source_record_id=event.source_record_id AND latest.event_type='connection_marked')
                 AND datetime(event.event_at)>=datetime(?) AND datetime(event.event_at)<datetime(?)""",
            (_iso(start), _iso(end)),
        ):
            details=json.loads(row["details_json"])
            if details.get("origin") == "workbench": people.add(row["person_id"]); batches.add(details.get("batch_id"))
        return {"people":len(people),"batches":len(batches)}
    finally: connection.close()


def recent_workbench_confirmations(path: Path, *, limit: int = 50) -> list[dict]:
    connection = _connect(path)
    try:
        result=[]
        for row in connection.execute("""SELECT event.source_record_id,event.person_id,event.event_at,event.details_json,source.raw_json,source.raw_name,source.source_target,source.raw_linkedin_profile
            FROM linkedin_connection_suppression_events event JOIN person_source_records source ON source.source_record_id=event.source_record_id
            JOIN linkedin_connection_suppression_state state ON state.source_record_id=event.source_record_id
            WHERE event.event_type='connection_marked' AND state.suppression_status='active'
              AND event.rowid=(SELECT max(latest.rowid) FROM linkedin_connection_suppression_events latest
                               WHERE latest.source_record_id=event.source_record_id AND latest.event_type='connection_marked')
            ORDER BY event.rowid DESC LIMIT ?""", (limit,)):
            details=json.loads(row["details_json"])
            if details.get("origin") != "workbench": continue
            raw=_raw(row)
            result.append({"source_record_id":row["source_record_id"],"name":raw.get("name") or row["raw_name"] or "-","source":raw.get("source_target") or row["source_target"] or "-","company":raw.get("resolved_company") or raw.get("company_name") or raw.get("current_company") or "-","linkedin_profile":raw.get("linkedin_profile") or row["raw_linkedin_profile"] or "","confirmed_at":row["event_at"]})
        return result
    finally: connection.close()


def _library_page(path: Path, *, query: str, page: int, page_size: int, confirmed_only: bool) -> dict[str, object]:
    if page_size != 10:
        raise LinkedInConnectionError("page size must be 10")
    connection = _connect(path)
    connection.create_collation("CASEFOLD", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()))
    connection.create_function("text_lower", 1, lambda value: str(value or "").lower())
    # Project only the display fields in SQL; fetch/parse no full raw JSON rows.
    fields = {
        "name": "coalesce(nullif(json_extract(raw_json,'$.name'),''), nullif(raw_name,''), '-')",
        "position": "coalesce(nullif(json_extract(raw_json,'$.identity'),''), nullif(json_extract(raw_json,'$.role_title'),''), nullif(json_extract(raw_json,'$.role_value'),''), '-')",
        "company": "coalesce(nullif(json_extract(raw_json,'$.resolved_company'),''), nullif(json_extract(raw_json,'$.company_name'),''), nullif(json_extract(raw_json,'$.current_company'),''), '-')",
        "greeting": "coalesce(nullif(json_extract(raw_json,'$.greeting_note'),''), '-')",
        "linkedin_profile": "coalesce(nullif(json_extract(raw_json,'$.linkedin_profile'),''), nullif(raw_linkedin_profile,''), '')",
        "source": "coalesce(nullif(json_extract(raw_json,'$.source_target'),''), nullif(source_target,''), '-')",
        "status": "text_lower(trim(coalesce(json_extract(raw_json,'$.outreach_status'), '')))"
    }
    projection = ", ".join(f"trim({value}) AS {key}" for key, value in fields.items())
    cte = f"WITH records AS (SELECT source_record_id, {projection} FROM person_source_records) "
    filters = []; params = []
    if confirmed_only:
        filters.append("status IN ('connection_attempted','invitation_sent','connected','message_sent')")
    needle = query.strip().lower()
    if needle:
        search_fields = "name || ' ' || position || ' ' || company"
        if not confirmed_only:
            search_fields += " || ' ' || source || ' ' || coalesce(nullif(status,''),'待建联')"
        filters.append(f"instr(text_lower({search_fields}), ?) > 0")
        params.append(needle)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    try:
        total = connection.execute(cte + "SELECT count(*) FROM records" + where, params).fetchone()[0]
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(max(page, 1), page_count)
        rows = connection.execute(cte + "SELECT * FROM records" + where + " ORDER BY company COLLATE CASEFOLD, name COLLATE CASEFOLD, source_record_id LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size]).fetchall()
        records = [dict(row) for row in rows]
        for record in records:
            event = connection.execute("""SELECT event.details_json
                FROM linkedin_connection_suppression_events event
                JOIN linkedin_connection_suppression_state state ON state.source_record_id=event.source_record_id
                WHERE event.source_record_id=? AND event.event_type='connection_marked'
                  AND state.suppression_status='active' ORDER BY event.rowid DESC LIMIT 1""",
                (record["source_record_id"],)).fetchone()
            details = json.loads(event["details_json"]) if event else {}
            record["can_restore"] = details.get("origin") == "workbench" and isinstance(details.get("pre_confirm_raw_json"), dict)
        for record in records:
            for key in ("name", "position", "company", "greeting", "source"):
                record[key] = record[key] or "-"
            record["status"] = record["status"] or "待建联"
        return {"total": total, "page": page, "page_count": page_count, "records": records}
    except sqlite3.DatabaseError as exc:
        raise LinkedInConnectionError(f"无法读取领英联系人：{exc}") from exc
    finally:
        connection.close()


def confirmed_page(path: Path, *, query: str = "", page: int = 1, page_size: int = 10) -> dict[str, object]:
    return _library_page(path, query=query, page=page, page_size=page_size, confirmed_only=True)


def master_library_page(path: Path, *, query: str = "", page: int = 1, page_size: int = 10) -> dict[str, object]:
    return _library_page(path, query=query, page=page, page_size=page_size, confirmed_only=False)


def restore_mistag(path: Path, source_record_id: str) -> None:
    _backup(path, path.parent / "linkedin_backups")
    connection=_connect(path); now_text=_iso(_now())
    try:
        connection.execute("BEGIN IMMEDIATE")
        row=connection.execute("""SELECT event.person_id,event.contact_id,event.details_json,source.raw_json FROM linkedin_connection_suppression_events event
            JOIN person_source_records source ON source.source_record_id=event.source_record_id
            JOIN linkedin_connection_suppression_state state ON state.source_record_id=event.source_record_id
            WHERE event.source_record_id=? AND event.event_type='connection_marked' AND state.suppression_status='active' ORDER BY event.rowid DESC LIMIT 1""",(source_record_id,)).fetchone()
        if not row: raise LinkedInConnectionError("This record cannot be restored; only Workbench-created active confirmations are reversible.")
        details=json.loads(row["details_json"]); snapshot=details.get("pre_confirm_raw_json")
        if details.get("origin") != "workbench" or not isinstance(snapshot,dict): raise LinkedInConnectionError("No safe pre-confirmation snapshot is available.")
        current = _raw(row)
        for field in CONNECTION_FIELDS:
            if field in snapshot:
                current[field] = snapshot[field]
            else:
                current.pop(field, None)
        raw_json=json.dumps(current,ensure_ascii=False,separators=(",",":"))
        connection.execute("UPDATE person_source_records SET raw_json=?,record_sha256=? WHERE source_record_id=?",(raw_json,_hash(raw_json),source_record_id))
        connection.execute("UPDATE linkedin_connection_suppression_state SET suppression_status='released',released_at=?,released_by='operator',release_reason='Workbench mistag restored',updated_at=? WHERE source_record_id=?",(now_text,now_text,source_record_id))
        connection.execute("INSERT INTO linkedin_connection_suppression_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(f"lcs_{uuid.uuid4().hex}",source_record_id,row["person_id"],row["contact_id"],"released",now_text,"operator",None,None,None,None,"Workbench mistag restored",json.dumps({"origin":"workbench","restored_event":True,"original_batch_id":details.get("batch_id")}),now_text))
        connection.execute("UPDATE person_tags SET active=0,removed_at=? WHERE person_id=? AND tag=? AND source_campaign_id=?",(now_text,row["person_id"],WORKBENCH_TAG,WORKBENCH_CAMPAIGN_ID))
        connection.commit()
    except Exception:
        connection.rollback(); raise
    finally: connection.close()
