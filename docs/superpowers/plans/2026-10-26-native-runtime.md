# Native Runtime Integration Plan

> **For agentic workers:** Use superpowers:executing-plans inline; no commits/push or per-task implementer agents. One fresh whole-candidate reviewer at the end. Prior user authorization covers continuing the established design and recommended method.

**Goal:** Integrate the verified C++ first-send core into the existing Runtime with safe wake/reconnect recovery, authoritative inbox/config fences and complete status; activate only after preserved-data rollout gates pass.

**Architecture:** Pure `local_app/native/` library + fixed-purpose C++ binary; existing Python business services remain owners of authentication/crypto/receipts and all UI/manual/form flows. A private mode selection feeds existing launchers and the power agent, with an explicit safe rollback.

**Tech Stack:** Existing Python/requests/PyMongo/PyCryptodome, C++17/system SQLite/libcurl/CLT/OpenSSL, installed Uni-app/Vue build and private Mongo/TLS fixtures.

**Spec:** docs/superpowers/specs/2026-10-26-native-runtime-design.md

## Global Constraints

- Preserve24-account capacity, current plans/permissions/history/credential storage and public entry.
- No extra real booking/cancel/catch-up, dependency installation, Git commit/push or unreviewed production reload.
- Work in the isolated Git checkout; shared environments/prior experiment/deploy source are read-only.
- Keep D/WAL/EXTRA/fullfsync, original two-second expiry, ACKed fences, single-use ownership and uncertain nonreplay.
- Keep source/build/test/rollout evidence; clean only verified task-owned temporary paths after owned processes stop.

## Review Focus

- A resident overnight native connection is stale: per-release warmup must happen before ARM without one slow account blocking peers.
- Recovery must distinguish proven-unclaimed future work from possibly sent work, and never auto-enable after operator pause or ambiguous identity/storage failure.
- Inbox/root/new-student writes must fence exactly affected identities, including CAS conflicts, without dropping unrelated armed tasks.
- Shutdown/restart/failed initialization must neither orphan an authorized sender nor delete persistent dedupe or current user changes.
- Status and v3 timing must be truthful across root/student/public/power views, not just an isolated green test flag.

### Task1: Pure production protocol, build and transport lifecycle

**Files:** `AL-BackEnd/local_app/native/{__init__,protocol,build,request,continuation}.py`; `AL-BackEnd/native_executor/{executor.cpp,authority.hpp,profile.hpp,wire.hpp,peer_threads.hpp}`; `tests/test_native_transport.py` and support fixture.
**Interfaces:** `NativeProcess(runtime_dir,binary,*,endpoint=None,count=24,initialize=False)` exposes existing ACKed state/ARM/JOB/result protocol plus `worker.warm(budget_ms)`; `build_native(root,output)` returns a source/binary identity. Fixed production endpoint and explicit test-only loopback injection.
- [x] Write and observe failing import-isolation/build/endpoint/TLS/warmup/parent-death/state-reopen tests.
- [x] Promote the audited core without test imports/global monkeypatches; fixed variant and no production FAULT/debug commands; verify with actual C++ and TLS fixture.
- [x] Validate original body/session/expiry parity and retained durable state after process death; keep earlier experimental snapshot intact.

### Task2: Existing Runtime authority and scheduling composition

**Files:** `native/{outbox,management,dispatch_base,dispatch,discovery,scheduler,engine}.py`, `runtime.py`, `config.py`, `tests/test_native_runtime.py`, `tests/test_native_authority.py`.
**Interfaces:** Runtime chooses `executor='python'|'cpp'` from validated private selection; NativeEngine provides `database`, `attach(accounts,store)` and lifecycle. Existing account/access collection API is preserved, with exact mutation fences.
- [x] Write/observe failing actual Runtime login→config→native release→receipt tests, per-release warm worker binding,24/25, inbox CAS and unrelated-account authority tests.
- [x] Promote existing dispatch/scheduler semantics and add independent warm reservations without deadline IO; keep manual/form paths and preserved original data.
- [x] Verify startup/close failure containment, unavailable state and no implicit Python fallback for native-owned jobs.

### Task3: Wake/reconnect/restart recovery

**Files:** `native/{engine,scheduler,protocol,outbox}.py`, `tests/test_native_recovery.py`.
**Interfaces:** automatic recovery state machine reports ready/recovering/blocked; read-only classification precedes any enable. Reuse existing power/inbox components and their quiet windows.
- [x] Fail then pass normal continuous-clock sleep, true clock step/trusted Date validation, operator-pause persistence, transient channel/DB faults and process-crash cases.
- [x] Reopen same ledger, refresh epoch identity, permit only proven-unclaimed future rebind with a fresh payload; claimed/lost results remain read-only uncertain.
- [x] Verify one slow/error account does not pause peers; drain all prep/result futures and reconcile outcomes before restoring control.

### Task4: Existing UI and administrator diagnostics

**Files:** `web.py`, `dashboard.py`, `inbox.py`, `AL-FrontEnd/services/scheduler_status.mjs`, `components/{SchedulerStatus,AdminDashboard}.vue`, related backend/frontend tests.
**Interfaces:** additive sanitized executor status in existing views; local-root-only recovery API; public/student filtered status. Preserve source-version timing boundaries and current responsive controls.
- [x] Fail then pass state/permission/filtering/timing UI tests; keep concise actionable error/recovery indicators.
- [x] Build H5 with installed dependencies and inspect desktop/iPad/phone; do not add implementation detail to student flows.

### Task5: Full verification, independent review, guarded rollout and cleanup

**Files:** evidence/identity/rollback/report under development; minimal existing launcher/config integration as required.
- [x] Run full backend/frontend/native compatibility and interleaved promoted-core comparison; inspect every result.
- [x] One fresh final reviewer checks whole candidate; reproduce/fix Important findings then rerun affected full suites.
- [x] Promote only baseline-matching source, preserving current legitimate user writes; prepare build/static/rollback before any production reload.
- [x] If source/runtime/clock/idle/distance>600s/data/permission/endpoint gates pass, select C++ and restart the existing managed launcher in the idle window (its supervisor owns backend/Mongo/tunnel together). Verify real selected executor and health/public/API without a booking; otherwise preserve healthy Python and state exact blocking evidence.
- [x] Preserve evidence and source; stop owned processes and clean only verified temporary work/build paths. Record disk availability and leave no disposable duplicate checkout after promotion.
