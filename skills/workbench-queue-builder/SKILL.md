---
name: workbench-queue-builder
description: Hand confirmed batch email campaigns to the operator's production BD Email Workbench and ensure its existing queue is resumed. Use when the operator says this batch, today arrange sending, batch queue, regional outreach, or delayed campaign sending. This skill preserves one company per selected email, submits the batch through Workbench, starts the existing Workbench app in the background only when needed, and verifies appsetting.queue_paused=false. It must not implement its own risk checks, SMTP loop, scheduler, worker, or send completion logic. Use workbench-send-mail instead for one reviewed email.
---

# Workbench Batch Handoff

Use this skill only as a thin handoff into the existing Workbench batch workflow.

## Active Paths

- Production app: `<detected-workbench-root>`
- Production DB: `<detected-workbench-root>\data\workbench.db`
- Production Python: `<detected-workbench-root>\.venv\Scripts\python.exe`

## Workflow Boundary

- Use `workbench-send-mail` for one reviewed email, customer reply, or test send.
- Use this skill for a confirmed batch.
- Workbench owns its existing risk review, suppression handling, draft generation, queue timing, random delay, scheduler, SMTP sending, and send history.
- This skill only hands over the confirmed batch, ensures the existing Workbench app is available, and resumes the Workbench queue.

## Required Flow

1. If the scope is not yet a confirmed roster, run `workbench-batch-planner` first and review its `main_candidates.csv`, alternates, exclusions, sender capacity, and sync-required count. Do not reimplement those planning rules here.
2. Preserve the confirmed batch scope: selected companies, one selected email per company, template, sender, schedule, and expected count. Do not invent an additional risk gate.
3. For cold outreach that rolls a company to a new contact, query that company's successful `sendrecord` history before draft generation. The selected template must not have been used in any prior successful send to that company. Resolve historical template identity from `sendrecord.template_id`, or from the linked `emaildraft.template_id` when the send record has no template id. If no compatible active unused template is available, block that company from the batch; never silently reuse a prior template.
4. This company-level template-rotation gate applies only when switching to a new cold-outreach contact. It does not override true reply or follow-up subject-continuity rules owned by `workbench-send-mail`.
5. Submit the batch through the existing Workbench app/service. Do not reproduce Workbench behavior in a helper script and do not write a separate sending loop.
6. If the production Workbench app is not running, start its existing FastAPI app once in the background. Do not create a Windows scheduled task, watchdog, supervisor, or replacement worker. Do not start a duplicate instance when it is already running.
7. Resume the queue through the Workbench app/service and read back `appsetting.queue_paused=false`.
8. Treat any `Template field resolution failed` result as a hard block. Read and report the exact failed field, affected draft/company count, and template. Immediately tell the operator that the batch can proceed by switching those rows to another approved template that does not require the failed field. Do not reduce this to a generic `failed` or `blocked` count, and do not switch templates without the operator's existing template rule or approval.
9. Report only the handoff result: accepted batch count, template, sender, schedule, queued count, Workbench availability, `queue_paused` value, and any expanded field-resolution block.

## Confirmation Rule

Require the operator's explicit approval of the batch scope before submitting it to a live queue or changing `queue_paused` from `true` to `false`. Once confirmed, do not add another approval checkpoint unless the scope changes.

## Prohibited Behavior

- Never call `process_due_queue(limit=...)` as the batch execution mechanism.
- Never send a fixed number of messages directly and then exit.
- Never implement custom email validation, suppression, SMTP, retry, pacing, heartbeat, completion monitoring, or reconciliation.
- Never create a custom queue worker, scheduled task, supervisor, or background service.
- Never claim the batch is completed merely because it was handed to Workbench.
- Never alter Workbench production code or database schema as part of a normal batch handoff.
- Never queue or send rendered content containing unresolved placeholders, `None`, `null`, `undefined`, `NaN`, or line breaks in email headers. Workbench must block these before SMTP delivery.

## Read-Only Planning

Use `workbench-batch-planner` when the operator asks to inspect or preview queue scope. It must not send or mutate the queue.

For a cold-outreach preview that includes rolled contacts, report the count of companies whose preferred template was changed because it had already been used, plus any companies blocked because no compatible unused template remained.

## Output

```text
结论: handed off / needs confirmation / blocked
批次: accepted count, template, sender, schedule
Workbench: available / unavailable
队列: queued count, queue_paused=false/true
边界: Workbench owns review and sending; no custom sender was run
```
