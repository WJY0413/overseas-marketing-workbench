from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session

from app.db import create_db_and_tables, engine
from app.services.bounce_scanner import bounce_log_path, scan_bounces


def main() -> int:
    create_db_and_tables()
    with Session(engine) as session:
        result = scan_bounces(session)
    print(
        "Bounce scan completed: "
        f"scanned={result['scanned_messages']} "
        f"detected={result['detected_bounces']} "
        f"suppressed={result['suppressed_emails']} "
        f"errors={result['errors']} "
        f"log={bounce_log_path()}"
    )
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
