"""5.05 regressions: temporary SQLite, synthetic contacts, fake SMTP only.
Run from the repository root:
    python -m unittest discover -s scripts -p test_queue_safety.py -v
No application startup, scheduler, real mailbox, or production database is used.
"""
import json
import socket
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings

# Patch settings BEFORE importing app.db/main; never read a workspace .env.
_MODULE_TMP = tempfile.TemporaryDirectory(prefix="workbench-505-tests-")
_SETTINGS = Settings(
    _env_file=None, database_url="sqlite://",
    workbench_data_dir=_MODULE_TMP.name, dry_run_email=False,
    bd_database_sqlite_path=None, bd_database_json_path=None,
    linkedin_master_sqlite_path=str(Path(_MODULE_TMP.name) / "unused.sqlite"),
    bounce_scan_enabled=False, enable_open_tracking=False,
)
_SETTINGS_PATCH = patch("app.config.get_settings", return_value=_SETTINGS)
_SETTINGS_PATCH.start()
from sqlmodel import SQLModel, Session, create_engine, select
from app import main
from app.models import Company, Contact, EmailDraft, EmailEvent, SenderAccount, Suppression
from app.services import queue, mailer
from app.time_utils import utc_now


def tearDownModule():
    _SETTINGS_PATCH.stop()
    _MODULE_TMP.cleanup()


class QueueSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="queue-safety-")
        self.addCleanup(self.temp.cleanup)
        self.engine = create_engine(
            "sqlite:///" + str(Path(self.temp.name) / "synthetic.sqlite"),
            connect_args={"check_same_thread": False, "timeout": 5},
        )
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        self.sent_messages = []
        self.login_hook = lambda: None
        self.delivery_error = None
        owner = self

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def starttls(self, **kwargs):
                pass
            def login(self, *args):
                owner.login_hook()
            def send_message(self, message):
                owner.sent_messages.append(message)
                if owner.delivery_error:
                    raise owner.delivery_error
                return {}

        for target, kwargs in (
            ("socket.socket.connect", {"side_effect": AssertionError("Network forbidden")}),
            ("subprocess.run", {"side_effect": AssertionError("External process forbidden")}),
            ("app.services.queue.validate_contact_email", {"return_value": SimpleNamespace(is_blocking=False)}),
            ("app.main.validate_contact_email", {"return_value": SimpleNamespace(is_blocking=False, normalized_email="changed@example.test")}),
            ("app.services.queue._inside_sender_window", {"return_value": True}),
            ("app.services.mailer.decrypt_secret", {"return_value": "synthetic-password"}),
            ("app.services.mailer.smtplib.SMTP", {"new": FakeSMTP}),
            ("app.services.mailer.smtplib.SMTP_SSL", {"new": FakeSMTP}),
            ("app.main.sync_power_awake", {"return_value": None}),
        ):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        with Session(self.engine) as session:
            company = Company(name="Synthetic Company", domain="example.test")
            session.add(company)
            session.flush()
            contact = Contact(company_id=company.id, email="to@example.test", full_name="Synthetic Contact")
            sender = SenderAccount(name="Synthetic Sender", email="sender@example.test",
                                   smtp_host="smtp.invalid", smtp_password_encrypted="fake")
            session.add(contact)
            session.add(sender)
            session.flush()
            draft = EmailDraft(company_id=company.id, contact_id=contact.id,
                               subject="Synthetic subject", body_html="<p>Synthetic body</p>",
                               body_text="Synthetic body", status="approved",
                               cc_emails="cc@example.test", template_snapshot="{}")
            session.add(draft)
            session.commit()
            self.draft_id, self.contact_id, self.sender_id = draft.id, contact.id, sender.id

    def enqueue(self):
        with Session(self.engine) as session:
            queue.queue_draft(session, self.draft_id, self.sender_id,
                              scheduled_at=utc_now() - timedelta(minutes=1))

    def process(self):
        with Session(self.engine) as session:
            return queue.process_due_queue(session)

    def draft(self):
        with Session(self.engine) as session:
            return session.get(EmailDraft, self.draft_id)

    def suppress(self, email):
        with Session(self.engine) as session:
            session.add(Suppression(email=email, reason="synthetic test"))
            session.commit()

    def test_normal_frozen_queue_sends_once(self):
        self.enqueue()
        self.assertEqual(self.process()["sent"], 1)
        self.assertEqual(self.process()["processed"], 0)
        self.assertEqual(len(self.sent_messages), 1)
        self.assertEqual(self.sent_messages[0]["To"], "to@example.test")

    def test_cancel_before_processing_prevents_delivery(self):
        self.enqueue()
        with Session(self.engine) as session:
            main.cancel_queue_items(draft_ids=[self.draft_id], session=session)
        self.assertEqual(self.process()["processed"], 0)
        self.assertEqual(self.draft().status, "approved")
        self.assertEqual(self.sent_messages, [])

    def test_cancel_waits_for_active_send_and_does_not_claim_success(self):
        self.enqueue()
        in_login = threading.Event()
        release_login = threading.Event()
        cancel_attempted = threading.Event()
        real_lock = threading.Lock()

        class ObservedLock:
            def acquire(self, *args, **kwargs):
                if threading.current_thread().name == "synthetic-cancel":
                    cancel_attempted.set()
                return real_lock.acquire(*args, **kwargs)
            def release(self):
                real_lock.release()
            def __enter__(self):
                self.acquire()
                return self
            def __exit__(self, *args):
                self.release()

        gate = ObservedLock()
        def login():
            in_login.set()
            if not release_login.wait(5):
                raise AssertionError("Test synchronization timed out")
        self.login_hook = login
        def cancel():
            threading.current_thread().name = "synthetic-cancel"
            with Session(self.engine) as session:
                return main.cancel_queue_items(draft_ids=[self.draft_id], session=session)

        with patch.object(queue, "QUEUE_PROCESSING_LOCK", gate), patch.object(main, "QUEUE_PROCESSING_LOCK", gate):
            with ThreadPoolExecutor(max_workers=2) as executor:
                sending = executor.submit(self.process)
                try:
                    self.assertTrue(in_login.wait(5))
                    cancelling = executor.submit(cancel)
                    self.assertTrue(cancel_attempted.wait(5))
                    self.assertFalse(cancelling.done())
                finally:
                    release_login.set()
                self.assertEqual(sending.result(timeout=5)["sent"], 1)
                response = cancelling.result(timeout=5)
                self.assertIn("Skipped:%201", response.headers["location"])
        self.assertEqual(self.draft().status, "sent")
        self.assertEqual(len(self.sent_messages), 1)

    def test_contact_edit_rejected_while_queued(self):
        self.enqueue()
        with Session(self.engine) as session:
            main.update_draft_prep_contact(
                contact_id=self.contact_id, company="Synthetic Company",
                full_name="Changed", email="changed@example.test", position="",
                country="", region="", priority="B", session=session,
            )
        with Session(self.engine) as session:
            self.assertEqual(session.get(Contact, self.contact_id).email, "to@example.test")
        self.assertEqual(self.draft().status, "queued")

    def test_import_like_recipient_change_blocks_send(self):
        self.enqueue()
        with Session(self.engine) as session:
            contact = session.get(Contact, self.contact_id)
            contact.email = "changed@example.test"
            session.add(contact)
            session.commit()
        self.process()
        self.assertEqual(self.draft().status, "pending_review")
        self.assertEqual(self.sent_messages, [])

    def test_legacy_queue_without_frozen_address_needs_review(self):
        self.enqueue()
        with Session(self.engine) as session:
            draft = session.get(EmailDraft, self.draft_id)
            draft.template_snapshot = "{}"
            session.add(draft)
            session.commit()
        self.process()
        self.assertEqual(self.draft().status, "pending_review")
        self.assertEqual(self.sent_messages, [])

    def test_cc_suppressed_after_enqueue_blocks_without_mutating_cc(self):
        self.enqueue()
        self.suppress("cc@example.test")
        self.process()
        self.assertEqual(self.draft().status, "pending_review")
        self.assertEqual(self.draft().cc_emails, "cc@example.test")
        self.assertEqual(self.sent_messages, [])

    def test_domain_suppression_during_smtp_login_blocks_delivery(self):
        self.enqueue()
        self.login_hook = lambda: self.suppress("@example.test")
        self.process()
        self.assertEqual(self.draft().status, "pending_review")
        self.assertIsNone(self.draft().approved_at)
        self.assertEqual(self.sent_messages, [])

    def test_recipient_change_during_smtp_login_blocks_delivery(self):
        self.enqueue()
        def mutate():
            with Session(self.engine) as session:
                contact = session.get(Contact, self.contact_id)
                contact.email = "changed@example.test"
                session.add(contact)
                session.commit()
        self.login_hook = mutate
        self.process()
        self.assertEqual(self.draft().status, "pending_review")
        self.assertEqual(self.sent_messages, [])

    def test_queued_attachment_change_rejected(self):
        self.enqueue()
        attachment = Path(self.temp.name) / "synthetic.txt"
        attachment.write_text("synthetic attachment", encoding="utf-8")
        with Session(self.engine) as session:
            main.update_draft_attachments(self.draft_id, attachment_paths=str(attachment), session=session)
        self.assertIsNone(self.draft().attachment_paths)
        self.assertEqual(self.draft().status, "queued")

    def test_unknown_delivery_is_not_automatically_retried(self):
        self.enqueue()
        self.delivery_error = TimeoutError("synthetic outcome unknown")
        self.process()
        self.assertEqual(self.draft().status, "send_unknown")
        self.assertEqual(self.process()["processed"], 0)
        self.assertEqual(len(self.sent_messages), 1)


if __name__ == "__main__":
    unittest.main()
