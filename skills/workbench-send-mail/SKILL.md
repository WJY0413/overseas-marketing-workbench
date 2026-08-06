---
name: workbench-send-mail
description: Prepare, preview, release, and verify email sends through the local 海外营销 Workbench. Use for a single email, internal test, confirmed batch release, or sent-record verification. Keep the native Workbench queue, suppression, sender, template, signature, and sendrecord controls authoritative; do not bypass them with a separate SMTP sender.
---

# Workbench Send Mail

Use the Workbench installation selected by `workbench-first-run-check`. Do not assume a fixed drive or create another app copy.

## Boundaries

- Inspect and draft read-only by default.
- Before a real send, show sender, To, CC, subject, rendered opening, signature, attachment list, cadence, and queue state.
- Obtain explicit approval for the exact send unless the current user message already gives that exact instruction.
- Use the native Workbench draft and queue routes. Do not build a parallel SMTP sender, scheduler, or database.
- Treat `sendrecord.smtp_status='sent'` as SMTP-acceptance evidence, not proof of final delivery.

## Pre-send gate

1. Confirm `workbench-first-run-check` is `READY` for environment, sender, signature, and Skills.
2. Read the live target database and current `queue_paused` state.
3. Confirm recipient and optional CC routes are not suppressed or attached to a NO-GO company.
4. Render the final subject, HTML/text body, and signature with the selected sender.
5. Block empty required fields, `None`, `null`, `undefined`, `NaN`, unresolved `{{ ... }}`, and CR/LF in headers.
6. Check recent successful sends for the same company, recipient, sender, and subject; explain any override before approval.
7. Confirm the selected sender is active, has remaining daily capacity, and has a valid local send window.

## Release and verify

- Release only the reviewed draft IDs through the native Workbench queue.
- Re-run queue audit immediately before delivery so an old draft cannot bypass current suppression or NO-GO state.
- After release, reconcile `emaildraft` status, queue state, and new `sendrecord` rows.
- Report queued, sent, failed, skipped, and the latest successful record separately. Queued is not sent.
- If any failure occurs, stop expanding the batch and report draft ID, recipient, subject, and exact error.

## Output

```text
结论: preview ready | awaiting confirmation | queued | partially sent | completed | blocked
Workbench: <detected path and version>
范围: sender=<...>, To=<n>, CC=<n>, drafts=<n>
安全检查: suppression=<pass/fail>, NO-GO=<pass/fail>, render=<pass/fail>, signature=<pass/fail>
生产证据: queue_paused=<...>, queued=<n>, sendrecord_sent=<n>, failed=<n>
下一步: <one action>
```
