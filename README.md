# BD Email Workbench Lite

This repository is a local-first workbench for preparing, reviewing, scheduling, sending, and auditing B2B email campaigns. It turns a loose spreadsheet or company database export into a controlled queue with draft approval, sender limits, follow-up rules, bounce suppression, and send records.

The public version starts in dry-run mode. It contains no customer database, mailbox password, send history, signature identity, production path, or private business note.

## What works

- CSV/Excel and BD JSON contact import
- reusable HTML/text templates and per-recipient draft generation
- human approval before queueing
- multiple SMTP sender accounts and per-sender daily limits
- scheduled queue processing, pause/resume/cancel, and follow-up rules
- optional open tracking, disabled by default
- IMAP bounce scanning and suppression records
- local SQLite storage and Excel sending reports
- encrypted local SMTP password storage or environment-variable lookup

## Start on Windows

1. Install Python 3.11, 3.12, or 3.13.
2. Clone this repository.
3. Double-click `start.cmd`, or run:

```powershell
.\start.ps1
```

The launcher creates `.venv`, installs dependencies, copies `.env.example` to `.env`, and opens <http://127.0.0.1:8000>.

## Start manually

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

For macOS/Linux, activate the virtual environment with `source .venv/bin/activate` and use the same `pip` and `uvicorn` commands.

## Safe first run

Keep these values until you have inspected every draft and sender setting:

```env
DRY_RUN_EMAIL=true
ENABLE_OPEN_TRACKING=false
```

Dry-run messages are recorded as `simulated`; they are not sent over SMTP. Before changing to real sending, add your own mailbox, app password, signature, legal identity, and provider-specific daily limits.

## Public package boundaries

Included: application source, HTML templates, relative-path launchers, maintenance and validation scripts, an example environment file, and a Codex operator skill.

Not included: `.env`, SQLite files, sender credentials, local encryption keys, contacts, customer data, send/reply/bounce history, production reports, backups, or internal campaign templates.

See [`SCRIPT_INDEX.md`](SCRIPT_INDEX.md) for all packaged operating, validation, backup, and release scripts.

## Daily Codex skill

Clone or copy this whole repository into your Codex skills directory as `bd-email-workbench-lite`. The root `SKILL.md` makes the application and all maintenance scripts available to the daily operator skill. It keeps routine work in dry-run/review mode unless real sending is explicitly authorized.

## Verification

```powershell
python -m compileall -q app scripts
python scripts/smoke_test.py
```

## Known limits

This is a practical single-user local application, not a hosted multi-tenant service. Real sending depends on your SMTP/IMAP provider and your compliance obligations. The app does not create consent or make a campaign lawful by itself.

## License

MIT. See `LICENSE`.
