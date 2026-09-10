#!/usr/bin/env python3
"""Fail-closed pacing and daily quota guard for linkedin-mining-v3.

This is a load-safety control, not a stealth mechanism.  It deliberately uses
fixed conservative limits, persists them across tasks, and refuses to accept
looser values from the command line.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 fallback
    ZoneInfo = None  # type: ignore[assignment]


STATE_VERSION = 1
LOCAL_TZ_NAME = "Asia/Shanghai"
DEFAULT_MIN_PAGE_INTERVAL_SECONDS = 25
DEFAULT_SESSION_CARD_CAP = 1000
DEFAULT_SESSION_PAGE_CAP = 100
DEFAULT_DAILY_CARD_CAP = 1000
DEFAULT_DAILY_PAGE_CAP = 100
DEFAULT_DAILY_SESSION_CAP = 2
DEFAULT_PAGE_CARD_RESERVATION = 10
MAX_PAGE_CARD_RESERVATION = 25
LOCK_WAIT_SECONDS = 30
STALE_LOCK_SECONDS = 300


class GuardError(RuntimeError):
    """A deliberate fail-closed guard rejection."""


def _local_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(LOCAL_TZ_NAME)
        except Exception:
            pass
    return timezone(timedelta(hours=8))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _today() -> str:
    return _now_utc().astimezone(_local_tz()).date().isoformat()


def _initial_state(day: str) -> Dict[str, Any]:
    return {
        "schema_version": STATE_VERSION,
        "timezone": LOCAL_TZ_NAME,
        "day": day,
        "cards_captured": 0,
        "pages_captured": 0,
        "sessions_started": 0,
        "last_page_started_at_utc": None,
        "active_task": None,
        "quota_reached": False,
        "history": {},
    }


def _state_path(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else (Path.cwd() / path)


def _task_key(raw: str) -> str:
    return str(Path(raw).resolve())


def _rotate_day(state: Dict[str, Any]) -> Dict[str, Any]:
    day = _today()
    if state.get("day") == day:
        return state
    old_day = str(state.get("day", "unknown"))
    history = state.get("history") if isinstance(state.get("history"), dict) else {}
    history[old_day] = {
        "cards_captured": int(state.get("cards_captured", 0)),
        "pages_captured": int(state.get("pages_captured", 0)),
        "sessions_started": int(state.get("sessions_started", 0)),
    }
    # Retain a bounded audit trail while ensuring a broken active session
    # cannot silently carry a reservation across a natural-day boundary.
    recent_history = dict(sorted(history.items())[-30:])
    fresh = _initial_state(day)
    fresh["history"] = recent_history
    return fresh


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _initial_state(_today())
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GuardError(f"guard state is unreadable; refusing to continue: {path}: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != STATE_VERSION:
        raise GuardError(f"guard state schema is invalid; refusing to continue: {path}")
    return _rotate_day(state)


def _write_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialize guard updates with a small cross-process lock file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} at={_now_iso()}\n".encode("utf-8"))
        except FileExistsError:
            try:
                stale = (time.time() - lock_path.stat().st_mtime) > STALE_LOCK_SECONDS
            except FileNotFoundError:
                stale = False
            if stale:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise GuardError(f"another LinkedIn mining run holds the guard lock: {lock_path}")
            time.sleep(0.25)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _limits() -> Dict[str, int]:
    return {
        "min_page_interval_seconds": DEFAULT_MIN_PAGE_INTERVAL_SECONDS,
        "session_card_cap": DEFAULT_SESSION_CARD_CAP,
        "session_page_cap": DEFAULT_SESSION_PAGE_CAP,
        "daily_card_cap": DEFAULT_DAILY_CARD_CAP,
        "daily_page_cap": DEFAULT_DAILY_PAGE_CAP,
        "daily_session_cap": DEFAULT_DAILY_SESSION_CAP,
        "page_card_reservation": DEFAULT_PAGE_CARD_RESERVATION,
    }


def _active(state: Dict[str, Any], task_dir: str) -> Dict[str, Any]:
    active = state.get("active_task")
    if not isinstance(active, dict) or active.get("task_dir") != _task_key(task_dir):
        raise GuardError("this task does not own the mining guard; start or recover the exact task first")
    return active


def _emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def cmd_start(args: argparse.Namespace) -> None:
    path = _state_path(args.state_file)
    task = _task_key(args.task_dir)
    next_page = int(args.next_page)
    if next_page < 1:
        raise GuardError("next_page must be >= 1")
    with _locked(path):
        state = _load_state(path)
        active = state.get("active_task")
        if active is not None:
            if active.get("task_dir") == task and active.get("in_flight") is None:
                if int(active.get("expected_page", -1)) == next_page:
                    active.setdefault("session_cards_captured", 0)
                    active.setdefault("session_pages_captured", 0)
                    _write_state(path, state)
                    _emit({"status": "already_started", "day": state["day"], "limits": _limits(), "task_dir": task})
                    return
            raise GuardError("another task or an unrecovered page already owns the guard")
        limits = _limits()
        if int(state.get("sessions_started", 0)) >= limits["daily_session_cap"]:
            raise GuardError("daily mining session cap reached; resume on the next natural day")
        state["sessions_started"] = int(state.get("sessions_started", 0)) + 1
        state["active_task"] = {
            "task_dir": task,
            "expected_page": next_page,
            "in_flight": None,
            "session_cards_captured": 0,
            "session_pages_captured": 0,
        }
        state["quota_reached"] = False
        _write_state(path, state)
        _emit({"status": "started", "day": state["day"], "limits": limits, "task_dir": task})


def cmd_before_page(args: argparse.Namespace) -> None:
    path = _state_path(args.state_file)
    task = _task_key(args.task_dir)
    page = int(args.page)
    reserve = int(args.reserve_cards)
    if page < 1:
        raise GuardError("page must be >= 1")
    if reserve < 1 or reserve > MAX_PAGE_CARD_RESERVATION:
        raise GuardError(f"reserve_cards must be between 1 and {MAX_PAGE_CARD_RESERVATION}")
    waited = 0.0
    while True:
        with _locked(path):
            state = _load_state(path)
            active = _active(state, task)
            if active.get("in_flight") is not None:
                raise GuardError("a page reservation is still in flight; inspect the capture and run recover/cancel")
            expected = int(active.get("expected_page", -1))
            if expected != page:
                raise GuardError(f"page order mismatch: guard expects page {expected}, received {page}")
            limits = _limits()
            if int(active.get("session_pages_captured", 0)) + 1 > limits["session_page_cap"]:
                raise GuardError("single-run page cap reached; checkpoint and start a later run")
            if int(active.get("session_cards_captured", 0)) + reserve > limits["session_card_cap"]:
                raise GuardError("single-run card cap would be exceeded by the next page")
            if int(state.get("pages_captured", 0)) + 1 > limits["daily_page_cap"]:
                raise GuardError("daily page cap reached; stop this run and resume on the next natural day")
            if int(state.get("cards_captured", 0)) + reserve > limits["daily_card_cap"]:
                raise GuardError("daily card cap would be exceeded by the next page; stop and resume tomorrow")
            last = state.get("last_page_started_at_utc")
            wait_for = 0.0
            if last:
                try:
                    last_dt = datetime.fromisoformat(str(last))
                    wait_for = max(0.0, limits["min_page_interval_seconds"] - (_now_utc() - last_dt).total_seconds())
                except Exception as exc:
                    raise GuardError("guard timestamp is invalid; refusing to continue") from exc
            if wait_for <= 0:
                active["in_flight"] = {"page": page, "reserve_cards": reserve, "started_at_utc": _now_iso()}
                state["last_page_started_at_utc"] = active["in_flight"]["started_at_utc"]
                _write_state(path, state)
                _emit({"status": "page_permitted", "page": page, "waited_seconds": round(waited, 3), "limits": limits})
                return
        time.sleep(wait_for)
        waited += wait_for


def cmd_commit_page(args: argparse.Namespace) -> None:
    path = _state_path(args.state_file)
    task = _task_key(args.task_dir)
    page = int(args.page)
    cards = int(args.cards)
    terminal = bool(args.terminal)
    if cards < 0:
        raise GuardError("cards must be >= 0")
    with _locked(path):
        state = _load_state(path)
        active = _active(state, task)
        flight = active.get("in_flight")
        if not isinstance(flight, dict) or int(flight.get("page", -1)) != page:
            raise GuardError("no matching page reservation to commit")
        limits = _limits()
        new_cards = int(state.get("cards_captured", 0)) + cards
        new_pages = int(state.get("pages_captured", 0)) + 1
        new_session_cards = int(active.get("session_cards_captured", 0)) + cards
        new_session_pages = int(active.get("session_pages_captured", 0)) + 1
        if new_session_pages > limits["session_page_cap"]:
            raise GuardError("page commit would exceed the single-run page cap")
        if new_session_cards > limits["session_card_cap"]:
            raise GuardError("page commit would exceed the single-run card cap; raw capture remains for review")
        if new_pages > limits["daily_page_cap"]:
            raise GuardError("page commit would exceed the daily page cap")
        if new_cards > limits["daily_card_cap"]:
            raise GuardError("page commit would exceed the daily card cap; raw capture remains for review")
        state["cards_captured"] = new_cards
        state["pages_captured"] = new_pages
        active["session_cards_captured"] = new_session_cards
        active["session_pages_captured"] = new_session_pages
        active["expected_page"] = page + 1
        active["in_flight"] = None
        if terminal:
            state["terminal_page"] = page
        state["quota_reached"] = (
            new_session_cards >= limits["session_card_cap"]
            or new_session_pages >= limits["session_page_cap"]
            or new_cards >= limits["daily_card_cap"]
            or new_pages >= limits["daily_page_cap"]
        )
        _write_state(path, state)
        _emit({
            "status": "page_committed",
            "page": page,
            "cards": cards,
            "terminal": terminal,
            "quota_reached": state["quota_reached"],
            "session_pages_captured": new_session_pages,
            "session_cards_captured": new_session_cards,
        })


def cmd_cancel_page(args: argparse.Namespace) -> None:
    path = _state_path(args.state_file)
    task = _task_key(args.task_dir)
    page = int(args.page)
    with _locked(path):
        state = _load_state(path)
        active = _active(state, task)
        flight = active.get("in_flight")
        if not isinstance(flight, dict) or int(flight.get("page", -1)) != page:
            raise GuardError("no matching page reservation to cancel")
        active["in_flight"] = None
        _write_state(path, state)
        _emit({"status": "page_cancelled", "page": page})


def cmd_recover(args: argparse.Namespace) -> None:
    path = _state_path(args.state_file)
    task = _task_key(args.task_dir)
    with _locked(path):
        state = _load_state(path)
        active = _active(state, task)
        active["in_flight"] = None
        if args.next_page is not None:
            next_page = int(args.next_page)
            if next_page < 1:
                raise GuardError("next_page must be >= 1")
            active["expected_page"] = next_page
        _write_state(path, state)
        _emit({"status": "recovered", "task_dir": task, "expected_page": active["expected_page"]})


def cmd_finish(args: argparse.Namespace) -> None:
    path = _state_path(args.state_file)
    task = _task_key(args.task_dir)
    with _locked(path):
        state = _load_state(path)
        active = _active(state, task)
        if active.get("in_flight") is not None:
            raise GuardError("cannot finish while a page reservation is in flight")
        session_pages = int(active.get("session_pages_captured", 0))
        session_cards = int(active.get("session_cards_captured", 0))
        state["active_task"] = None
        _write_state(path, state)
        _emit({
            "status": "finished",
            "day": state["day"],
            "session_cards_captured": session_cards,
            "session_pages_captured": session_pages,
            "cards_captured": state["cards_captured"],
            "pages_captured": state["pages_captured"],
            "quota_reached": state["quota_reached"],
        })


def cmd_status(args: argparse.Namespace) -> None:
    path = _state_path(args.state_file)
    with _locked(path):
        state = _load_state(path)
        _write_state(path, state)
        _emit({"status": "ok", "state_file": str(path), "state": state, "limits": _limits()})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed LinkedIn pacing and daily quota guard")
    parser.add_argument("--state-file", default="output/linkedin_mining_guard_state.json")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--next-page", required=True, type=int)
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("before-page")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--page", required=True, type=int)
    p.add_argument("--reserve-cards", type=int, default=DEFAULT_PAGE_CARD_RESERVATION)
    p.set_defaults(func=cmd_before_page)

    p = sub.add_parser("commit-page")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--page", required=True, type=int)
    p.add_argument("--cards", required=True, type=int)
    p.add_argument("--terminal", action="store_true")
    p.set_defaults(func=cmd_commit_page)

    p = sub.add_parser("cancel-page")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--page", required=True, type=int)
    p.set_defaults(func=cmd_cancel_page)

    p = sub.add_parser("recover")
    p.add_argument("--task-dir", required=True)
    p.add_argument("--next-page", type=int)
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("finish")
    p.add_argument("--task-dir", required=True)
    p.set_defaults(func=cmd_finish)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        args.func(args)
        return 0
    except GuardError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
