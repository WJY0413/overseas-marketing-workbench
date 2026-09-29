# Overseas Marketing Workbench v5.04

Released 2026-09-29.

This maintenance release retains the v5.03 application behavior and extracts the existing domain normalization helper without changing its matching rules. The private, read-only domain-history one-shot workflow is installed only in the operator's local workspace; it is not part of this public package.

The package includes 10 Workbench Skills. Existing sender settings, queue, database, and local credentials remain operator-owned during an in-place upgrade.

Validation: 38 domain-history regression tests passed in the internal source tree. The public Clean and Pure Empty archives passed package checks and a clean import smoke check. No real email was sent during validation.

## Downloads

- Production Clean: application, setup scripts and bundled Skills, with no operational database or credentials.
- Pure Empty: no sample or seed data; same safe first-run defaults.

The internal LinkedIn/NO-GO seed distribution is not included in this public repository or release. Configure your own sender, signature and data sources. NO-GO feedback requires an explicit `WORKBENCH_NOGO_FEEDBACK_TO` environment variable and an operator action; no internal feedback recipient is embedded in the public build.

Stop the existing application before an in-place upgrade, back up its database, and use `scripts/update_existing_workbench.ps1`. Preserve the existing `.env`, local databases and operator-owned LinkedIn master. A queued or SMTP-accepted message is not proof of final delivery.
