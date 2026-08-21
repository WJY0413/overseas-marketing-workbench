# v0.4.27

- Added the Workbench-integrated, read-only batch planner at `/planner`.
- Replaced static template-ID compatibility lists with persisted template rotation policies.
- Added General, Sports / Line Marking, and Manual Only policy scopes.
- Reports already arranged drafts separately from newly queueable companies and no-compatible-unused-template exclusions.
- Preserves the existing durable 50-item queue handoff, primary-recipient hard blocks, and one-time CC cleanup behavior.
- Enforces the 48-hour company cooldown, 7-day exact-primary-recipient cooldown, and source `success_locked` route exclusion before any template rotation.
- Shows those three exclusions separately in the planner response.
