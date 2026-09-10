---
name: workbench-parameter-tuner
description: Inspect or modify common BD Email Workbench operational parameters such as sender daily_limit, send windows, random delay range, open tracking flag, active flag, and queue_paused. Use when the operator asks to change daily send limits, sender windows, queue pause/resume state, send interval parameters, sender activation, or other routine Workbench settings. This skill is not for sending email, inbox checks, queue construction, version updates, or data migrations.
---

# Workbench Parameter Tuner

Use this narrow skill for routine Workbench parameter inspection and updates.

## Scope

- Sender account parameters: `daily_limit`, `window_start`, `window_end`, `random_delay_min_seconds`, `random_delay_max_seconds`, `enable_open_tracking`, `is_active`.
- Queue state parameter: `appsetting.queue_paused`.
- Readback verification after every change.

## Active Paths

- Production app: `<detected-workbench-root>`
- Production DB: `<detected-workbench-root>\data\workbench.db`
- Production Python: `<detected-workbench-root>\.venv\Scripts\python.exe`

## Rules

1. Inspect current values first and show the exact row/key to be changed.
2. Do not back up routine parameter changes by default. Backups are for version updates and data migrations unless the operator explicitly requests one.
3. Use the smallest update possible and immediately read back the changed rows.
4. Never print SMTP passwords or secret env values.
5. If the requested change is a version update, code sync, DB schema migration, or planned bulk data transformation, stop and use the backup-required workflow instead.
6. Queue resume can cause live sends if queued drafts are due; report queued count and due-now count before unpausing.

## Script

Inspect:

```powershell
& "<detected-workbench-root>\.venv\Scripts\python.exe" "<codex-skills-root>\workbench-parameter-tuner\scripts\tune_workbench_parameters.py" --show
```

Apply after confirmation:

```powershell
& "<detected-workbench-root>\.venv\Scripts\python.exe" "<codex-skills-root>\workbench-parameter-tuner\scripts\tune_workbench_parameters.py" --sender-email "sender@example.com" --daily-limit 50 --apply
```

## Output

```text
结论: updated / no change / blocked
变更: exact sender/key old -> new
证据: readback rows
备份: not made by default unless version/data migration or requested
```
