"""Queue-time delivery for Workbench-rendered Feishu-native email replies."""

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app.services.mailer import parse_attachment_paths
from app.services.delivery_outcome import DeliveryUncertainError


NATIVE_REPLY_MODE = "feishu_native_reply"
NATIVE_QUOTE_MARKERS = (
    "history-quote-wrapper",
    "history-quote-meta-wrapper",
    "history-quote-gap-tag",
    "quote-head-meta-mailto",
)
NATIVE_QUOTE_ID_RE = re.compile(
    r'''(?i)(?:id|data-[\w:-]+)\s*=\s*["']lark-mail-quote-[^"']+["']'''
)


def native_reply_metadata(template_snapshot: str | None) -> dict:
    try:
        value = json.loads(template_snapshot or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def is_native_reply_draft(template_snapshot: str | None) -> bool:
    return native_reply_metadata(template_snapshot).get("delivery_mode") == NATIVE_REPLY_MODE


def _run_lark(*args: str, cwd: str | None = None) -> dict:
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    npm_root = Path(shutil.which("lark-cli.cmd") or "").parent
    node = shutil.which("node.exe") or shutil.which("node")
    run_script = npm_root / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
    if not node or not run_script.is_file():
        raise RuntimeError("lark-cli runtime was not found on PATH")
    result = subprocess.run(
        [node, str(run_script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=120,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"lark-cli failed ({result.returncode}): {detail[:1500]}")
    payload = json.loads(result.stdout)
    if payload.get("ok") is False:
        raise RuntimeError(f"lark-cli returned an error: {json.dumps(payload, ensure_ascii=False)}")
    return payload


def get_feishu_message(mailbox: str, message_id: str) -> dict:
    payload = _run_lark(
        "mail",
        "user_mailbox.messages",
        "get",
        "--as",
        "user",
        "--user-mailbox-id",
        mailbox,
        "--message-id",
        message_id,
        "--format",
        "json",
    )
    message = payload.get("data", {}).get("message")
    if not isinstance(message, dict):
        raise RuntimeError(f"Feishu message not found: {mailbox} / {message_id}")
    return message


def _read_back_message(mailbox: str, message_id: str) -> dict:
    last_error = None
    for _ in range(6):
        try:
            return get_feishu_message(mailbox, message_id)
        except Exception as exc:  # pragma: no cover - depends on mail API latency
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"sent message could not be read back: {last_error}")


def _decode_body_html(value: str | None) -> str:
    if not value:
        return ""
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return value


def _native_quote_evidence(value: str | None) -> dict:
    body_html = _decode_body_html(value)
    marker_hits = [marker for marker in NATIVE_QUOTE_MARKERS if marker in body_html]
    has_quote_id = bool(NATIVE_QUOTE_ID_RE.search(body_html))
    valid = "history-quote-wrapper" in body_html or (
        any(marker in body_html for marker in ("history-quote-meta-wrapper", "quote-head-meta-mailto"))
        and has_quote_id
    )
    return {
        "valid": valid,
        "markers": marker_hits,
        "has_lark_mail_quote_id": has_quote_id,
        "decoded_html_length": len(body_html),
    }


def _copy_attachments(paths: list[str], temp_dir: Path) -> list[str]:
    copied = []
    used = set()
    for index, item in enumerate(paths, start=1):
        source = Path(item).expanduser().resolve()
        if not source.is_file():
            raise RuntimeError(f"attachment not found: {item}")
        name = source.name
        if name.lower() in used:
            name = f"{index}-{name}"
        used.add(name.lower())
        shutil.copy2(source, temp_dir / name)
        copied.append(name)
    return copied


def send_queued_native_reply(draft, sender, contact) -> dict:
    """Send one already-approved reply through Feishu's native thread path."""
    metadata = native_reply_metadata(draft.template_snapshot)
    source_message_id = str(metadata.get("source_message_id") or "").strip()
    if not source_message_id:
        raise RuntimeError("Native reply draft is missing source_message_id.")

    source = get_feishu_message(sender.email, source_message_id)
    source_from = (source.get("head_from") or {}).get("mail_address", "").strip().lower()
    if source_from != contact.email.strip().lower():
        raise RuntimeError("Native reply source sender no longer matches the approved contact route.")
    if source.get("folder_id") != "INBOX":
        raise RuntimeError("Native reply source is no longer in the sender INBOX.")
    source_thread_id = str(source.get("thread_id") or "").strip()
    if not source_thread_id:
        raise RuntimeError("Native reply source has no Feishu thread ID.")

    with tempfile.TemporaryDirectory(prefix="workbench-native-reply-") as temp_name:
        temp_dir = Path(temp_name)
        (temp_dir / "reply.html").write_text(draft.body_html, encoding="utf-8")
        attachment_names = _copy_attachments(parse_attachment_paths(draft.attachment_paths), temp_dir)
        command = [
            "mail",
            "+reply",
            "--as",
            "user",
            "--mailbox",
            sender.email,
            "--from",
            sender.email,
            "--message-id",
            source_message_id,
            "--body-file",
            "reply.html",
            "--no-signature",
            "--confirm-send",
            "--format",
            "json",
        ]
        if draft.cc_emails:
            command.extend(["--cc", draft.cc_emails])
        if attachment_names:
            command.extend(["--attach", ",".join(attachment_names)])
        try:
            response = _run_lark(*command, cwd=temp_name)
        except Exception as exc:
            raise DeliveryUncertainError(f"Feishu reply outcome requires verification: {exc}",
                {"source_message_id": source_message_id}) from exc

    data = response.get("data", {})
    if data.get("automation_send_disable_reason"):
        raise RuntimeError(
            "Feishu automation blocked send: "
            f"{data.get('automation_send_disable_reason')} "
            f"{data.get('automation_send_disable_reference') or ''}".strip()
        )
    outgoing_message_id = data.get("message_id")
    outgoing_thread_id = data.get("thread_id")
    if not outgoing_message_id or not outgoing_thread_id:
        raise DeliveryUncertainError("Feishu reply returned incomplete delivery identifiers.", data)

    receipt = {"feishu_message_id": outgoing_message_id, "thread_id": outgoing_thread_id,
               "source_message_id": source_message_id, "external_accepted": True}
    try:
        sent_message = _read_back_message(sender.email, outgoing_message_id)
    except Exception as exc:
        raise DeliveryUncertainError(f"Feishu accepted reply; readback requires verification: {exc}", receipt) from exc
    quote_evidence = _native_quote_evidence(sent_message.get("body_html"))
    same_thread = sent_message.get("thread_id") == source_thread_id == outgoing_thread_id
    sent_in_sent = sent_message.get("folder_id") == "SENT"
    if not (same_thread and sent_in_sent and quote_evidence["valid"]):
        raise DeliveryUncertainError(
            "Feishu native reply verification failed: "
            + json.dumps(
                {
                    "same_thread": same_thread,
                    "sent_in_sent": sent_in_sent,
                    "native_quoted_history_evidence": quote_evidence,
                },
                ensure_ascii=False,
            ), receipt
        )
    return {
        "mode": NATIVE_REPLY_MODE,
        "source_message_id": source_message_id,
        "source_smtp_message_id": source.get("smtp_message_id"),
        "source_thread_id": source_thread_id,
        "feishu_message_id": outgoing_message_id,
        "smtp_message_id": sent_message.get("smtp_message_id"),
        "thread_id": outgoing_thread_id,
        "true_thread_reply": True,
        "native_quoted_history_evidence": quote_evidence,
    }
