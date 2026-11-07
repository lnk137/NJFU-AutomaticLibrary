# Offline inbox implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; fresh final review. Preserve the dirty checkout; do not commit.

**Goal:** School-first login with eligible cached-verifier fallback, durable pending plans, automatic local synchronization and account-level monitoring.

**Architecture:** `deploy/inbox_gateway.py` handles public HTTPS proxy/fallback and SQLite receipts; `AL-BackEnd/local_app/inbox.py` exports verifiers and runs the local synchronization worker; existing frontend distinguishes pending from effective state.

**Tech Stack:** Existing Flask, Werkzeug, Waitress, requests, SQLite and MongoDB. Locked server-only runtime is authorized.

**Spec:** `docs/superpowers/specs/2026-10-24-offline-inbox-design.md`

## Global constraints
- No extra real school submission/cancellation/backfill, changes to existing plans/history/access, core upgrades or commits.
- 50 booking workers and existing root/public boundaries remain.
- No school password/key/verifier/session secret in logs, test evidence or browser data.
- Pause Mac power implementation; retain its design and draft tests outside active test discovery.

## Review focus
- School rejection or ambiguous login timeout must not fall back to offline success.
- Revoked/stale/never-verified accounts and old-generation sessions must fail closed.
- Crash after account commit but before acknowledgement must not duplicate or overwrite later edits.
- Slow accounts, release boundaries and offline cloud must not block existing scheduled work.
- Only committed local state can be labelled effective; cached data and pending config stay explicit.

### Task 1: Durable cloud inbox and school-first gateway
- [x] RED/GREEN `tests/test_inbox_gateway.py`: eligibility, generation, TTL/rate/CSRF/ownership, online denial/offline fallback, idempotent queue and acknowledgement leases.
- [x] Implement private SQLite storage and Flask proxy/fallback gateway; reuse the native student API shape, deny all public root paths.

### Task 2: Local synchronization and authentication state
- [x] RED/GREEN `tests/test_offline_inbox.py` with isolated Mongo and fake school: seed verifier, revision/generation checks, atomic commit/readback, crash replay, transient retry, independent accounts and quiet window.
- [x] Connect optional configured worker to runtime/auth events, root-only reporting and continuous automatic startup/reconnection; feature stays absent when unconfigured.

### Task 3: UI, deployment and final verification
- [x] Add responsive student synchronization notices and admin account-level monitoring; test actual rendering and request behavior.
- [x] Run scoped then backend/frontend regressions and H5 build; obtain independent final review.
- [x] Install approved private server runtime, deploy least-privileged gateway/static assets and Nginx with rollback; configure local worker and idle-load using existing protected data.
- [x] Verify public offline mode with isolated account fixtures rather than disrupting production relay; compare production hashes; clean only task-owned temporary fixtures and record disk space.
