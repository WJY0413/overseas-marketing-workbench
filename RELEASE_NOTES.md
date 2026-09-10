# Overseas Marketing Workbench v5.03

Released 2026-09-10 after review and test acceptance.

This release preserves approved recipients and CC rules through queue handoff, applies sender/contact checks at delivery time, and makes pause/cancel and daily capacity scheduling consistent. Uncertain or partially accepted deliveries remain visible for verification instead of being blindly retried.

It also preserves historic evidence during bounce cleanup, validates TLS certificates, fixes SQLite backup and migration paths, and improves LinkedIn confirmation, restore and paging. Homepage previews now load on demand in a sandbox. The package includes 10 Workbench Skills.

Validation of the reviewed application: 169 unittest cases passed with one Python-version skip, five helper tests and two Node behavior suites passed. The v5.03 packages passed archive/content checks and clean import smoke validation. Real outbound transport and full browser visual checks were not part of release validation.

## Downloads

- Production Clean: application, setup scripts and bundled Skills, with no operational database or credentials.
- Pure Empty: no sample or seed data; same safe first-run defaults.

The internal LinkedIn/NO-GO seed distribution is not included in this public repository or release. Configure your own sender, signature and data sources. NO-GO feedback requires an explicit `WORKBENCH_NOGO_FEEDBACK_TO` environment variable and an operator action; no internal feedback recipient is embedded in the public build.

Stop the existing application before an in-place upgrade, back up its database, and use `scripts/update_existing_workbench.ps1`. Preserve the existing `.env`, local databases and operator-owned LinkedIn master. A queued or SMTP-accepted message is not proof of final delivery.
