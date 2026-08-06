# Draft And Queue Operations

Use this reference when the operator asks to check, delete, approve, queue, unqueue, or otherwise manage drafts.

## Read-Only Draft Inspection

Inspect before changing anything:

- draft ids and count
- status distribution: `draft`, `approved`, `queued`, `sending`, `sent`, `failed`, `cancelled`
- campaign/template/follow-up step
- sender id and sender email
- subject and recipient samples
- scheduled time, if present
- latest `sendrecord` overlap

Preferred helper:

`python scripts/draft_queue_admin.py --action inspect --status queued --follow-up-step 1`

## Delete Drafts

Deleting production drafts is a DB write. Before deletion:

1. Show the exact filter and expected count.
2. Get the operator's confirmation unless the current instruction already names an exact disposable draft set.
3. Delete only unsent drafts. Never delete `sendrecord` as part of draft cleanup.
4. Do not create a backup by default. Back up only for version updates, data migrations, or the operator's explicit backup request.

Preview first:

`python scripts/draft_queue_admin.py --action delete --status draft --follow-up-step 1`

Apply only after confirmation:

`python scripts/draft_queue_admin.py --action delete --status draft --follow-up-step 1 --apply`

## Queue Or Unqueue Drafts

Queue writes require exact scope confirmation.

- To queue: verify `queue_paused=true`, sender readiness, suppression, duplicate recipients, and signature preview first.
- To unqueue: prefer moving unsent `queued` rows back to `draft` or `approved` according to existing app behavior.
- Never alter rows already `sent`; use `sendrecord` for proof and reporting.

Preview unqueue:

`python scripts/draft_queue_admin.py --action set-status --status queued --follow-up-step 1 --to-status draft`

Apply only after confirmation:

`python scripts/draft_queue_admin.py --action set-status --status queued --follow-up-step 1 --to-status draft --apply`

## Sample Output

`当前有 112 封 queued 草稿，队列已暂停。未发现 sending。按你的确认，我可以把这些 queued 改回 draft，或只删除 step=1 的未发送草稿。`
