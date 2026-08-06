# Sendability And Targets

Use this reference when the operator asks how many companies or recipients can currently be sent.

## Required Checks

1. Confirm DB path and queue status.
2. Count target companies/contacts in scope.
3. Exclude already sent recipients by `sendrecord`, not only by `emaildraft`.
4. Exclude suppressed or bounced routes.
5. Exclude rows without a usable email route.
6. Report company count and recipient/email count separately when possible.

## Output

Keep the answer concise:

- `可发企业数`
- `可发邮箱/收件人数`
- `已发送排除`
- `退信/抑制排除`
- `无有效邮箱排除`
- `口径说明`

If the scope is ambiguous, inspect likely current campaign/template context first before asking the operator.
