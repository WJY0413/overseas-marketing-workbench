import argparse
import os
import sys
from pathlib import Path


def bootstrap(app_dir: str):
    app_path = Path(app_dir)
    os.chdir(app_path)
    sys.path.insert(0, str(app_path))


def parse_bool(value: str | None):
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    raise SystemExit(f"invalid boolean: {value}")


def main():
    parser = argparse.ArgumentParser(description="Inspect or update common Workbench parameters.")
    parser.add_argument("--app-dir", default=r"<detected-workbench-root>")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--sender-email")
    parser.add_argument("--daily-limit", type=int)
    parser.add_argument("--window-start")
    parser.add_argument("--window-end")
    parser.add_argument("--delay-min", type=int)
    parser.add_argument("--delay-max", type=int)
    parser.add_argument("--open-tracking")
    parser.add_argument("--active")
    parser.add_argument("--queue-paused")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    bootstrap(args.app_dir)

    from datetime import time
    from sqlmodel import Session, select
    from app.db import engine
    from app.models import AppSetting, EmailDraft, SenderAccount
    from app.time_utils import utc_now
    from app.services.queue import format_sender_windows, parse_sender_windows_text, sender_windows

    def parse_time(value: str | None):
        if not value:
            return None
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))

    with Session(engine) as session:
        print("queue:")
        queue_setting = session.exec(select(AppSetting).where(AppSetting.key == "queue_paused")).first()
        queued = session.exec(select(EmailDraft).where(EmailDraft.status == "queued")).all()
        print(f"  queue_paused={queue_setting.value if queue_setting else 'missing'} queued={len(queued)}")
        print("senders:")
        senders = session.exec(select(SenderAccount).order_by(SenderAccount.id)).all()
        for sender in senders:
            print(
                f"  id={sender.id} email={sender.email} active={int(sender.is_active)} "
                f"limit={sender.daily_limit} timezone={sender.send_timezone} windows={format_sender_windows(sender)} "
                f"delay={sender.random_delay_min_seconds}-{sender.random_delay_max_seconds} "
                f"open_tracking={int(sender.enable_open_tracking)}"
            )

        if args.show and not args.apply:
            return

        changes = []
        sender = None
        if args.sender_email:
            sender = session.exec(select(SenderAccount).where(SenderAccount.email == args.sender_email.strip().lower())).first()
            if sender is None:
                raise SystemExit(f"blocked: sender not found: {args.sender_email}")
            if args.daily_limit is not None:
                changes.append(("sender.daily_limit", sender.daily_limit, args.daily_limit))
                if args.apply:
                    sender.daily_limit = args.daily_limit
            if args.window_start or args.window_end:
                current = sender_windows(sender)
                if len(current) != 1 and not (args.window_start and args.window_end):
                    raise SystemExit("blocked: multiple windows require both --window-start and --window-end to replace the window set")
                start = parse_time(args.window_start) or current[0][0]
                end = parse_time(args.window_end) or current[0][1]
                windows = parse_sender_windows_text("", fallback_start=start, fallback_end=end)
                changes.append(("sender.send_windows", format_sender_windows(sender), f"{start}-{end}"))
                if args.apply:
                    sender.window_start, sender.window_end = start, end
                    sender.send_windows_json = windows
            if args.delay_min is not None:
                changes.append(("sender.random_delay_min_seconds", sender.random_delay_min_seconds, args.delay_min))
                if args.apply:
                    sender.random_delay_min_seconds = args.delay_min
            if args.delay_max is not None:
                changes.append(("sender.random_delay_max_seconds", sender.random_delay_max_seconds, args.delay_max))
                if args.apply:
                    sender.random_delay_max_seconds = args.delay_max
            open_tracking = parse_bool(args.open_tracking)
            if open_tracking is not None:
                changes.append(("sender.enable_open_tracking", sender.enable_open_tracking, open_tracking))
                if args.apply:
                    sender.enable_open_tracking = open_tracking
            active = parse_bool(args.active)
            if active is not None:
                changes.append(("sender.is_active", sender.is_active, active))
                if args.apply:
                    sender.is_active = active
            if args.apply and changes:
                sender.updated_at = utc_now()
                session.add(sender)

        queue_paused = parse_bool(args.queue_paused)
        if queue_paused is not None:
            old_value = queue_setting.value if queue_setting else "missing"
            new_value = "true" if queue_paused else "false"
            changes.append(("appsetting.queue_paused", old_value, new_value))
            if args.apply:
                if queue_setting is None:
                    queue_setting = AppSetting(key="queue_paused", value=new_value)
                else:
                    queue_setting.value = new_value
                    queue_setting.updated_at = utc_now()
                session.add(queue_setting)

        if not changes:
            print("no requested changes")
            return
        print("requested_changes:")
        for field, old, new in changes:
            print(f"  {field}: {old} -> {new}")
        if not args.apply:
            print("preview_only=true")
            return

        session.commit()
        print("applied=true")


if __name__ == "__main__":
    main()
