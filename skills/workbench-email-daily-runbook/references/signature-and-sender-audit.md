# Signature And Sender Audit

Use this reference when the operator says a draft preview, sent email, or signature looks wrong.

## Required Comparison

Compare all three layers:

1. Draft row: sender id/email fields.
2. Rendered preview HTML: visible signature email/name/company.
3. Sent proof: `sendrecord` sender/account fields and provider message metadata if available.

Do not conclude from draft preview alone. The UI can render a default sender while the send path uses the correct sender.

## Common Checks

- Template rendering function used by preview.
- API endpoint used by UI preview.
- Sender selector value in the browser.
- Fallback sender logic.
- Whether app process was restarted after code change.

## Output

Say whether the problem is:

- preview-only display bug
- send-path bug
- stale UI/process issue
- database sender assignment issue

If fixing UI/code, back up the exact files first and verify with multiple sender ids.
