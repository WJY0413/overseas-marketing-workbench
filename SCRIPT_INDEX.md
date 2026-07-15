# Script index

| Script | Purpose |
| --- | --- |
| `scripts/import_bd_json_and_generate_drafts.py` | Import normalized company/contact JSON and prepare drafts. |
| `scripts/scan_bounces.py` | Scan configured inboxes for bounce evidence. |
| `scripts/apply_bounce_cleanup.py` | Apply reviewed bounce suppression updates. |
| `scripts/validate_attachments.py` | Check attachment paths and supported files before sending. |
| `scripts/validate_email_quality_cases.py` | Exercise deterministic email-quality classifications. |
| `scripts/smoke_test.py` | Verify app import, routes, database setup, and dry-run defaults. |
| `scripts/backup_data.ps1` | Back up the active local data folder. |
| `scripts/backup_prod_db.ps1` | Create a production-database rollback copy. |
| `scripts/backup_test_db.ps1` | Create a test-database rollback copy. |
| `scripts/promote_test_to_prod.ps1` | Promote a reviewed test database through a guarded local workflow. |
| `scripts/rollback_prod_db.ps1` | Restore a selected local production backup. |
| `scripts/create_code_snapshot.ps1` | Snapshot source code without runtime data. |
| `scripts/create_shareable_package.ps1` | Build a data-free shareable application package. |
| `scripts/verify_shareable_package.ps1` | Check that a package excludes runtime data and required source files are present. |

Backup, promotion, rollback, bounce cleanup, and real sending are state-changing operations. Review their parameters and keep a current backup before use.
