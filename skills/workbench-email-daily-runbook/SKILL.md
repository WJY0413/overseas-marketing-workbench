---
name: workbench-email-daily-runbook
description: Route the operator's daily BD Email Workbench operations and use narrower Workbench skills when possible. Use when the operator asks broad or mixed Workbench questions about sendability, drafts, queues, sending, inbox replies, bounces, sender/template/signature checks, or routine parameters. Prefer workbench-send-mail for sending, workbench-inbox-check for inbox/reply checks, workbench-queue-builder for queue creation, and workbench-parameter-tuner for common setting changes.
---

# Workbench Email Daily Runbook

Use this skill as the broad Workbench router and compatibility layer. For narrow tasks, use specialist skills first:

- Use `workbench-send-mail` for single sends, internal test sends, confirmed batch sends, and `sendrecord` verification.
- Use `workbench-inbox-check` for sender inbox search, reply checks, bounce clues, and delivery notices.
- Use `workbench-queue-builder` for queue creation, sender distribution, schedule planning, and queue readiness.
- Use `workbench-parameter-tuner` for sender daily limits, send windows, delay ranges, open tracking, active flags, and `queue_paused`.
- Use `workbench-batch-planner` for Workbench v0.4.26+ integrated read-only planning at `/planner` / `/api/planner/run`; it reads persisted rotation policies rather than a static template-ID list.

Do not use this skill for lead discovery, company scoring, rolling contact research, generic outreach strategy, or Codex maintenance.

## Scope

- Count currently sendable targets and explain why rows are excluded.
- Check, preview, generate, update, delete, queue, unqueue, or approve drafts when the task is not better handled by a specialist skill.
- Apply and audit To/CC rules, including company-level CC behavior.
- Validate selected sender, rendered signature, and sent-record sender consistency.
- Confirm what was actually sent from `sendrecord`; treat `emaildraft` as draft/queue state, not proof of sending.
- Process bounce suppression and manual reply ingestion when it changes who should receive email.

## Safety Rules

1. Default to read-only inspection.
2. Never start a queue, resume sending, or perform a production send without the operator's explicit confirmation for that exact action unless the current user message itself is an explicit send command.
3. Before queue, draft, suppression, or production DB writes, read current production DB state and say whether the queue is paused.
4. Do not back up routine sends, batch sends, queue execution, inbox checks, or common parameter changes. Default backups are for Workbench version updates/code releases and data migrations, including DB schema migrations or planned bulk data transformations.
5. Keep C-drive test copies and `<detected-workbench-root>` separate in all status reports.
6. Bounce cleanup should remove or suppress only the bad email route where possible; preserve the company/contact row.
7. Use current DB truth over UI assumptions. Stale browser or draft previews can be wrong.
8. When a draft or queue reports `Template field resolution failed`, expand the error immediately: name the field, template, and affected count, then tell the operator that switching to an approved template without that field is the direct remedy. Never report only a generic failure count.

## Active Paths

- Production app: `<detected-workbench-root>`
- Production DB: `<detected-workbench-root>\data\workbench.db`
- Production Python: `<detected-workbench-root>\.venv\Scripts\python.exe`
- App source: `<detected-workbench-root>\app`
- Test workspace entrypoint: `<operator-workspace>`

## Route By Intent

| User intent | Required route |
| --- | --- |
| send, test email, single email, confirmed batch send, remove placeholders | `workbench-send-mail` |
| inbox, reply check, bounce clue, delivery notice, mailbox search | `workbench-inbox-check` |
| create queue, queue approved drafts, distribute across senders, schedule queue | `workbench-queue-builder` |
| daily limit, send window, delay range, open tracking, active flag, queue_paused | `workbench-parameter-tuner` |
| sendable counts, exclusions, already-arranged work, today batch plan | `workbench-batch-planner` via Workbench `/planner` |
| generate drafts from template, follow-up draft generation, CC rules | `references/template-draft-generation.md` |
| inspect/delete/unqueue/requeue drafts outside queue-building scope | `references/draft-and-queue-operations.md` |
| signature or sender-render mismatch | `references/signature-and-sender-audit.md` |
| bounce suppression or reply ingestion that changes CRM state | `references/bounce-and-reply-handling.md` |

Open and follow the relevant reference before taking action. Use scripts when they fit; otherwise write a small one-off inspection script and keep it read-only unless the operator confirmed a write.

## Daily Startup Checklist

For any broad Workbench email task, first collect:

- Running app process and port if UI behavior matters.
- `appsetting.queue_paused`.
- Counts by `emaildraft.status`.
- Recent `sendrecord` count and latest sent timestamp.
- Any sender/template/follow-up scope the operator named.

Then state the action boundary in Chinese, for example:

`我只检查可发数量，不启动队列。当前 queue_paused=true。`

## Arranged-Batch Readback

After a confirmed batch is handed to Workbench, keep policy eligibility separate from execution readiness. Report `safe`, `already arranged`, `queued with planned recipient`, and `pending/approved exception` as different counts. A pending or approved draft is arranged work, but is not proof that it can actually send.

For a queue handoff, defer to `workbench-queue-builder` for the recipient-snapshot readback and Jarvis native `workbench_queue_progress` probe registration. This observer reads Workbench directly and does not start a Codex/AI turn. Do not call a batch arranged-and-running until its queued primary recipient matches the planner-approved `selected_to` route.

## Tooling Preference

- Use SQLite reads for authoritative status.
- Use Workbench's existing app/API paths when available for behavior that the UI also uses.
- Use direct SQL writes only for narrow maintenance actions such as pausing the queue or deleting a clearly scoped draft set, and only after the operator confirms the exact production change.
- Do not add backups unless it is a version update or data migration, or the operator explicitly asks.
- Use `scripts/draft_queue_admin.py` for draft inspection, scoped unqueue/requeue status changes, and unsent-draft deletion previews.
