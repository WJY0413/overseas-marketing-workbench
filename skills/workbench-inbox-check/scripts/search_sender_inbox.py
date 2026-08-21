import argparse
import email
import imaplib
import os
import re
import sys
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from pathlib import Path


def bootstrap(app_dir: str):
    app_path = Path(app_dir)
    os.chdir(app_path)
    sys.path.insert(0, str(app_path))


def decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def text_from_message(msg) -> str:
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype in {"text/plain", "text/html"}:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    chunks.append(payload.decode(charset, errors="replace"))
                except LookupError:
                    chunks.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        try:
            chunks.append(payload.decode(charset, errors="replace"))
        except LookupError:
            chunks.append(payload.decode("utf-8", errors="replace"))
    text = "\n".join(chunks)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def main():
    parser = argparse.ArgumentParser(description="Read-only IMAP search for one Workbench sender inbox.")
    parser.add_argument("--app-dir", default=r"D:\BD_Email_Workbench\production")
    parser.add_argument("--sender-email", required=True)
    parser.add_argument("--query", default="")
    parser.add_argument("--since-days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--mailbox", default="INBOX")
    parser.add_argument("--include-spam", action="store_true", help="Also search provider Spam/Junk folders discovered through IMAP LIST.")
    args = parser.parse_args()

    bootstrap(args.app_dir)

    from sqlmodel import Session, select
    from app.db import engine
    from app.models import SenderAccount
    from app.services.mailer import _read_env_value
    from app.services.secrets import decrypt_secret

    sender_email = args.sender_email.strip().lower()
    query = args.query.strip().lower()

    with Session(engine) as session:
        sender = session.exec(select(SenderAccount).where(SenderAccount.email == sender_email)).first()
        if sender is None:
            raise SystemExit(f"blocked: sender not found: {sender_email}")
        password = decrypt_secret(sender.smtp_password_encrypted)
        if not password:
            password = _read_env_value(sender.password_env or "")
        if not password:
            raise SystemExit("blocked: missing mailbox password")

        host = (sender.smtp_host or "smtp.feishu.cn").replace("smtp.", "imap.", 1)
        since = (datetime.now() - timedelta(days=args.since_days)).strftime("%d-%b-%Y")

        matches = []
        with imaplib.IMAP4_SSL(host, 993, timeout=30) as mailbox:
            mailbox.login(sender.smtp_username or sender.email, password)
            mailbox_names = [args.mailbox]
            if args.include_spam:
                status, folders = mailbox.list()
                if status != "OK":
                    raise SystemExit(f"blocked: imap LIST failed: {status}")
                for folder in folders or []:
                    name = folder.decode("utf-8", errors="replace").rsplit('"', 2)[-2]
                    if any(token in name.lower() for token in ("spam", "junk", "bulk", "垃圾")) and name not in mailbox_names:
                        mailbox_names.append(name)

            for mailbox_name in mailbox_names:
                if mailbox.select(mailbox_name, readonly=True)[0] != "OK":
                    continue
                status, data = mailbox.uid("SEARCH", None, "SINCE", since)
                if status != "OK":
                    continue
                uids = data[0].split()
                for uid in reversed(uids):
                    if len(matches) >= args.limit:
                        break
                    status, fetched = mailbox.uid("FETCH", uid, "(BODY.PEEK[])")
                    if status != "OK" or not fetched:
                        continue
                    raw = None
                    for item in fetched:
                        if isinstance(item, tuple):
                            raw = item[1]
                            break
                    if raw is None:
                        continue
                    msg = email.message_from_bytes(raw)
                    subject = decode(msg.get("Subject"))
                    from_ = decode(msg.get("From"))
                    date = decode(msg.get("Date"))
                    body = text_from_message(msg)
                    haystack = f"{subject}\n{from_}\n{body}".lower()
                    if query and query not in haystack:
                        continue
                    matches.append({
                        "mailbox": mailbox_name,
                        "uid": uid.decode("ascii", errors="ignore"),
                        "date": date,
                        "from": from_,
                        "subject": subject,
                        "snippet": body[:500],
                    })

    print(f"sender={sender_email}")
    print(f"mailbox={args.mailbox}; include_spam={args.include_spam}")
    print(f"since_days={args.since_days}")
    print(f"query={args.query}")
    print(f"matches={len(matches)}")
    for index, match in enumerate(matches, 1):
        print(f"\n[{index}] mailbox={match['mailbox']} uid={match['uid']}")
        print(f"date={match['date']}")
        print(f"from={match['from']}")
        print(f"subject={match['subject']}")
        print(f"snippet={match['snippet']}")


if __name__ == "__main__":
    main()
