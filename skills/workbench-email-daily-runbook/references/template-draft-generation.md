# Template Draft Generation

Use this reference when the operator asks to generate first-touch or follow-up drafts from a DOCX/template.

## Required Checks

1. Read the template file if the operator provides one.
2. Identify campaign/follow-up step and target scope.
3. Reuse previous CC rules if the campaign already has them; otherwise inspect current Workbench data for company-level route structure.
4. Remove or skip bounced/suppressed emails before rendering drafts.
5. Render selected sender signature from the chosen sender, not a default sender.
6. Generate drafts as `draft` unless the operator explicitly asks to queue them.

## Confirmation Boundary

Draft generation can be done after the target scope is clear. Queueing or sending requires separate confirmation.

## QA

After generating drafts, inspect:

- draft count by status
- at least 3 sample subjects
- sender id/email
- To/CC split
- signature email in rendered HTML
- duplicate recipient warnings
