# Native Campus Contract Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task in the current session. The design and continued implementation direction are already established; no Git commits are authorized.

**Goal:** Deliver and verify an isolated actual-school-protocol candidate for C++ scheduled first send with Python receipt/finite-retry continuation.

**Architecture:** Existing Python `CampusClient` owns authentication, encryption, canonical request construction and record queries. C++ consumes a single-use bounded capsule before due, retains the selected D/WAL/threaded authority, and returns a transport result. Python owns durable result presentation and the established retry/record rules; the running production executor remains unchanged.

**Tech Stack:** Existing Python/requests/PyCryptodome/PyMongo, C++17, system libcurl/SQLite/CLT/OpenSSL, existing isolated Mongo fixtures.

**Spec:** `docs/superpowers/specs/2026-10-25-native-campus-contract-design.md`

## Global Constraints

- No real school request, booking, cancellation, catch-up, dependency installation, Git commit or production configuration change.
- Preserve 24-account concurrency and existing plans, credentials, history, permissions, public UI and running backend.
- Only test Mongo and loopback TLS are accepted by the candidate.
- Keep durable D claims, revocation ACK, leases, version checks, nonreplay and the source payload's original two-second expiry.
- Keep original worktree and prior source/test identities. Log and clean only successfully created task-owned temporary paths with identity checks after owned process groups exit.

## Review Focus

- A changed cookie/token/session or an already consumed/expired payload must not produce an armed request.
- A response may arrive after permissions/plan versions change: persist the actual first send, prohibit further sends, and avoid a false skipped/green state.
- A lost response/claim/storage write must never grant a replay or a second continuation owner.
- A slow warmup/account must not delay healthy accounts; cohort capacity must stay24.
- Timing comparisons must use the same encrypted school contract, TLS/session warmth, receipt matching and cohort accounting.

### Task 1: Canonical request capsule

**Files:** create `experimental/native_executor/campus/request.py`, `tests_request.py`.

**Interfaces:** `capture_request(client, run, seat_id) -> PreparedRequestCapsule`; the immutable capsule exposes sanitized metadata and a private `encode()` frame. `decode_result(status, body, timings) -> Submission` retains current school's strict code/busy/error semantics.

- [x] Test real `CampusClient` request capture, RSA/AES round trip, exact headers/cookies/route, no network POST, one-use consumption and original expiry; observe failure before implementing.
- [x] Implement capture-only canonical construction, frame bounds/controls, safe repr and outcome parity with current `CampusClient.submit`.
- [x] Run focused tests including malformed/expired/mismatched identities and header injection.

### Task 2: Native school-shaped transport

**Files:** create `campus/wire.hpp`, `campus/protocol.py`, `tests_wire.py`; extend isolated executor parsing/route selection and optional subprocess env.

**Interfaces:** versioned length framing under explicit `NJFU_NATIVE_CAMPUS_FIXTURE=1`; `CampusNative` selects that mode for its own child. `CONFIG` reports the wire protocol. Synthetic default behavior is unchanged.

- [x] Test wrong protocol/magic/header/length, fixed route, TLS verification, token/cookie reset between jobs, original expiry and capsule-hash binding; observe failures.
- [x] Implement school fixture wire mode while retaining hardcoded loopback/blocked production ports and D/WAL/threaded guards.
- [x] Run native protocol, durability, revocation, crash/nonreplay and peer regressions.

### Task 3: School-shaped first outcome and retry continuation

**Files:** create `campus/fixture_school.py`, `campus/dispatch.py`, `campus/continuation.py`, `tests_continuation.py`.

**Interfaces:** fixture validates/decrypts canonical body and serves `/ic-web/reserve/resvInfo`; `CampusDispatch` returns the actual first `Submission` and delegates receipt matching to the existing reservation service. A continuation is owned once per persisted native binding.

- [x] Test accepted+matching receipt, HTTP200 without receipt, lost/malformed/refused reply, mismatched record fields, and post-send plan/access change.
- [x] Test busy→wait→fresh query→backup, late receipt/no second POST, query/persist failures, current dual retry floors and bounded exhaustion; compare against existing Python behavior.
- [x] Implement persistence/recovery so native claimed or uncertain runs never replay, and interruption cannot acquire a second continuation owner.

### Task 4: Warmup, scheduling and compatibility composition

**Files:** create `campus/scheduler.py`, `campus/application.py`, `tests_scheduler.py`; add minimal default-preserving hooks to isolated native classes.

**Interfaces:** independent 24-account warmup/refresh, per-payload expiry in job binding, existing state/outbox authority, original Python manual/form path.

- [x] Test slow/failed preflight/warmup,24/25 admission, independent accounts, date/Friday rules, pause/revoke/version changes, wake-like advances and restart classifications.
- [x] Test authenticated configuration→ARM→first send→receipt→status; manual and form operations retain the original booking implementations.
- [x] Run relevant existing native and complete backend regressions, verify production/source/data hashes remain protected.

### Task 5: Interleaved comparison and handoff

**Files:** create `campus/benchmark.py`; extend explicit runner/manifest and document validated limits.

- [x] Compare warmed Python/native on the same TLS fixture and canonical request at1/11/24 accounts with interleaved variants, independent receipt/early/duplicate/miss assertions and cohort statistics.
- [x] Review actual changed source and failures independently; resolve Important findings and retain exact build/source identity.
- [x] Record which activation gates passed or failed. Do not activate production from test counts alone.
- [x] Preserve reports, original samples, red/green and rollback evidence; drain task processes and clean verified temporary paths, measuring free space before/after.
