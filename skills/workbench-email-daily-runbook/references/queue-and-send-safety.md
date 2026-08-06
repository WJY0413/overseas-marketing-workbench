# Queue And Send Safety

Use this reference before any send readiness, queue start, pause, or app process troubleshooting.

## Always Inspect First

- Running app process and port.
- `appsetting.queue_paused`.
- Count by `emaildraft.status`.
- Any `sending` rows.
- Latest `sendrecord` timestamp and count.
- Sender account status if available.

## Pause First Rule

If the operator reports unexpected sending, signature mismatch during queueing, or duplicate app processes, pause the queue first. Pausing is allowed as a safety action.

## Resume/Start Boundary

Starting or resuming sending always requires explicit confirmation that includes:

- target scope
- draft count
- sender account(s)
- template/follow-up step
- queue status
- suppression/bounce status

## Report Format

Use:

- `当前状态`
- `风险`
- `需要你确认的发送动作`
- `我不会自动启动队列`
