---
name: workbench-batch-planner
description: Run the BD Email Workbench integrated, read-only batch planner. Use when Cooper asks for current sendable counts, planned templates, exclusions, or already-arranged work before any queue handoff.
---

# Workbench Batch Planner

For Workbench v0.4.26 or later, the planner lives in Workbench. Do not use the legacy static-template-ID script for a current Workbench plan.

1. Check `GET http://127.0.0.1:8000/health` and `GET /planner`.
2. Run the read-only endpoint:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/planner/run'
```

3. Report `already_arranged`, `newly_queueable`, `no_compatible_unused_template`, cooldown/success-lock exclusions, and the returned recipient/template rows.

The endpoint never creates drafts, queues a draft, sends mail, or changes a template policy. Compatibility is read from persisted template rotation policies, not a static template-ID list. Active first-touch templates are General by default; Sports / Line Marking templates require a configured company-context keyword; Manual Only templates never enter automatic rotation. A company excludes only a template that already succeeded for that company.

Before template rotation, v0.4.27+ excludes: a company with any successful send in the preceding 48 hours; the selected exact primary-recipient email with a successful send in the preceding 7 days; and a selected contact whose source `priority_roll_status` is `success_locked`. These are returned as `company_48h_cooldown`, `recipient_7d_cooldown`, and `source_success_locked`.

Never write the Workbench database, generate drafts, modify queue state, send email, change suppression, or treat this plan as proof that a send occurred. Use production `sendrecord` as send truth and `emaildraft` only as draft/queue state.
