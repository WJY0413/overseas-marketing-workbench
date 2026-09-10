---
name: linkedin-greeting-workflow
description: Process one completed LinkedIn People-search capture into parsed library records and unsent connection-greeting drafts. Use after capture completion when the operator explicitly specifies the compatible LinkedIn library path; never use for browsing or outreach.
---

# LinkedIn Greeting Workflow

This Workbench-facing controller turns one completed capture into verified parsed records and greeting drafts stored in the operator-selected LinkedIn library. It controls `$linkedin-raw-library-ingest` and `$linkedin-two-layer-parser-v1` rather than repeating their low-level rules.

## Required input

Require all of the following before any write:

- a completed LinkedIn mining task directory;
- the exact target `linkedin_raw_library.sqlite` path chosen by the operator or Workbench setting;
- a new output directory for parsing and QA artifacts;
- an approved human-readable network-owner label.

Never infer, substitute, or overwrite another database path. If the supplied capture has not passed its terminal completion gate, stop and return the capture blocker.

## Controlled route

1. Call `$linkedin-raw-library-ingest` with the completed task and the exact selected library path. Require a successful ingest receipt.
2. Call `$linkedin-two-layer-parser-v1` against that same library. Require full precision parsing and independent QA from original card evidence; uncertain records remain review.
3. Require its database validation before preparing greetings.
4. Run `scripts/write_greeting_drafts.py --database <exact selected library path>`. It writes only `linkedin_greeting_draft` rows in that same library.
5. To make the ready drafts appear as Workbench's pending LinkedIn connection contacts, run `scripts/sync_greeting_drafts_to_master.py --draft-library <exact selected library path> --master <Workbench data/linkedin_people_master.sqlite path>`. This is the explicit handoff into the operator's LinkedIn master; it does not mark a connection as sent.
5. Report actual ingest, parsed, review, and `draft_ready` counts plus the selected library path.

## Greeting boundary

Create an English greeting draft only where the parse result has independent-QA support, a strict company route, and a role. Do not invent company facts, mutual contacts, products, prior conversations, or familiarity. Entries that lack sufficient evidence are stored as `greeting_status=review` with no draft text.

Drafts are not outreach. Do not send a message, mark a contact as invited/contacted, or write any CRM, BD database, or production people master.
