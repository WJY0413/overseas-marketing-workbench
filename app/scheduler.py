import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.config import get_settings
from app.db import engine
from app.services.power_awake import sync_power_awake
from app.services.queue import process_due_queue, process_next_queue_handoff_batch, scan_followups
from app.services.bounce_scanner import scan_bounces

scheduler = BackgroundScheduler(timezone=get_settings().app_timezone)


def _process_queue_job() -> None:
    with Session(engine) as session:
        # A 50-row intake chunk is short and resumable. It shares the queue
        # mutation gate with sending, so a second writer waits/readbacks rather
        # than replaying a timed-out bulk request.
        try:
            process_next_queue_handoff_batch(session)
        except Exception:
            session.rollback()
            logging.getLogger(__name__).exception("Queue intake failed; existing queue remains independently processable")
        if not get_settings().dry_run_email:
            process_due_queue(session)
        sync_power_awake(session)


def _scan_followups_job() -> None:
    with Session(engine) as session:
        scan_followups(session)


def _scan_bounces_job() -> None:
    if not get_settings().bounce_scan_enabled:
        return
    with Session(engine) as session:
        scan_bounces(session)


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(_process_queue_job, "interval", seconds=1, id="process_queue", replace_existing=True)
    scheduler.add_job(_scan_followups_job, "interval", hours=1, id="scan_followups", replace_existing=True)
    scheduler.add_job(
        _scan_bounces_job,
        "interval",
        minutes=max(get_settings().bounce_scan_interval_minutes, 5),
        id="scan_bounces",
        replace_existing=True,
    )
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
