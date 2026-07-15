from __future__ import annotations

import tempfile
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.mailer import _add_file_attachments, serialize_attachment_paths, validate_attachment_paths


def _attachment_parts(message: EmailMessage) -> list[EmailMessage]:
    return [
        part
        for part in message.walk()
        if part.get_content_disposition() == "attachment"
    ]


def main() -> None:
    message = EmailMessage()
    message["Subject"] = "attachment validation"
    message.set_content("text fallback")
    message.add_alternative("<p>html body</p>", subtype="html")

    before = len(_attachment_parts(message))
    assert before == 0, f"expected no initial attachments, got {before}"
    assert _add_file_attachments(message, None) == 0
    assert len(_attachment_parts(message)) == 0

    with tempfile.TemporaryDirectory() as tmp_dir:
        attachment_path = Path(tmp_dir) / "catalogue-test.pdf"
        attachment_path.write_bytes(b"%PDF-1.4\n% Workbench attachment validation\n")
        stored = serialize_attachment_paths(str(attachment_path))
        assert stored is not None
        assert validate_attachment_paths(stored) == []

        added = _add_file_attachments(message, stored)
        parts = _attachment_parts(message)
        assert added == 1, f"expected one added attachment, got {added}"
        assert len(parts) == 1, f"expected one MIME attachment, got {len(parts)}"
        assert parts[0].get_filename() == "catalogue-test.pdf"
        assert parts[0].get_content_type() == "application/pdf"

    print("attachment validation passed: no-attachment path unchanged; PDF uses MIME add_attachment")


if __name__ == "__main__":
    main()
