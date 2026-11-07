# Native campus protocol candidate

## Intent and constraints

Continue the reviewed native candidate toward an actual-school-compatible
scheduled first send. Preserve all existing account permissions, encrypted local
password storage, plans, history, Friday/date-exception rules, manual/form booking,
bounded busy retries and the user-requested 24-account concurrency ceiling.
Do not contact the school or submit/cancel/backfill a real booking during this
work. Keep the running production Python executor and its source unchanged.

This stage delivers a complete isolated candidate for the school request/receipt
contract and scheduled first-send/retry integration. Production activation remains
gated by the completed compatibility and interleaved performance evidence; fake
acceptance is never reported as a real school booking. No dependency installation,
Git commit or production configuration edit is part of this stage.

## Approaches

- Reimplement school authentication, crypto and all booking business rules in
  C++: a large second implementation with substantial compatibility risk.
- Replace only synchronous Python HTTP calls: smaller, but the selected native
  authority requires advance ARM and a durable due-time claim, so an at-time RPC
  would add latency and discard the main benefit.
- Selected implementation approach: retain Python's existing school client for
  login, request construction, encryption and self-query; export a single-use
  prepared request to C++ before due. C++ keeps D/WAL/threaded timed first send.
  Python persists the returned first outcome, confirms actual school-shaped
  receipts, and performs the current bounded busy/alternate-seat continuation.
  Manual and form bookings keep their current Python paths.

The user approved continuing the protocol/compatibility work. The detailed
hybrid architecture above is the implementation choice for that work, not a
claim that the user specified these internals.

## Request boundary

A request capsule carries only the prepared reservation body and the exact
allowed school headers/cookies required for `POST /ic-web/reserve/seat`. The
body is produced by the existing `CampusClient.submit` through a capture-only
transport; no POST escapes capture. The original cached first payload is consumed
once and its original two-second lifetime is retained. Login passwords do not
enter the capsule. Header values and ciphertext stay in private memory/IPC and
are excluded from repr, persistent diagnostics and SQL state.

Use a bounded versioned length-framed capsule. C++ validates allowed header names,
header controls/lengths, complete frame/body lengths and the fixed route before
ARM. Hash the entire capsule, including auth headers, into the native binding.
Never follow redirects, reuse cookies across accounts, disable TLS verification,
or send a capsule to the synthetic `/send/<job>` route.

The candidate still uses an explicitly selected campus-protocol loopback TLS
fixture. The host/path, token/cookie and JSON shapes match the current school
client, while the network endpoint remains hardcoded loopback. No production
network switch is introduced merely to make tests pass.

## Scheduling and outcomes

Reuse native discovery, bounded preflight retries, cohort admission, private
management outbox, leases, durable dedupe, final version/permission checks and
revocation ACK. Add independent 24-account payload warmup/refresh before ARM.
Each job's expiry must not extend the source payload lifetime.

The native first result is persisted even if a plan or access changes after the
send. HTTP success alone cannot mark success. Reuse the school client's strict
JSON/code semantics, exact busy rejection and active status 1027, plus the
existing whole-record student/seat/day/time match. Unknown/malformed/transport
outcomes remain uncertain; no automatic POST replay.

Only a confirmed busy rejection can enter the existing bounded retry order.
Honor both current interval floors; query the target day's own records after the
wait and before every additional POST. A late matching receipt, any other record,
query/persistence error, clock/live/access/version change or expiry stops further
POSTs. The continuation is owned once and cannot run concurrently or after
restart. Recovery uses persistent native claims to classify runs as uncertain or
provably unclaimed; it never guesses that a send did not happen.

## Compatibility and validation

Build an independent TLS school fixture that decrypts the actual RSA/AES fields,
checks token/cookie/account identity, creates school-shaped paginated records,
and injects explicit busy/rate-limit/refusal/malformed/dropped responses. Use only
synthetic credentials, private Mongo databases and task-owned runtime paths.

Tests must cover capsule lifetime/single-use/auth isolation/injection, no early
send, receipt matching and uncertain nonreplay, busy backup continuation and
late receipts, state edits/revocation, wake-like clock advances, restart/claim
classification, bounded preflight failures, and 24/25 admission. Existing native
and backend regressions remain required. Manual/form paths must still run through
their original implementations.

Compare Python and native first sends on the same TLS fixture and encrypted
request contract with interleaved variants, warmed sessions, 1/11/24 accounts,
whole-batch statistics, matching receipts and no early/duplicate/missed sends.
Report local send and fixture receive boundaries separately. Native activation
requires correct behavior and a defensible non-regression result, rather than a
test-count or one favorable microbenchmark.

## Files and isolation

New code lives under `experimental/native_executor/campus/`; only explicit
test-only extension hooks may change the already isolated native core. Preserve
the original worktree and the previous integration manifest/evidence. Reuse the
existing project Python, CLT, libcurl/SQLite/OpenSSL and Mongo fixture.
Record exact source/build identities and preserve review/test/rollback evidence.
Clean only proven task-created directories after their owned process groups
exit, with device/inode checks for retained fixture cleanup.
