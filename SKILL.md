---
name: bd-email-workbench-lite
description: Operate the local BD Email Workbench for imports, draft review, queue planning, dry-run checks, sending, bounce scans, and daily status. Use when the user asks to prepare or inspect a campaign, check queue/send health, process bounces, or send approved B2B email through this repository.
---

# BD Email Workbench Operator

Use the application database and current UI state as authority. Resolve the project root from the current repository; never assume a machine-specific absolute path.

## Daily flow

1. Read `.env` only to confirm configuration keys; never print secret values.
2. Confirm `/health` responds and record whether `DRY_RUN_EMAIL` is enabled.
3. Inspect import results, draft count, approved count, queued count, sender availability, daily limits, bounces, and replies before proposing an action.
4. Keep new or changed campaigns in dry-run until the user has reviewed recipients, templates, sender identity, links, attachments, and schedule.
5. Real sending requires an explicit instruction that identifies the approved batch or drafts. Do not infer authorization from “prepare”, “queue”, or “review”.
6. After a send, verify `SendRecord` evidence and report sent, simulated, failed, bounced, replied, and remaining counts separately.

## Common commands

```powershell
python -m compileall -q app scripts
python scripts/smoke_test.py
python scripts/validate_email_quality_cases.py
python scripts/validate_attachments.py --help
python scripts/scan_bounces.py --help
```

Use `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` to start the UI.

## Data and safety boundary

- Do not commit `.env`, `data/`, `*.db`, reports, contacts, sender passwords, or local encryption keys.
- Use only business contacts the operator is permitted to process.
- Honor suppression, unsubscribe, bounce, and do-not-contact state before queueing.
- Keep provider limits conservative and treat platform/provider rules as current external facts that may need verification.

## Output

Lead with current state, risks, the exact next action, and the evidence path. Counts from chat memory are not authoritative when the live database is available.
