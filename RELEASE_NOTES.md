# Overseas Marketing Workbench v5.05 candidate

Prepared 2026-10-02 from public commit `b8f7732651d0440ee75074afa5b56bd1a6b2297f`.
This is a source review candidate, not a production deployment or a published release.

## Scope

- Serialize single-item cancellation with the existing in-process send gate; re-read selected draft state before processing. Direct queue intake uses that gate too.
- Reject contact edits while that contact has queued, sending or uncertain mail. Freeze the queued To address in the existing draft snapshot and check it before delivery, including changes made through other data-update paths.
- Check CC email/domain suppression at send audit without silently changing CC. SMTP rechecks fresh delivery fields, To/CC suppression and company NO-GO after login and before send_message. Native replies run the same callback before the external reply command.
- Reject queued attachment edits; cancel and review the draft before changing them.
- Retain sending/send_unknown protection and no automatic retry of uncertain delivery.

The gate is process-local. This candidate does not establish multi-process exactly-once delivery. A restriction committed after the final preflight cannot retract an already-started external send. Suppression at preflight blocks the whole message rather than changing its recipients.

## Existing queues and upgrade boundary

Queued drafts without the new frozen address fail closed into pending_review. Review them and explicitly queue again; they are not silently migrated or sent. Existing sending/send_unknown records require outcome verification and are not made retryable by this change. No database schema migration is introduced.

Stop the existing application before a future authorized upgrade. This branch does not modify production data, credentials, main, or existing release tags. No fresh ZIP/wheel or release asset checksums are claimed; existing package manifest/checksum files belong to earlier artifacts.

## Validation and exclusions

Added 11 focused unittest regressions in `scripts/test_queue_safety.py`: temporary synthetic SQLite, fake SMTP, network/process guards, cancellation ordering, recipient changes, CC/domain suppression, queued attachments, and uncertain-delivery retry protection.

Run from the repository root after installing requirements in an isolated environment:

```text
python -m unittest discover -s scripts -p test_queue_safety.py -v
```

**Not run in this cloud task:** the selected execution environment has not become usable and no shell execution tool is available. Static review only; no PASS, full acceptance, or Linux runtime compatibility claim. No real email was sent. Fake SMTP tests do not prove provider acceptance or delivery.

The offline machine's exact P1/P2 patches and the 57 KB MCP handoff ZIP have not been received. This candidate implements only the public-source fixes above. It does not include or supersede the locally reviewed MCP changes or inherit their test results.

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
