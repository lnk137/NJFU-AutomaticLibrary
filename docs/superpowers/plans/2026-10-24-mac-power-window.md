# Mac power window implementation plan

> **For agentic workers:** Use superpowers:executing-plans in this conversation. Preserve the dirty checkout; do not commit.

**Goal:** Wake daily at 06:55 Beijing, retain system wake through 07:05, and release only this app's assertion after a fresh safe result review.

**Architecture:** A root-owned fixed native helper maintains an owner-tagged one-time wake event; an unprivileged LaunchAgent owns the awake assertion and service recovery. The existing backend exports a private, read-only health/outcome heartbeat. Local root sees real installation and runtime state.

**Tech Stack:** Existing Python/Flask/Vue and macOS launchd, pmset, caffeinate. No added packages.

**Spec:** `docs/superpowers/specs/2026-10-24-mac-power-window-design.md`

## Delivery status
Activated on 2026-10-24 after a native administrator grant in Terminal. Actual launchd/pmset, pause/cancel/rearm and runner fallback/delegation readbacks passed. Physical overnight sleep/wake is not yet exercised.

## Global constraints
- No real school submissions, cancellation, backfill, plan/history/credential changes or physical sleep tests.
- Preserve existing power events, other applications' assertions and global sleep settings.
- Fixed Asia/Shanghai 06:55–07:05 window; display sleep remains allowed.
- Preserve the now-deployed offline inbox, its worker, and public entry.
- Native administrator authentication is required only after a concrete tested installer is ready.

## Resumed execution contracts
- `PowerFiles(base)` owns only private power config/status files; config revision conflicts fail without mutation.
- `decide(wall, elapsed, config, snapshot)` returns `Decision(hold, reason)`; `quiet(wall)` covers 06:59:50–07:00:40 Beijing.
- `PowerService(settings, accounts, store, scheduler, catalog)` publishes bounded read-only backend evidence off scheduler threads. Web `power=` is optional for existing test callers.
- `power_agent.py` is an unprivileged single-instance supervisor; `public_run.py --power-managed` delegates only to an enabled, live, fresh supervisor and falls back otherwise.
- `/api/local/admin/power` GET/PUT and `/api/local/admin/power/install` POST require local root, normal origin/CSRF checks, and fixed validated options. No arbitrary command/path API.
- Keep existing plans, history, authorization, 50 submit workers, and public offline inbox behavior. No commit or physical sleep test.
- Use the task ledger and scoped source checkpoint instead of clean-branch/commit-based helper bookkeeping. One independent security/source review precedes native installation.

## Review focus
- Stale backend, clock jumps, pending/failed/uncertain outcomes retain protection after review.
- A manual stop pauses recovery; an agent crash makes the runner restore legacy protection.
- Repeated installation/ticks cannot duplicate wake events or cancel unrelated events.
- Arbitrary file/shell inputs never reach root execution; installation refuses foreign existing paths.
- Closed lid, missing AC, login/FileVault and unrelated sleep blockers remain visible limitations.

### Task 1: Private state and safe release policy
- [x] Add behavioral tests in `tests/test_mac_power.py` for boundaries, stale/error/busy/missing outcomes, clock jumps, private files and revision conflicts; observe RED.
- [x] Implement `AL-BackEnd/local_app/power.py`: pure policy, private config/status, read-only backend snapshot/service; connect runtime and root-only web API.
- [x] Run focused tests with isolated MongoDB and no external school client.

### Task 2: Unprivileged supervisor and native installation
- [x] Test supervisor assertion acquisition/release, manual pause, bounded recovery, quiet window and fallback behavior using fake processes/clocks.
- [x] Implement `power_agent.py`, `deploy/mac_power_install.py`, fixed native helper and Install/Status commands. Modify public runner and Stop/Start commands for explicit pause/resume and guarded delegation.
- [x] Validate generated launch plists, shell syntax, schedule lifecycle against a controlled fake pmset, and local-root/public/student API boundaries.

### Task 3: Root visibility, regression, guarded activation
- [x] Add `MacPowerPanel.vue` to the root profile with real status, clear lid/AC warnings and enable/pause controls; no public controls. Test rendering and build using existing dependencies.
- [x] Run backend/frontend regressions; review source and native security boundaries. Preserve concise validation evidence and rollback source.
- [x] Load only in an idle window, compare protected collection hashes and verify port owner/health. Request the native OS administrator grant, then verify launchctl/pmset readback separately from overnight execution.
- [x] Clean only task-owned rebuildable temporary artifacts and record free-space change.
