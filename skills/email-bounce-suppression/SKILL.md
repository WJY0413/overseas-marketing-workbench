---
name: email-bounce-suppression
description: Operate the operator's Overseas Marketing Workbench bounce-mail, automatic-reply, route-replacement, and suppression workflow. Use when Codex needs to inspect Feishu/Lark mailbox bounces or auto-replies, preview delivery failures, handle retirement/leave/email-change notices, add suppressed recipient emails, remove or replace contact routes, review logs, or adjust the scanner policy. Preserve Contact and Company records while preventing unsuitable future sends.
---

# Email Bounce Suppression

Use this skill for 海外营销 Workbench 退信扫描、退信预览、退信邮箱剔除、suppression 写入和退信日志核查.

## Ground Rules

- Default language: Chinese.
- Use `email-workbench-operator-agent` first when the task also touches production DB, sender accounts, queue state, `.env`, or real Workbench operations.
- Production path: `<detected-workbench-root>`.
- Test/editable path: `<detected-workbench-root>`.
- Production DB: `<detected-workbench-root>\data\workbench.db`.
- Never send mail from this workflow.
- **Absolute bounce rule:** once the full message evidence confirms a mailbox bounce, suppress that exact address. Do not exempt 4xx/522 temporary failures, greylisting, recipient quota, IP/domain reputation limits, gateway or anti-spam/policy rejects, or sender-authentication failures. The only exception is an explicit, address-specific instruction from the operator.
- Before production DB writes or code edits, create an object-level backup of the exact DB/file/folder.
- The cleanup policy is: add bounced email to `suppression`, remove the bounced email/contact route and its drafts/send records, keep both the `Contact` row and the `Company` row. Do not delete contacts or companies.

Read `references/workbench-bounce-contract.md` before changing code, production DB, cleanup policy, or scanner matching logic.

## Automatic Replies and Adjustable Suppression

Treat automatic replies as mailbox evidence, not normal business replies. Do not mark a company `replied` unless the message also contains a substantive business response.

1. Classify the full message before writing: `temporary_absence`, `route_replaced`, `anti_spam_verification`, `service_acknowledgement`, `forwarding_or_ambiguous`, or `opt_out_or_no_contact`.
2. Use the actual reply sender, full body, and DB routes (`sendrecord`, `contact`, `contactroute`) as evidence. Do not infer a new private address.
3. Preserve historical Contact, Company, SendRecord, and message evidence. Do not delete rows.

### Decision Rules

- **Temporary absence with an explicit return date:** add or update an expiring `suppression` entry for the route. For organisation-wide closure, cover every active company route. Resume at the stated return date in recipient local time; use 09:00 only when no reopening time is provided. With no return date, record only.
- **Retired/left/role changed with an explicit replacement email:** permanently suppress or retire the old route; add or reactivate the stated replacement under the same company. Make the responsible successor primary and demote the old contact. A generic inbox remains secondary/CC-only unless the operator directs otherwise.
- **Retired/left without replacement:** permanently suppress or retire only the old route and report `contact_gap`.
- **Explicit mailbox migration:** replace only when the notice explicitly states the new route; otherwise report `forwarding_or_ambiguous`.
- **Anti-spam verification, whitelist request, CAPTCHA, or mail-security challenge:** permanently suppress exact address with reason `anti_spam_verification_required`; do not click, verify, whitelist, resend, or reply.
- **Service acknowledgement/ticket confirmation:** record explicit case/reference only; do not suppress, replace a contact, or count as business reply.
- **Company closure, acquisition, forwarding, or ambiguous notice:** do not infer a replacement. Permanently suppress only an explicitly retired/decommissioned address; otherwise report for review.
- **Opt-out/no-contact notice:** permanently suppress the explicit address and follow an explicit company-wide instruction.

### Expiry Requirement

Write temporary absence only after live `suppression` supports `expires_at` and every queue/filter ignores rows where `expires_at <= now`. Keep `expires_at IS NULL` as permanent. If that support is absent, do not misuse a permanent suppression for a holiday: report it pending implementation. Never expire a hard bounce, opt-out, anti-spam-verification, or retired-route suppression.

## Common Tasks

### Preview Historical Bounces

Run read-only IMAP checks first when the operator says "先看", "预览", "查退信", or questions whether INBOX is readable.

- Log in with existing sender credentials from `SenderAccount`.
- Select `INBOX` read-only.
- Prefer reading `ALL` and doing local bounce detection; Feishu IMAP subject/from search may miss Chinese `邮件退信`.
- Print counts and matched bounced recipients. Do not write `suppression`, `BounceRecord`, `Contact`, `Company`, `EmailDraft`, or `SendRecord`.

### Apply Bounce Suppression

For approved write runs:

1. Back up `data\workbench.db`.
2. Run a read-only full-scope preview first. Do not rely on the production scanner alone when it cannot prove all sender inboxes or CC routes.
3. Verify:
   - `BounceRecord` count,
   - distinct bounced emails,
   - suppression rows,
   - contacts are preserved with the bounced email removed/replaced by an internal invalid placeholder,
   - no remaining `Company.status == "bounced"`,
   - no companies were deleted by the cleanup.
4. Report backup path, counts, and log path.

Default command in production:

```powershell
cd <detected-workbench-root>
.\.venv\Scripts\python.exe .\scripts\scan_bounces.py
```

Use a one-off script only when the operator requests a full historical pass or a read-only preview. Keep one-off scripts under the app `scripts/` folder or the current workspace `output/`, not inside production data folders.

## Matching Rules

- Treat `sendrecord` as the proof source for emails sent by Workbench.
- Match each bounce against both `SendRecord.recipient_email` and `SendRecord.cc_emails`. Search all sender aliases when an exact sender-alias match is absent.
- A confirmed bounce with no current Workbench route is still a valid suppression-only target. Do not invent a Contact, Company, or route merely to make it match.
- Keep duplicate bounce messages as logs, but dedupe cleanup by recipient email.
- Common bounce signals: `mailer-daemon`, `postmaster`, `Mail delivery failed`, `Undeliverable`, `Delivery Status Notification`, and Feishu `邮件退信`.

## Bounce Decision and Cleanup Gate

Before applying suppression, check the exact recipient's current and previous `sendrecord` outcomes. If cleanup may already have run, use preserved pre-scan/pre-cleanup evidence because cleanup can delete `sendrecord` and `emaildraft` rows.

Classify every confirmed bounce for reporting, but do not use classification to skip suppression unless the operator explicitly identifies an exception:

- `hard`: 5xx / recipient unknown / no such user.
- `temporary`: 4xx / timeout / temporary resource failure / greylist.
- `sender_or_gateway`: sender-authentication, IP/domain/network/reputation, or recipient policy/gateway rejection.
- `suppression_only`: confirmed bounce with no current Workbench route.

Historical `smtp_status='sent'` is context, not a veto. Every confirmed-bounce bucket suppresses; this rule is not relaxed because the failure appears temporary or sender-side.

For a primary-recipient route: add suppression and BounceRecord; remove the exact route; replace `Contact.email` with an internal invalid placeholder only when it equals the bounced address; remove drafts and send records tied to that exact primary route.

For a CC route: add suppression and BounceRecord; remove the exact route; remove that address from `cc_emails` in related drafts/send records; preserve the primary recipient and its send record.

For a suppression-only target: add suppression and record bounce evidence when available; do not create or alter Contact/Company rows.

Always preserve Contact and Company rows and other valid company routes.

## Sender-Reputation and Recipient-Policy Gate

Apply this gate to label the cause in the report. It does not override the operator's default all-bounce suppression policy.

- `sender_reputation_risk`: label and report the affected sender/gateway, then suppress the bounced route unless the operator exempts it.
- `recipient_policy_permanent`: label and suppress the exact route.
- `temporary_or_greylist`: label and suppress the exact route.
- `ambiguous_policy`: label and suppress the exact route.

For every production write, show the exact addresses, route type (`To`, `CC`, or suppression-only), and decision bucket, then obtain the operator's confirmation. Retain the Company and every other valid route. Also report sender-IP/domain evidence; suppression does not replace sender-side diagnosis.

## Auto-Reply Route Replacement

When an auto-reply says the contacted person has retired, left the company, or is no longer responsible, and it provides a new email/contact route:

- Treat the message as actionable contact-route evidence, not a normal bounce.
- Extract only the explicitly stated replacement email/contact details from the auto-reply. Do not infer new private routes.
- Add or update the replacement contact/route in Workbench under the same company.
- Demote the old contact and old email route from future sending: set the old contact `priority_contact_rank` to `999`, mark the old email route inactive/retired when the schema allows it, and do not keep it as the primary target.
- Keep the old contact row and company row for history; do not delete them.
- Do not mark `Company.status = replied` unless the auto-reply contains a real business response from the company, not just a retirement/out-of-office notice.
- Report old contact/email, replacement contact/email, changed row IDs, and evidence UID/path.

## Output Shape

Use a concise report:

```text
结论: completed / needs confirmation / blocked
环境: test / production
动作: preview / write cleanup / code update
结果: scanned=<n>, bounces=<n>, suppressed=<n>, emails_removed=<n>, contacts_preserved=<n>, companies_deleted=0
证据: <log path, DB backup path, validation counts>
风险边界: <what was not done>
下一步: <single concrete action>
```
