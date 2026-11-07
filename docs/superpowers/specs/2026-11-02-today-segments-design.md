# Same-day segmented booking

## Approved outcome

The user selected one confirmation for several same-day intervals, each with its own preferred/backup seats. Examples: 09:00–12:00 seat A, 14:00–18:00 seat B, 18:00–20:00 seat C. Adjacent intervals are allowed; overlapping intervals are rejected. Existing daily/weekly plans, date exceptions, credentials, favorites and school reservations are preserved.

## Behavior

- Add/remove interval cards in the existing same-day dialog. Each card has begin/end and 1–5 ordered seats selected through the existing seat library. One interval remains a valid batch; bound requests to seven intervals (the current opening window and minimum two-hour interval admit no more).
- Use Beijing today, future half-hour boundaries, the existing two-hour minimum and Friday closing cap. Normalize the effective intervals before comparing them and show that exact result before confirmation. Reject the whole preview if any interval is invalid, overlaps another interval or conflicts with a school record.
- Preview performs school reads only. Confirmation authorizes this batch once. Each interval is submitted in chronological order and produces a separate manual run/receipt. Only exact known busy/occupied rejections that exhaust the mainline shared candidate policy permit the next interval after fresh checks. An uncertain result, failed readback, revoked access, stale revision, expiry, invalid clock, service shutdown or an impending scheduled trigger stops the remaining intervals, which are explicitly shown as not submitted.
- Database outages during interruption finalization are retried by an independent cleanup worker; it only classifies durable results and stops the batch, never invokes execution again. Local-root account history refreshes after a terminal batch without overwriting the scheduled draft.
- Keep successful reservations when another interval fails; do not cancel, roll back, reschedule or silently repeat a batch. School limits remain enforced by the school response. Preserve every existing run matching and at-most-once safeguard.
- If a later explicit school-record query/reconciliation verifies a stopped interval's receipt, GET/latest and duplicate-confirmation views project that exact bound manual run's success without changing the persisted batch or submitting later intervals. A reopened saved draft, including an unapplied seat picker, keeps its times/seats until the user explicitly discards them.
- Busy/occupied retries for a manual run may coexist with known-active, verified-identity school records whose valid intervals do not overlap that run. Exact receipts still win; conflicting, unknown-status, malformed or wrong-identity records still stop retries. Scheduled retry behavior stays unchanged.

## Implementation boundaries

Introduce a focused batch coordinator and a focused frontend dialog/editor rather than growing the scheduled-plan editor. Keep the existing scalar today endpoints and personal-form behavior compatible. New endpoints under `/api/local/accounts/<id>/today/batches` provide `POST /preview`, `POST /submit`, `GET /<batch_id>` and `GET` for the latest batch. All use the existing ownership/CSRF/session/offline restrictions.

Preview returns `{batch_id,date,segments:[{index,begin,end,seats}],expires_at,preview_token}`. Submit and status return `{batch_id,date,state,message,segments:[{index,begin,end,seat_ids,status,message,run_id,result}]}`; latest returns `{batch: value_or_null}`. Batch states are `running`, `completed`, `stopped`; interval states are `pending`, `running`, `success`, `failed`, `uncertain`, `skipped`, `not_submitted`.

Use signed, short-lived previews, deterministic per-interval operation IDs and a dedicated Mongo batch document claimed before school writes. Execute in a bounded background worker so HTTP timeouts cannot authorize a second batch. Duplicate confirmations return the durable existing state. Polling and reopening read status only. After a restart, interrupted batches remain stopped, and no remaining interval is replayed. The account/revision/access/live/clock and time-window checks are repeated before each interval. Keep the protected preparation window around scheduled triggers free of this new manual work.

## Validation

Use fake school identities, a temporary standalone Mongo, the worktree dependency symlinks and random loopback ports. First reproduce new behavior with failing tests; then verify success/adjacency/overlap, per-interval backups, duplicate/concurrent confirmation, partial failure, uncertain stop, stale/expired/foreign previews, record changes, restart, CSRF/ownership/offline protections and unchanged saved plans/date exceptions/favorites. Verify frontend draft ownership, late-response protection, one confirmation, polling/reopen without POST replay and desktop/iPad/phone layout in a real isolated browser. Run relevant suites, the worktree full Python/Node suites and H5 build. Inspect the incremental diff and perform one bounded independent review.

No production login/booking, account/plan mutation, service restart, deployment, dependency install, Git commit/push or worktree removal is authorized by this development increment. Mainline has later executor/retry changes; preserve it and deliver an incremental patch for a later scoped integration.
