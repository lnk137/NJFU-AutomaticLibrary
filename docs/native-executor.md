# Native deadline executor

The existing start/stop commands, campus authentication, public inbox, account permissions, plans, school receipts and manual booking remain the same. Scheduling capacity is 20 accounts after the updated backend starts. Python owns authentication, encryption, business policy, receipt matching and bounded continuations; C++ owns the scheduled first HTTP request at the release deadline.

## Installation and selection

Reuse the existing project Python, macOS Command Line Tools, system libcurl/SQLite and installed frontend dependencies. There is no dependency download at startup. Build explicitly with `local_app.native.build.build_native(root)`; it records the exact C++ source and executable hashes under `.runtime/native-executor/`.

After isolated acceptance, `local_app.native.selection.select_native(root, data_dir)` creates the private `.local-data/native-selection.json`. It pins the complete Python adapter, launchers, C++ sources and compiled binary. Existing launchers and the wake agent read that selection on every backend start. A mismatched or malformed selection disables execution; it never silently starts another sender.

`.local-data/native-state.json` independently binds the persistent native ledger and management outbox identities. The first installation requires no earlier native directory or Mongo ownership evidence. Later starts only open the retained state. Missing files, identity mismatches, corruption or epoch rollback require a controlled repair; automatic recovery and the root recovery button cannot discard that evidence. Keep these files with the existing application data; do not delete them as a troubleshooting step.

## Seat-specific rejection recovery

The exact school replies `code=1 / 当前设备正在被预约，请稍后重试` and `code=1 / 设备在该时间段内已被预约` permit continuing to another configured candidate. A confirmed occupied seat is excluded only for the current task. Untried candidates keep priority, then busy candidates may be tried again, with at most two attempts per seat and six actual submissions overall. Before every subsequent submission, retain the existing pacing and complete self-query; matching receipts, uncertain/query failure, permission changes and rate-limit/other school rejections stop further submissions. Python/manual/form and native-first continuations share this policy. Saved seats, plans and historical records are unchanged.

## Runtime recovery

- Each release independently refreshes the authenticated payload and prewarms an idle native TLS worker. One slow or failed account does not hold other accounts' first requests.
- Ordinary sleep/wake uses the continuous clock and nudges inbox synchronization. Real wall-clock steps revoke sends; bounded trusted public time validation happens outside the release quiet window.
- Temporary connection/readback failure retains the actual engine with its gate closed, then retries at1/3/10/30 seconds. Recovery revokes sends, drains owned work, validates persistent identities/epochs before opening a new process, and classifies old results by readback.
- Proven-unclaimed future work may receive a fresh payload and ownership binding. Claimed or uncertain work only queries school records. Missed release windows are not backfilled.
- A manual root pause survives reconnect/restart. In the local root dashboard, “恢复连接” preserves that pause; “继续自动预约” explicitly resumes future enabled plans.
- More than 20 eligible accounts use the existing Python queue bounded to 20 concurrent submissions. A late extra account cannot take a native-owned task. Explicit Python rollback also refuses native-owned work.

The Mac must be awake with campus connectivity at release. The existing06:55 wake/awake protection remains in use; closing the lid, power loss or unavailable campus connectivity can still prevent execution. Isolated benchmarks do not establish school server latency or promise a successful real booking.

## Rollback

Use an idle window, revoke/stop the current sender and preserve all application data. An explicit private selection of `{"schema":1,"executor":"python"}` chooses the retained Python path in the new safety-aware Runtime. Interrupted native ownership remains uncertain and cannot be submitted by Python. Do not remove native ledgers/anchors or use a missing selection as rollback. Old source restoration is only safe after independently proving no native work can have been sent.

## Acceptance

Run backend tests with a private test database and loopback school fixture, and frontend tests/build with the already installed environment. Native browser and performance acceptance require explicit evidence directories and are skipped by the normal unit run. No real school booking, cancellation, or catch-up is part of acceptance. Preserve source/build identity, test results, rollout hashes and a checked rollback copy before activation.
