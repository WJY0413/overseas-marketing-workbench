from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.services.email_quality import validate_contact_email  # noqa: E402


CASES = [
    ("example@gmail.com", "invalid", "placeholder/test"),
    ("example+lead@gmail.com", "invalid", "placeholder/test"),
    ("test@outlook.com", "invalid", "placeholder/test"),
    ("sales@example.com", "invalid", "reserved or test email domain"),
    ("john.doe@gmail.com", "valid", "email syntax is valid"),
]


def main() -> int:
    failures = []
    for email, expected_status, reason_text in CASES:
        result = validate_contact_email(email, check_deliverability=False)
        reason = result.reason.lower()
        passed = result.status == expected_status and reason_text in reason
        marker = "PASS" if passed else "FAIL"
        print(f"{marker} {email} -> {result.status}: {result.reason}")
        if not passed:
            failures.append((email, expected_status, reason_text, result))
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
