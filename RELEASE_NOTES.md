# Overseas Marketing Workbench v5.05

Released 2026-10-14.

LinkedIn library selection is now available in Settings. Use the native library or select an existing compatible SQLite library by absolute path. New installations create an empty native library; switching preserves both libraries and their histories. The active source and path are shown on LinkedIn pages. Validation is read-only, configuration persists, and stale pages cannot mark records after a library switch. A launcher environment override is shown explicitly and cannot be silently changed from the UI.

Startup memory is reduced by limiting numerical worker allocation before imports and loading Excel support only when needed. The release also includes the existing local fixes for queue concurrency, protected recipients, last-moment suppression checks, delivery-state display and suppression expiry timestamps.

Validation: 244 Python tests and 213 subtests passed, plus both JavaScript behavior suites. One Python-version-specific launcher test was skipped; the unchanged internal seed distribution was not rebuilt. Both public archives passed package checks and import smoke tests. No real mail was sent by validation.

Upgrade with the included updater after stopping Workbench. Preserve operator-owned data, credentials, queue settings and LinkedIn libraries. Public Clean and Pure Empty packages include ten bundled Workbench Skills and no operational database or credentials. The private history MCP runtime and operator-specific reconciliation are not public distribution contents.

---

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
