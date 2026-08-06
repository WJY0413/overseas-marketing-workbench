# Workbench Bounce Contract

## Paths

- Test copy: `<detected-workbench-root>`
- Production copy: `<detected-workbench-root>`
- Production DB: `<detected-workbench-root>\data\workbench.db`
- Bounce log: `data\bounce_scan.log`

## Tables

- `senderaccount`: SMTP/IMAP identity and encrypted sender password.
- `sendrecord`: proof of real Workbench sends.
- `bouncerecord`: detected bounce log with mailbox UID, recipient, reason, and excerpt.
- `suppression`: emails that must not be used for future sends. The current production schema has `email`, `reason`, and `created_at`; adjustable suppression requires an `expires_at` migration and queue enforcement before temporary rows can be used.
- `contact`: contact/person row. `Contact.email` is the current sending email and cannot be NULL in the current schema.
- `contactroute`: secondary CRM route metadata.
- `company`: company CRM row; must not be deleted by bounce cleanup.
- `emaildraft` and `emailevent`: draft lifecycle and audit events.

## Required Cleanup Policy

When a confirmed bounce is processed under the operator's default all-bounce policy:

1. Add the recipient email to `suppression`.
2. Write or keep a `bouncerecord` row.
3. Remove the bounced email/contact route from future sending. Treat both primary recipients and CC recipients as sending routes.
4. Preserve the `contact` row. If the bounced address is stored in `Contact.email`, replace it with an internal placeholder like `removed-bounce-contact-<id>@invalid.invalid` and set status to `bounced_email_removed`.
5. For a primary-recipient route, remove drafts and send records tied to that exact address. For a CC route, remove only that address from `cc_emails`; preserve the primary recipient and its sent record.
6. Preserve the `company` row, even when this was the only email/contact for that company.
7. Reset any temporary `company.status == "bounced"` back to a normal non-bounced state.

When no current Workbench route exists but a confirmed mailbox bounce supplies the exact address, add suppression-only and preserve all CRM rows. Do not invent a Contact, Company, or route. Do not delete contacts or companies in this workflow. If the operator explicitly asks to delete them, treat it as a separate destructive CRM-cleanup task and require a fresh DB backup plus explicit confirmation.

## Feishu IMAP Notes

- Feishu/Lark IMAP host: `imap.feishu.cn`, SSL port `993`.
- Use existing `SenderAccount.smtp_username` or sender email for login.
- Use the existing encrypted sender password or `password_env`; do not print secrets.
- Select `INBOX` read-only for previews.
- Prefer `SEARCH ALL` then local parsing; Feishu may not match all bounce subjects through IMAP search, especially Chinese `邮件退信`.

## Automatic-Reply Contract

Run automatic-reply classification in the same inbox pass as bounce detection, but keep it distinct from `BounceRecord` unless the message is an actual delivery failure.

| Evidence | Required action |
|---|---|
| Holiday, annual leave, or company closure with a return date | Upsert expiring suppression for the exact route; a company closure covers all active company routes. No date means report only. |
| Retired, left, no longer responsible, or explicit email replacement | Permanently suppress/retire the old route; add the stated replacement contact/route under the same company. |
| Retired/left without a replacement | Permanently suppress/retire only the old route; retain the company as a contact gap. |
| Whitelist, CAPTCHA, anti-spam confirmation, or security challenge | Permanently suppress exact address with `anti_spam_verification_required`; do not click, verify, whitelist, or resend. |
| Ticket acknowledgement or shared-inbox receipt | Record explicit case/reference only; no suppression and no `replied` company status. |
| Forwarding/migration/closure notice without an explicit usable replacement | Do not infer a replacement; report for review. |
| Explicit unsubscribe, privacy, or no-contact request | Apply permanent suppression at the explicitly stated scope. |

### Adjustable Suppression Migration

Before writing any temporary absence row, add nullable `expires_at` to `suppression` and update every queue/filter query:

```sql
suppressed when expires_at IS NULL OR expires_at > current_time
```

An expired row must not block a send; a `NULL` expiry remains permanent. Keep reason, source message ID, company/contact IDs, and timezone in durable audit evidence. Do not represent a temporary absence by deleting and later recreating a permanent suppression row.

## Matching and Validation

- Search `sendrecord.recipient_email` and `sendrecord.cc_emails`, including historical sender aliases when necessary.
- A missing live route does not cancel a confirmed bounce: add suppression-only.
- Classify hard, temporary/greylist, sender/gateway, and policy cases for reporting. Suppress all by default unless the operator explicitly names an exception.

## Validation Queries

Use read-only checks after cleanup:

```powershell
cd <detected-workbench-root>
@'
from sqlmodel import Session, select
from sqlalchemy import func
from app.db import engine
from app.models import BounceRecord, Suppression, Company, Contact, SendRecord
with Session(engine) as s:
    print("bounce_records", s.exec(select(func.count(BounceRecord.id))).one())
    print("distinct_bounce_emails", s.exec(select(func.count(func.distinct(BounceRecord.recipient_email)))).one())
    print("suppression_bounce_like", s.exec(select(func.count(Suppression.id)).where(Suppression.reason.like("bounce%"))).one())
    print("email_removed_contacts", s.exec(select(func.count(Contact.id)).where(Contact.status == "bounced_email_removed")).one())
    print("remaining_bounced_companies", s.exec(select(func.count(Company.id)).where(Company.status == "bounced")).one())
    print("remaining_bounced_sendrecords", s.exec(select(func.count(SendRecord.id)).where(SendRecord.smtp_status == "bounced")).one())
'@ | .\.venv\Scripts\python.exe -
```
