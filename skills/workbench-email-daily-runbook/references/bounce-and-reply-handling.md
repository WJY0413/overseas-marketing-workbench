# Bounce And Reply Handling

Use this reference when the operator asks whether bounced emails were deleted, whether replies were ingested, or whether recipients should be excluded.

## Bounce Handling

1. Inspect bounce source and bounced recipient address.
2. Add or verify suppression for the exact email.
3. Preserve company/contact rows when possible.
4. Remove or replace only the bad route from future sending.
5. Recompute sendable counts after suppression.

## Reply Handling

1. Record the reply source and `connected_email`.
2. Mark the company/contact as replied or not-to-send according to Workbench conventions.
3. Do not suppress a valid business email merely because it replied.
4. Exclude replied contacts from follow-up if the campaign rule says no further sequence.

## Audit Output

- bounced email
- affected company/contact
- action taken
- remaining valid routes
- follow-up eligibility after cleanup
