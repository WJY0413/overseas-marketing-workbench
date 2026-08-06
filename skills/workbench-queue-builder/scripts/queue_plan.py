import argparse
import os
import sys
from pathlib import Path


def bootstrap(app_dir: str):
    app_path = Path(app_dir)
    os.chdir(app_path)
    sys.path.insert(0, str(app_path))


def main():
    parser = argparse.ArgumentParser(description="Read-only queue planning for Overseas Marketing Workbench.")
    parser.add_argument("--app-dir", default=r"<detected-workbench-root>")
    parser.add_argument("--template-id", type=int)
    parser.add_argument("--status", default="approved")
    parser.add_argument("--senders", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    bootstrap(args.app_dir)

    from sqlmodel import Session, select
    from app.db import engine
    from app.models import AppSetting, EmailDraft, FollowUpRule, SendRecord, SenderAccount

    with Session(engine) as session:
        queue_paused = session.exec(select(AppSetting).where(AppSetting.key == "queue_paused")).first()
        queued_count = len(session.exec(select(EmailDraft).where(EmailDraft.status == "queued")).all())
        query = select(EmailDraft).where(EmailDraft.status == args.status)
        if args.template_id:
            query = query.where(EmailDraft.template_id == args.template_id)
        candidates = session.exec(query.order_by(EmailDraft.id)).all()
        if args.limit > 0:
            candidates = candidates[: args.limit]
        sender_emails = [email.strip().lower() for email in args.senders.split(",") if email.strip()]
        senders = []
        for email in sender_emails:
            sender = session.exec(select(SenderAccount).where(SenderAccount.email == email)).first()
            if sender is not None and sender.is_active:
                senders.append(sender)
        active_followups = session.exec(select(FollowUpRule).where(FollowUpRule.is_active == True)).all()
        latest_send = session.exec(select(SendRecord).order_by(SendRecord.sent_at.desc())).first()

        print(f"queue_paused={queue_paused.value if queue_paused else 'missing'}")
        print(f"existing_queued={queued_count}")
        print(f"candidate_status={args.status}")
        print(f"candidate_template_id={args.template_id or ''}")
        print(f"candidate_count={len(candidates)}")
        print(f"active_followup_rules={len(active_followups)}")
        print(f"latest_sendrecord_id={latest_send.id if latest_send else ''}")
        print(f"latest_sent_at={latest_send.sent_at if latest_send else ''}")
        print("senders:")
        if senders:
            for idx, sender in enumerate(senders):
                assigned = len([draft for i, draft in enumerate(candidates) if i % len(senders) == idx])
                print(f"  {sender.email}: assigned_preview={assigned} limit={sender.daily_limit} window={sender.window_start}-{sender.window_end}")
        else:
            print("  none selected or none active")
        print("draft_ids_preview:")
        ids = [str(draft.id) for draft in candidates[:100]]
        print(",".join(ids))


if __name__ == "__main__":
    main()
