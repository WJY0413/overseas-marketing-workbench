---
name: linkedin-mining-v3
description: Safely capture a signed-in LinkedIn People search result set page by page with a fail-closed pacing guard, cross-task daily caps, raw checkpoints, and terminal proof. Use when the operator asks to mine or resume native LinkedIn people-search results without opening profiles, performing outreach, or modifying an official master.
---

# LinkedIn Mining V3

Use this workflow only for LinkedIn People-search result pages supplied by the operator. Capture raw people-search evidence; do not enrich, rank, open profiles, invite, message, or write an official SQLite master. V3 preserves the V2 capture behavior and adds operational load-safety controls; it does not guarantee that LinkedIn will not review or restrict an account.

## Required route

1. Read the existing task state before interacting. Resume only from `next_page`.
2. Initialize a new task with `scripts/init_linkedin_capture_task.py` when no task directory exists.
3. Start the global guard before browser interaction:
   `python <skill-dir>\scripts\linkedin_mining_guard.py start --task-dir <task-dir> --next-page <next_page>`.
   The guard state is shared at `output/linkedin_mining_guard_state.json`; do not delete, reset, or hand-edit it.
4. Parse and freeze the supplied source identity before browser use: `connectionOf`, declared `network` scope, and task directory. LinkedIn may normalize or remove `lipi`; validate the durable query values instead of requiring byte-identical URLs.
5. Use the controlled signed-in browser selected for the supplied LinkedIn URL. Read the browser-control skill before browser use.
6. Navigate to the known previous checkpoint page. Do not invent or probe page URLs after a failed load.
7. Before every page navigation, run:
   `python <skill-dir>\scripts\linkedin_mining_guard.py before-page --task-dir <task-dir> --page <N>`.
   This command waits until the minimum interval has elapsed and then reserves the page. If it exits non-zero, stop; never bypass the guard.
8. Use a fresh visible DOM to find the actual `Page N` or `Next` control. Click only that observed node. Never click a result-card link, a Connect/Pending/Follow control, a profile, or Messaging.
9. After navigation, verify the People-search URL reports the expected page, preserves the frozen source identity/scope, and contains at least one direct result-card link in `main`. Require the ordered first/last profile signature to change from the previous page.
10. Extract direct `a[href*="/in/"]` anchors whose own visible text contains a relationship marker allowed by the declared scope (`1st` for `F`, `2nd` for `S`). Ignore mutual-connection/profile links whose own text lacks an allowed marker. Within one page, dedupe by canonical profile URL and keep the longest matching direct-card text; do not dedupe repeated cards across pages. Retain `href`, name, identity, location, action label, relationship degree, and complete card text.
11. Detect `Next` from the live visible button text. Call `scripts/append_linkedin_raw_capture_page.py` immediately after each valid page. Treat a successful script response as the checkpoint before any subsequent page navigation.
12. After the append succeeds, commit the guard reservation with:
    `python <skill-dir>\scripts\linkedin_mining_guard.py commit-page --task-dir <task-dir> --page <N> --cards <count> [--terminal]`.
    If append fails, run `cancel-page` for that page and stop. If commit fails, stop and preserve the raw checkpoint for review.
13. Continue page by page using only the immutable task files and checkpoints. Do not write the fixed raw library while the capture is still in progress.
14. Before any terminal decision, run the terminal-stability check below. Stop only after its two delayed observations confirm no `Next` (or a documented no-results terminal probe). Reconcile raw pages, links, task-master rows, duplicate reviews, and terminal evidence, then require the capture completion gate to pass.
15. After the completion gate passes, run `linkedin_mining_guard.py finish` and only then invoke `$linkedin-raw-library-ingest` exactly once for the completed task directory. Require its successful raw-library receipt before downstream handoff or the final workflow report. An ingest failure never invalidates the completed source capture.
16. After raw-library ingest and validation, finalize the browser tabs as the final browser action. Make no browser call after finalization.

## Built-in load-safety guard

The V3 guard is intentionally conservative and fail-closed. Its compiled defaults are:

- Minimum interval between page starts: **25 seconds**.
- Single-run cap: **100 pages or 1000 raw cards**, whichever is reached first.
- Daily cap across all V3 tasks: **100 pages or 1000 raw cards**, whichever is reached first.
- Daily session cap: **2 sessions**.
- One active task at a time; a second task or an unrecovered page reservation is rejected.

The command line cannot raise these limits. One full 100-page run therefore consumes the full daily page/card budget; shorter runs may share the remaining daily budget across at most two sessions. The guard uses a cross-process lock and an atomic JSON state file so separate tasks share the same daily budget. It rolls over only at the next Asia/Shanghai natural day. If the state is corrupt, the clock is invalid, a lock is held, a CAPTCHA/review appears, or any guard command fails, stop and report the blocker. Do not attempt to evade platform review with randomization, concurrency, hidden tabs, direct API calls, or a manual state reset.

If a process stops after `before-page` but before `commit-page`, inspect the raw checkpoint, then run `linkedin_mining_guard.py recover --task-dir <task-dir> --next-page <workflow_state.next_page>` before resuming. Recovery clears only the in-flight reservation; it does not increase the daily allowance.

## Browser and pagination recovery

- On `ERR_CONNECTION_CLOSED`, stale tab, or target-page disconnect, rebuild the tab/browser connection and reopen the exact frozen source URL. Do not restart Codex; a Codex restart always requires the operator's explicit authorization.
- If the observed `Next` click fails, refresh the visible DOM and retry once after the page settles. If it still fails, reload or open a fresh tab from the same browser binding and verify the source identity again.
- Only when the current page/checkpoint is proven and a visible `Page N+1` control was observed may the run navigate once to that exact next page as recovery. Never probe arbitrary page numbers. Verify expected page, source identity, card count, and changed first/last profile signature before accepting it.
- Treat CAPTCHA, identity review, account restriction, or repeated navigation failure as a blocker. Stop and leave the checkpoint resumable.

## No-Next Continuation Constraint

When the operator asks to mine a result set to completion, continue page-by-page for as long as a valid page shows `Next`, subject to the guard. Do not end a turn merely because of intermediate progress, a page-count milestone, or an assumed time limit. Checkpoint every valid page first; if a turn or runtime interruption occurs, resume from `next_page` in the next available turn. A genuine external blocker (for example, loss of access, CAPTCHA awaiting the operator, guard quota exhaustion, or repeated browser failure) may be reported, but never treat it as completion.

## Terminal-stability check (mandatory)

`Next` absent or zero extracted cards is never terminal evidence by itself. Before appending a page with `next_visible:false`:

1. Wait at least 5 seconds after the navigation that produced the apparent terminal state.
2. Take a fresh visible DOM snapshot; record the URL, ordered canonical profile sequence, card count, and visible `Next` state.
3. Wait a further 8 seconds and repeat the same observation.
4. Append as terminal only when both observations have the same durable source identity, URL, ordered profile sequence, and card count as the payload, and `Next` is absent in both. If cards materialize, order/count changes, or `Next` appears, do not append a terminal page; continue from the rendered page.

Include this object on every terminal payload; the append script rejects a terminal payload without it:

```json
"terminal_confirmation": {
  "initial_wait_ms": 5000,
  "recheck_wait_ms": 8000,
  "first_url": "https://www.linkedin.com/search/...",
  "second_url": "https://www.linkedin.com/search/...",
  "first_card_count": 0,
  "second_card_count": 0,
  "first_profile_sequence_sha256": "...",
  "second_profile_sequence_sha256": "...",
  "first_next_visible": false,
  "second_next_visible": false
}
```

## Task creation

Create a new independent task directory under the active project `output/` folder:

```powershell
python <skill-dir>\scripts\init_linkedin_capture_task.py `
  --task-dir output\<source>_linkedin_connection_<scope> `
  --source-target "<anchor name>" `
  --source-url "<supplied LinkedIn people-search URL>" `
  --network-scope "<S or F,S from the supplied URL>"
```

Do not overwrite an existing task. Do not modify source JSONL after capture. Do not sync to `linkedin_people_master.sqlite` in this skill.

## Path and encoding discipline

- Resolve the task directory once from the initialization receipt or existing manifest and reuse that exact absolute path. Do not repeatedly retype a long `connectionOf` directory name.
- Before append, verify the expected `page_NNN_payload.b64` exists under that resolved task directory. After append, require the receipt page number and `next_page` to match the browser page.
- On Windows, use `--payload-b64` for payloads and UTF-8-aware reads for JSON/JSONL. Do not pipe default-encoding `Get-Content` into `ConvertFrom-Json`; use `Get-Content -Raw -Encoding UTF8` or the Python validators.
- Keep browser output concise. Do not serialize screenshots or base64 browser metadata into page receipts unless needed for a specific diagnosis.

## Raw-library handoff

The task JSON/JSONL and `raw_pages/` directory remain the immutable capture evidence. Do not hand off an in-progress task. After the capture completion gate passes, hand the completed task directory to `$linkedin-raw-library-ingest` exactly once. That mini skill owns the configured raw-library path, schema, idempotent insert, source-mutation rejection, and database validation.

Do not parse, enrich, dedupe against a master, rate, or promote records in this capture skill. Legacy `sync_linkedin_capture_to_staging.py` files are compatibility artifacts and are not the normal route for new captures.

## Per-page contract

The append payload must contain:

```json
{"page": 13, "source_url": "https://www.linkedin.com/search/...", "next_visible": true, "cards": [{"name":"...","identity":"...","location":"...","action_label":"Connect","relationship_degree":"2nd","linkedin_profile":"https://www.linkedin.com/in/...","page_intro":"..."}]}
```

Use `--payload-b64` for Unicode-safe payload transfer on Windows. The append script rejects out-of-sequence pages, non-direct profile links, blank identity evidence, duplicate raw-page writes, and a repeated first/last-card signature.

## Completion gate

The capture completion gate passes only when all are true:

- `workflow_state.json` is `complete` and `next_page` is null.
- `coverage_audit.json` has `coverage_status: complete`, a terminal page, and `final_valid_page_without_next` or an explicit no-results terminal reason.
- The terminal raw page has a validated `terminal_confirmation` with two delayed, matching no-Next observations.
- Raw pages are consecutive from page 1 to the terminal page with no missing number.
- The direct-link ledger equals the count of raw source cards; `contacts_master.jsonl` may be lower only by explicit duplicate-profile reviews.
- The V3 guard has committed every captured page and has been finished cleanly.

After this gate passes, perform the one-time raw-library ingest. State the page range, raw-card/link count, unique task-master count, duplicate count, raw-library ingest result, guard totals, and that no LinkedIn action or official SQLite-master write occurred.
