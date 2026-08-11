# Overseas Marketing Workbench v0.4.22

## Queue recovery

- A draft delayed by its sender's configured randomized interval is appended after that sender's existing queue tail.
- Multiple delayed drafts append sequentially with new randomized sender intervals.
- Drafts that are not delayed retain their planned times, and separate sender accounts remain independently schedulable.
- `sender_interval_tail_deferred` records this queue-recovery action in the audit trail.

## BD master source

- Workbench now reads the normalized SQLite BD master directly from its `companies` and `contacts` tables.
- Contact-table data overrides stale nested contact snapshots, while per-contact route and exclusion metadata remains intact.
- A legacy JSON master remains supported only as a compatibility fallback.

## Verification

- Queue recovery, multi-sender independence, daily limit, and send-window tests run against an in-memory SQLite database.
- SQLite source loading and isolated Workbench synchronization are covered by regression tests.
- The clean-package verifier confirms that the archive excludes local environment files, databases, reports, backups, virtual environments, and compiled caches.
