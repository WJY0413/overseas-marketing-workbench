---
name: workbench-inbox-check
description: Check BD Email Workbench sender inboxes for replies, bounce clues, customer responses, or delivery notices. Use when the operator asks to inspect inbox, check whether someone replied, search sender mailbox, verify bounce or delivery messages, or manually ingest reply evidence. This skill is read-only by default and does not send email or build queues.
---

# Workbench Inbox Check

Use this narrow skill for sender mailbox inspection.

## Scope

- Search a Workbench sender's INBOX and Spam/Junk folder for replies, bounce notices, or delivery clues.
- Use `sendrecord` first to identify likely sender mailbox when the target recipient/company is known.
- Return mailbox, query, date window, matching messages, and recommended action.
- Do not send email, modify Workbench state, or suppress/delete routes unless the operator explicitly asks for that follow-up operation.
- For routine inbox operations, optimize for speed: first report human replies since the last successful inbox check, then defer bounce cleanup to a separate thread or later slow pass.

## Active Paths

- Production app: `<detected-workbench-root>`
- Production DB: `<detected-workbench-root>\data\workbench.db`
- Production Python: `<detected-workbench-root>\.venv\Scripts\python.exe`

## Rules

1. Default to read-only inbox search.
2. Treat Spam/Junk as a required second mailbox, not as noise. A human business reply, actionable automatic reply, or delivery notice found there follows the same rules as an INBOX hit; report its source folder clearly.
3. Prefer targeted searches by sender mailbox, recipient/company, subject, date window, or clue. Avoid broad all-mailbox scans unless the operator asks.
3. Never print SMTP/IMAP passwords or secret env values.
4. If reply evidence changes CRM status or suppression, switch to the relevant write workflow and ask for confirmation.
5. No backup is required for read-only inbox checks.
6. For recurring or "check my inbox" requests, determine the time window from the last-check state before scanning. Use `<operator-workspace>\coo_state\workbench_inbox_last_check.json` when present; otherwise default to the past 24 hours.
7. Do not run long, broad bounce scans in the main thread. The main thread should finish the fast reply report first. Bounce processing belongs in a child/background thread, with production DB backup before writes.
8. Avoid extra processes. Before starting a slow scan, check whether a relevant Workbench Python scan is already running; after timeout or interruption, verify no new orphan process remains.
9. Reply triage order:
   - Human/business replies from external contacts.
   - Auto-replies that state the contacted person retired/left/is no longer responsible and provide a replacement email/contact route.
   - Actionable auto-replies with alternate contact details.
   - Ticket acknowledgements from target companies.
   - Internal notifications and unrelated mail are noise unless the operator asks for full mailbox triage.
10. Bounce triage order:
   - Confirm the bounced address is tied to Workbench `sendrecord.recipient_email` for the same sender.
   - Treat CC/body-extracted addresses as evidence only, not automatic suppression targets.
   - Deduplicate by recipient email and report already-suppressed vs newly-actionable bounces separately.
11. DB-first follow-up rule: when a reply/contact/company is selected for backcheck, follow-up, routing, or a child thread, query the production Workbench DB before using mailbox snippets or public web research as the main context.
    - Check `sendrecord`, `company`, `contact`, and `contactroute` for company/contact IDs, sender mailbox, recipient email, CC routes, sent history, company status, contact status, source, and existing route evidence.
    - If the company likely exists in the operator's BD database, query that source next before public research.
    - Treat the inbox message as the latest trigger/evidence, not as the system of record.
    - Pass DB IDs and DB-derived context into any background/backcheck thread.
12. Reply-class emails must read like a direct human reply, not a campaign template.
    - Start from the recipient's latest message and answer that specific ask.
    - Keep wording short, natural, and specific; avoid bulk-marketing phrasing, overlong product claims, and repeated self-introductions inside an existing thread.
    - When attaching a catalogue or proposing a meeting, mention it plainly and tie it to the recipient's requested details.
13. Scheduled reply rule: when the operator asks to reply to a business contact, schedule the send inside the recipient's local working hours whenever possible.
    - Prefer authoritative DB/source evidence first, then official/company pages for office hours if timing matters.
    - If the office opens at 08:00, prefer about 08:30 local time; if it opens at 09:00, prefer 09:00-09:30 local time.
    - Avoid weekends and known local holidays unless the operator explicitly asks for urgency.
    - A scheduled-send handoff must include exact sender, recipient, subject, body, attachment path, recipient timezone, scheduled local time, and the required proof check after sending.

## Fast Routine Workflow

1. Read `coo_state\workbench_inbox_last_check.json` for `last_successful_check_at`.
2. Search INBOX first, then Spam/Junk, for every active Workbench sender from that timestamp to now. Use IMAP `LIST` to identify the provider's actual Spam/Junk folder name; do not assume a missing `Spam` folder means no spam scan occurred.
3. Return the reply report first:
   - `reply_count`,
   - sender mailbox,
   - date,
   - from,
   - company/contact if matched from production DB,
   - short action needed,
   - source folder (`INBOX` or exact Spam/Junk folder).
4. Update the last-check state only after the reply scan succeeds.
5. Start or hand off bounce cleanup as a separate thread when needed. Do not wait for slow bounce cleanup before reporting replies.
6. Save secondary evidence under `<operator-workspace>\output\`.

## Reply Follow-Up / Backcheck Workflow

Use this workflow before drafting a reply, researching a person, or opening a child thread from a mailbox hit.

1. Use the reply sender, subject, recipient mailbox, and nearby quoted message to search production `sendrecord`.
2. Join to `company`, `contact`, and `contactroute`; capture IDs, names, status, source, latest sent date, sender account, recipient email, and CC route clues.
3. If no exact `sendrecord` hit exists, search by sender domain, company name, email domain, and subject keywords before concluding "not in DB".
4. Check BDdatabase only when Workbench DB indicates a likely company/domain match or the operator asks for broader BD master context.
5. Only after DB context is captured should public background research or a child thread start.
6. Child-thread prompt must include DB evidence first, then inbox evidence, then requested public-research tasks.

## Human Reply / Scheduled Send Handoff

Use this when a mailbox reply needs a real follow-up message.

1. Confirm the actual reply route from the inbox message and compare it with Workbench DB routes; use the real reply sender unless the operator asks to use an older route or CC list.
2. Determine the recipient's local timezone and working-hours window from DB/source evidence or official/company pages.
3. Draft as a concise human reply in the existing thread.
4. If sending later, create the scheduled job with:
   - sender account,
   - recipient and CC/BCC policy,
   - subject,
   - final body,
   - attachment paths,
   - scheduled time in both the operator local time and recipient local time,
   - instruction to verify sent proof after execution.
5. Do not use a cold-outreach template for a reply-class email.

## Script

Use `scripts/search_sender_inbox.py` for read-only IMAP checks.

```powershell
& "<detected-workbench-root>\.venv\Scripts\python.exe" "<codex-skills-root>\workbench-inbox-check\scripts\search_sender_inbox.py" --sender-email "sender@example.com" --query "Example Company" --since-days 7 --limit 20
```

## Output

```text
结论: found / not found / blocked
邮箱: sender mailbox and date window
线索: message date/from/subject/snippet
下一步: ingest reply / suppress bounce / no action
```
