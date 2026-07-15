import os
import base64
import json
import mimetypes
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from uuid import uuid4

from sqlmodel import Session

from app.config import get_settings
from app.models import Contact, EmailDraft, SenderAccount
from app.services.secrets import decrypt_secret


def parse_attachment_paths(value: str | None) -> list[str]:
    if not value:
        return []
    text = value.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = re.split(r"[\r\n;]+", text)
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        path = str(item).strip().strip('"').strip("'")
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def serialize_attachment_paths(value: str | list[str] | None) -> str | None:
    if isinstance(value, list):
        paths = parse_attachment_paths(json.dumps(value, ensure_ascii=False))
    else:
        paths = parse_attachment_paths(value)
    return json.dumps(paths, ensure_ascii=False) if paths else None


def validate_attachment_paths(value: str | None) -> list[str]:
    issues: list[str] = []
    for item in parse_attachment_paths(value):
        path = Path(item).expanduser()
        if not path.exists():
            issues.append(f"Attachment not found: {item}")
        elif not path.is_file():
            issues.append(f"Attachment path is not a file: {item}")
    return issues


def _add_file_attachments(msg: EmailMessage, value: str | None) -> int:
    attached = 0
    for item in parse_attachment_paths(value):
        path = Path(item).expanduser()
        if not path.is_file():
            raise RuntimeError(f"Attachment not found: {item}")
        content_type, _ = mimetypes.guess_type(path.name)
        if content_type:
            maintype, subtype = content_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        attached += 1
    return attached


def _read_env_value(key: str) -> str:
    value = os.getenv(key, "")
    if value:
        return value
    env_path = Path(".env")
    if not key or not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        current_key, current_value = line.split("=", 1)
        if current_key.strip() == key:
            return current_value.strip().strip('"').strip("'")
    return ""


def add_open_pixel(body_html: str, tracking_id: str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    pixel = (
        f'<img src="{base}/tracking/open/{tracking_id}.png" width="1" height="1" '
        'style="display:none" alt="" />'
    )
    if "</body>" in body_html.lower():
        return body_html.replace("</body>", f"{pixel}</body>")
    return f"{body_html}\n{pixel}"


def _prepare_inline_images(body_html: str) -> tuple[str, list[dict[str, str | bytes]]]:
    inline_images: list[dict[str, str | bytes]] = []

    def replace_data_image(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        content_type = match.group("content_type").lower()
        payload = re.sub(r"\s+", "", match.group("payload"))
        suffix = match.group("suffix")
        try:
            image_bytes = base64.b64decode(payload, validate=True)
        except ValueError:
            return match.group(0)
        maintype, subtype = content_type.split("/", 1)
        if subtype == "jpg":
            subtype = "jpeg"
        cid = f"template-image-{uuid4().hex}"
        inline_images.append(
            {
                "cid": cid,
                "data": image_bytes,
                "maintype": maintype,
                "subtype": subtype,
            }
        )
        return f"{prefix}cid:{cid}{suffix}"

    html = re.sub(
        r"(?P<prefix><img\b[^>]*\bsrc=[\"'])data:(?P<content_type>image/(?:png|jpe?g|gif|webp));base64,(?P<payload>[^\"']+)(?P<suffix>[\"'][^>]*>)",
        replace_data_image,
        body_html,
        flags=re.IGNORECASE,
    )
    return html, inline_images


def send_draft(session: Session, draft: EmailDraft, sender: SenderAccount) -> str:
    contact = session.get(Contact, draft.contact_id)
    if contact is None:
        raise RuntimeError("Contact not found.")

    html = draft.body_html
    if get_settings().enable_open_tracking and sender.enable_open_tracking:
        html = add_open_pixel(html, draft.tracking_id)

    if get_settings().dry_run_email:
        return "dry_run"

    password = decrypt_secret(sender.smtp_password_encrypted)
    if not password:
        password = _read_env_value(sender.password_env or "")
    if not password:
        raise RuntimeError("Missing SMTP password. Save it on the sender settings page or configure the password environment variable.")

    msg = EmailMessage()
    msg["From"] = f"{sender.name} <{sender.email}>"
    msg["To"] = contact.email
    if draft.cc_emails:
        msg["Cc"] = draft.cc_emails
    msg["Subject"] = draft.subject
    msg.set_content(draft.body_text or "Please view this email in an HTML-capable client.")
    html, inline_images = _prepare_inline_images(html)
    msg.add_alternative(html, subtype="html")
    if inline_images:
        html_part = msg.get_payload()[-1]
        for image in inline_images:
            html_part.add_related(
                image["data"],
                maintype=image["maintype"],
                subtype=image["subtype"],
                cid=f"<{image['cid']}>",
            )
    _add_file_attachments(msg, draft.attachment_paths)

    if sender.smtp_port == 465:
        smtp = smtplib.SMTP_SSL(sender.smtp_host, sender.smtp_port, timeout=30)
    else:
        smtp = smtplib.SMTP(sender.smtp_host, sender.smtp_port, timeout=30)

    with smtp:
        if sender.smtp_port != 465:
            smtp.starttls()
        smtp.login(sender.smtp_username or sender.email, password)
        smtp.send_message(msg)
    return "sent"
