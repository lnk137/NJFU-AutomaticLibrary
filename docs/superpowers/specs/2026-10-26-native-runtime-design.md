# Native runtime integration design

## Intent and authorization

Continue the approved C++ candidate into the actual existing Runtime, retain the current login/public inbox/manual/form/dashboard/power behavior, and make the24-account executor safe across ordinary wake, reconnect and restart. The user requested continued inline implementation; routine architecture and reversible source integration proceed without another approval round. No extra real booking, cancellation or catch-up; credentials/plans/history and permissions remain authoritative in their original Mongo collections.

## Approaches

Directly importing the experiment is unsafe: it installs process-wide network guards and assumes synthetic database paths. Reimplementing authentication/crypto/business policy would duplicate the riskiest rules. Chosen: promote the audited minimal native core into `local_app/native/` and a fixed-purpose C++ source directory. Leave prior experimental source/evidence immutable. Python keeps school authentication, payload construction, self-query, bounded busy continuation, manual/form paths and UI. This extraction and component layout are implementation decisions.

The default executor remains Python until the candidate is validated. A private executor selection file survives launcher/power-agent restarts. A controlled idle rollout may select C++, after preservation checks and an independent final review. If any activation gate cannot be demonstrated, retain the healthy current service and report the specific gate.

## Process and transport

The production library imports no experiment/test modules and changes no global sockets, environment or `sys.path`. Existing macOS CLT, libcurl/SQLite, project Python and installed dependencies are reused. The build produces a source/binary hash manifest. The production target is fixed `https://libseat.njfu.edu.cn`; redirects/proxies and arbitrary paths/endpoints are forbidden, with peer/hostname TLS verification. The same executable has an explicit private loopback fixture endpoint accepted only by test injection; production configuration cannot select it. FAULT/debug RPCs and benchmark variant overrides are unavailable in production.

Native handles must be warmed every release, not merely at application startup: a resident process can otherwise hold stale overnight TLS connections. Reserve an idle worker during independent per-account prewarm, send a bounded unauthenticated fixed public-config GET, and bind that fresh worker to the later ARM. Slow preparation is per-account; keep24 scheduling capacity. Bodies/Cookie/token never enter SQL/logs. Preserve original two-second payload expiry, D/WAL/EXTRA/fullfsync, identity/version/lease checks, acknowledged revocation and nonreplay. Python fallback for oversized cohorts remains bounded and cannot acquire native-owned work.

A parent-death watcher terminates orphan native children; persistent claims survive. Missing/corrupt/wrong-owner state is never automatically reset. Build/init is explicit and verified; shutdown fences sends before draining already-sent results. The UI remains available in a visibly disabled executor state if startup is safely contained; no silent Python send fallback after native ownership exists.

## Authoritative state and existing workflows

Wrap account/access mutations with durable management intents and native gate ACKs before applying Mongo changes. No new business database or password copy. Inbox CAS/idempotence/expiry, root access changes, account revisions, date exceptions and manual/form paths use the same wrapper. Changes to a new/unrelated student must not globally invalidate healthy armed accounts. Known metadata-only writes can bypass fences; all unknown authority writes fail closed. Preserve existing quiet windows and version-before-release policy.

Runtime owns exactly one scheduler and one native engine under the existing app lock. Recovery classifies native claims before any send. Claimed/ambiguous old work becomes uncertain and is queried only; provably unclaimed future work may be re-prepared with current permission/version/session, never catch up a released task. Normal sleep advances the same continuous clock and automatically resumes heartbeat/discovery/inbox work. A real wall-clock step first revokes send permission; only stable clock evidence and a bounded verified public HTTPS Date check (no account or booking POST) can restore it; the root API reuses that validation. Storage identity/corruption and unresolved claims cannot be cleared by resetting files.

Transient native/control/DB failures retry recovery with bounded backoff and visible state, without blocking healthy accounts for account-specific school errors. Native process restart reuses the same state and rechecks identity/epoch. All state transitions, preflight/result futures and shutdown are serialized/drained before re-enabling. Manual operator pause must remain paused after recovery.

## Status and UI

Expose concise executor state, last successful recovery and actionable error via existing status/dashboard/power snapshots; no secrets, raw packets or filesystem/credential internals. Root can see executor diagnostics and invoke local recovery; student/public status remains filtered. Preserve current compact green UI, desktop/iPad/phone layout, chart timing semantics(v2 Python/v3 native), all existing actions and power06:55–07:05 policy. Public static deployment is independent; only actual publication/readback proves it updated.

## Validation and rollout

Use real private Mongo and independent loopback TLS fixture that validates canonical CAS/RSA/AES packets and matching receipts. Test import isolation, fixed TLS endpoint, per-release warmup,24/25/late overflow, authority/inbox/revocation/version edits, busy/partial/lost replies, shutdown, parent death, restart/nonreplay, normal wake and clock steps, malformed persistence, root/student API isolation, manual/form/Friday/date exceptions. Run current baseline and relevant full backend/frontend/native regressions, plus an interleaved first-send comparison on the promoted implementation. Physical wake/school acceptance remain separate evidence, never inferred from fixtures.

Before rollout, verify actual idle state and distance>600s from every saved trigger, no active forms/runs/busy accounts, current clock/health, and exact source/config/plan/history hashes. Compile/build beforehand, retain a verified rollback source/binary/selection manifest, then perform one controlled managed-launcher restart while preserving its database/configuration (the existing supervisor owns backend, Mongo and tunnel together). Verify actual selected executor, healthy status, same plans/history/permissions, public page/API and subsequent heartbeats; do not test by sending a booking. Preserve legitimate concurrent user authentication/config updates rather than restoring stale snapshots. Clean only task-owned disposable worktrees/build fixtures after results and necessary evidence are preserved.
