---
name: workbench-batch-planner
description: Run the BD Email Workbench integrated, read-only batch planner. Use when the operator asks for current sendable counts, planned templates, exclusions, or already-arranged work before any queue handoff.
---

# Workbench Batch Planner

For Workbench v0.4.26 or later, the planner lives in Workbench. Do not use the legacy static-template-ID script for a current Workbench plan.

## v0.4.26+ Run

1. Check `GET http://127.0.0.1:8000/health` and `GET /planner`.
2. Run the read-only endpoint:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/planner/run'
```

3. Report `already_arranged`, `newly_queueable`, `no_compatible_unused_template`, other exclusions, and the returned recipient/template rows.

The endpoint never creates drafts, queues a draft, sends mail, or changes a template policy. `/planner` is the policy-maintenance page. Compatibility is read from persisted template rotation policies, not a static template-ID list. Active first-touch templates are General by default; Sports / Line Marking templates require a configured company-context keyword; Manual Only templates never enter automatic rotation. A company excludes only a template that already succeeded for that company.

Before template rotation, v0.4.27+ also excludes: a company with any successful send in the preceding 48 hours; the selected exact primary-recipient email with a successful send in the preceding 7 days; and a selected contact whose source `priority_roll_status` is `success_locked`. These are returned as `company_48h_cooldown`, `recipient_7d_cooldown`, and `source_success_locked`; they are not silently folded into the generic excluded total.

## Legacy Fallback (pre-v0.4.26 only)

Use the bundled script only when the target Workbench lacks `/planner`. Explicitly report that its template compatibility policy may be older than the Workbench policy; do not mix its count with the integrated plan.

## Legacy Script Run

```powershell
python scripts/plan_batch.py `
  --bd-db "C:\path\bd_company_database.sqlite" `
  --workbench-db "D:\path\workbench.db" `
  --out "C:\path\batch-plan-output"
```

Require both database paths to exist. Keep the Workbench database read-only; the script opens both SQLite files with `mode=ro` and `PRAGMA query_only=ON`.

## Legacy Script Policy

Apply these rules in the script-defined order:

1. Start from valid `email_send_status=include` routes. Treat `priority_roll_status` as source ordering metadata, never as contact lifecycle or route deliverability. Within each company, always prefer a personal To over a public inbox; within the personal/public class prefer `priority_roll_status=ready`, then apply priority rank. Do not let a ready public inbox replace an otherwise eligible non-ready personal contact. `needs_roll_activation=true` is informational compatibility output only; never mutate the BD master merely to make the route active. Select one To and at most one public CC per company.
2. Exclude companies with a successful send in the last 48 hours. Then apply a live 7-day cooldown to each exact recipient route. A never-sent route or a route whose cooldown elapsed is eligible even when source roll metadata is blank or `not_eligible`; `success_locked` remains ineligible. Use prior successful-send count only as a ranking tie-breaker after live eligibility is established.
3. Exclude BD or Workbench No-Go, replied, unsubscribed, paused, blacklisted, blocked, or suppressed companies.
4. Exclude suppression, hard/repeated bounce, recent soft bounce, and source email-owner conflict. Treat active `pending_review`/`approved`/`queued` work as **already arranged**, not as unavailable: keep it in the plan and report it separately from new queue candidates. Do not generate a duplicate draft for it.
5. For previously contacted companies, require an unused compatible company-level first-touch template for the new route. First-contact companies need only a compatible active template.
6. Exclude ambiguous Workbench mappings. Treat unmapped records as data-eligible but not directly draft-generatable.
7. Keep one company per enterprise key. For shared email providers, deduplicate by the complete email address. Otherwise prefer the BD company domain, falling back to recipient domain.
8. Sort deterministically: companies never successfully contacted come first; then previously contacted companies outside the cooldown. Within each group, prefer higher rating, required contact priority, and lower prior successful-send count. Today's unreserved capacity equals active sender daily limits minus successful sends today and `approved`/`queued` drafts already scheduled today. Compare the final safe count with that unreserved capacity.

The shared-provider list includes Gmail, Outlook, Hotmail, Yahoo, AOL, iCloud, Live, Googlemail, BTConnect, Orange, Wanadoo, Free, Laposte, ProtonMail, and related common services. Update the script list only after reviewing a real false-positive/false-negative case.

## Outputs

Return and preserve:

- `batch_plan.json`: full policy, fingerprints, stages, exclusions, capacity, main candidates, and alternates.
- `main_candidates.csv`: one selected To per safe company, including template, enterprise key, capacity flag, and direct-draft readiness.
- `alternates.csv`: enterprise duplicates and sender-capacity overflow.
- `summary.md`: concise counts and the layered funnel.

Report runtime, safe company count, already-arranged count, newly-queueable count, today's unreserved capacity, direct-draft count, legacy-status-blocked count, sync-required count, and output paths. When a company cannot be newly arranged because no compatible unused template remains, state that reason explicitly; do not mislabel existing drafts or queues as an exclusion. Keep policy eligibility separate from current production draft-generatability until the status migration is deployed.

## Safety

Never write either authority database. Never generate drafts, modify queue state, send email, change suppression, or treat this plan as proof that a send occurred. Use production `sendrecord` as send truth and `emaildraft` only as draft/queue state.
