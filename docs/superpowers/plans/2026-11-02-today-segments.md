# Same-day Segmented Booking Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for the coupled increment. A scoped implementation helper may handle the backend under superpowers:subagent-driven-development; no commits or production changes. User approved the one-confirmation design in this chat.

**Goal:** Book several non-overlapping same-day intervals with independent seats after a single confirmation.

**Architecture:** Keep scalar today/form endpoints compatible. A dedicated durable batch coordinator delegates independent manual runs to the existing executor, and a focused frontend dialog shares the existing seat library. Signed previews, batch claims, stable per-interval IDs and read-only polling prevent replay.

**Tech Stack:** Python/Flask, existing PyMongo/Mongo fixture, Vue/Uni-app, Node test runner and current H5 build. Reuse installed environments.

---

### Task 1: Freeze boundaries and establish baseline

- [x] Inspect dirty main/worktree state, existing today checks/executor/UI/tests and later mainline drift.
- [x] Save the approved short spec and the exact small source backups/hash manifests under `development/work/today-segments-20261102/`.
- [x] Check existing Python/Node/Mongo executables and run today/recovery/API ownership plus account-page baseline tests. Retain exit status and logs.

### Task 2: Durable backend coordinator

Files: add `AL-BackEnd/local_app/today_batches.py`, a focused record-window helper if needed, and `tests/test_today_batches.py`; minimally change `today.py`, `reservations.py`, `runtime.py`, `web.py`.

- [x] Write API tests first, using the existing real isolated Mongo/runtime and a fake school that records `(begin,end,seat)` submissions. The first missing batch preview endpoint must fail with 404 rather than a fixture/setup error.
- [x] Validate 1–7 effective half-hour intervals and chronological adjacency before any submit. Add the signed preview and account-scoped batch APIs in the approved spec.
- [x] Claim a durable batch before worker dispatch and use stable IDs such as `uuid5(NAMESPACE_URL, batch_id + ':' + str(index)).hex` for every manual run. Repeated/concurrent confirmation returns existing state and never reschedules work.
- [x] Before every interval recheck account revision, access, expiry, actual date/start, execution guard, pending tasks and fresh school records. Use the existing live gates; reject work around scheduled trigger preparation. Stop and label unsubmitted intervals when evidence becomes uncertain or a safety check fails.
- [x] Keep only narrowly justified manual record-window allowances in `_retry_readback`; all scheduled and malformed/unknown evidence cases retain the stop behavior.
- [x] Add restart/shutdown and cross-account/CSRF/offline tests, partial clear failure and uncertain stop tests, and exact unchanged plan/date/preferences assertions. Run `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest discover -s tests -p 'test_today_batches.py' -v` and the original today/recovery/booking/auth suites to green.

### Task 3: Focused frontend dialog

Files: add `AL-FrontEnd/components/TodaySegments.vue`, a small controller/helper in `services/`, new frontend tests; minimally adapt `account_info.vue`, `local_api.js` and draft persistence as needed.

- [x] Write controller/render tests for segment addition/removal, adjacency/conflict, account-owned drafts, invalidating a preview on edits, one batch confirmation, read-only status polling and late account/session responses. Observe the feature-missing failures.
- [x] Render compact interval cards, ordered seats through `SeatLibrary`, one effective preview and one final confirmation button. Show per-interval state and why remaining intervals were not submitted.
- [x] Integrate with the existing today entry and preserve unrelated scheduled/account/date drafts. Restore/read the latest durable batch on reopening without automatically POSTing it again. Keep errors and unsaved state visible across desktop/phone layouts.
- [x] Run the targeted frontend suites and H5 build using existing `node_modules`.

### Task 4: Acceptance and closeout

- [x] Run the complete worktree Python and Node suites once the related checks pass; rerun only if new changes/failures justify it.
- [x] Start a task-owned fake-school UI fixture at a random loopback port. Verify multiple segments, real seat picking, a single confirmation, per-interval results and reopening; inspect desktop/iPad/phone widths and browser errors. Stop the fixture and remove its exclusively owned temporary Mongo/vault paths.
- [x] Review the incremental source diff, new failure paths and protected mainline source hashes. Complete a bounded independent spec/code review and one necessary fix pass with regression tests.
- [x] Save the final source identity, incremental patch, validation report and retained evidence. Delete only this turn's unused reconstructable build/scratch artifacts, measure disk space and keep the managed worktree.

Commands use the explicit worktree `/Users/a0000/.codex/worktrees/library-date-exceptions/图书馆自动预约`. The source snapshots and recorded source manifest provide the diff baseline; Git HEAD alone does not attribute this increment because much of the inherited app is untracked.
