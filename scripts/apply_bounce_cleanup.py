from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session, select

from app.db import create_db_and_tables, engine
from app.models import SenderAccount
from app.services.bounce_scanner import cleanup_bounced_contacts, scan_sender_bounces


def main() -> int:
    create_db_and_tables()
    totals = {
        "scanned_messages": 0,
        "detected_bounces": 0,
        "suppressed_by_scan": 0,
        "scan_errors": 0,
    }
    with Session(engine) as session:
        senders = session.exec(select(SenderAccount).where(SenderAccount.is_active == True)).all()
        for sender in senders:
            result = scan_sender_bounces(session, sender, lookback_days=0)
            totals["scanned_messages"] += result.scanned_messages
            totals["detected_bounces"] += result.detected_bounces
            totals["suppressed_by_scan"] += result.suppressed_emails
            totals["scan_errors"] += result.errors
        totals.update(cleanup_bounced_contacts(session))

    print("Bounce cleanup completed")
    for key in sorted(totals):
        print(f"{key}={totals[key]}")
    return 1 if totals["scan_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
