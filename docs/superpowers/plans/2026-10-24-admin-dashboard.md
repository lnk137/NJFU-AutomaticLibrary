# Admin Dashboard Implementation Plan

Goal: Deliver the approved local-root operations dashboard and improve the existing management workflow without school requests or data changes.
Architecture: A bounded read-only Flask endpoint, a focused aggregation module, independent Vue/SVG dashboard components, and session-safe API/polling helpers.
Tech Stack: existing Python/Flask/PyMongo + Vue/Uni-app/Node; no new dependencies.
Spec: ../specs/2026-10-24-admin-dashboard-design.md
Execution: Native implementation in this conversation as expressly authorized by the user. Source checkpoints replace commits; no commit/push/reset, worktree or dependency installation. Evidence uses development/work/njfu-admin-dashboard-20261024 (existing project convention).

## Global constraints

Local root only; dates Asia/Shanghai, days 7/14/30 default14, kind all/scheduled/manual defaultscheduled. Count complete bounded history, max5000 tasks/1000ms reads, no truncated statistics. Three charts, missing values stay missing. No changes to scheduler, school submissions, secrets, plans or receipts. Preserve existing dirty files and task-external design-preview/.

## Review focus

- A missing first attempt timestamp must not use a later retry.
- A delayed old 401 must not log out a new session or delete its Cookie.
- Polling and filter changes must not overlap requests or reset root note drafts.
- Missing samples, manual tasks and empty dates must not become zero-time or failed scheduled tasks.
- Read failure/over-limit must keep old results visibly stale, not show partial fresh statistics.

## Task 1: Read-only metrics and endpoint

Files: create AL-BackEnd/local_app/dashboard.py and tests/test_admin_dashboard.py; modify web.py.
Interface: build_dashboard(store,catalog,params,*,now=None,runtime=None)->dict; GET /api/local/admin/dashboard calls it only after admin().
- [x] RED tests with real isolated Mongo and fake school: day/kind/timezone/counts, >100 records, exact first attempt, invalid/absent timing, nearest-rank, Friday/weekly readiness, bounds/timeouts, no writes or school calls. API401/403/noCookie-delete/no secrets.
- [x] Implement bounded projections and strict params, sanitize output whitelist, upcoming plan calculation.
- [x] Relevant tests green.

## Task 2: Safe polling and root sessions

Files: create services/admin_dashboard.mjs and tests/frontend/admin_dashboard.test.mjs; modify local_api.js and tests/frontend/session.test.mjs.
Interfaces: createDashboardLoader({fetch,identity,changed}) exposes state/load/stop; chart helpers derive stacked columns, gapped paths and duration labels. API auth generation guards old callbacks/retries; explicit logout still clears Cookie server-side.
- [x] RED single-flight/latest filters/error retention/stop-new identity tests and old401 after login.
- [x] Implement pure controller and API generation checks, add dashboard API helper.
- [x] Node tests green.

## Task 3: Root dashboard and management UX

Files: create components/AdminDashboard.vue, AdminCharts.vue; modify pages/admin/admin.vue and components/SessionBar.vue.
Interfaces: dashboard active prop and open-account event; data follows Task1 JSON and Task2 loader.
- [x] Actual Vue render tests for counts/empty samples/charts/alerts/permissions.
- [x] Dashboard first tab, user management and audit tabs retained. 30sec read-only polling stops on hide/logout; click/keyboard chart details, filters, phase details, responsive root table/cards.
- [x] Improve root search/status filters/counts/note draft preservation and named disable confirmation; no new destructive/bulk operations.
- [x] Green frontend tests and isolated H5 build; browser desktop/mobile acceptance.

## Task 4: Review, idle activation and closeout

- [x] Full backend and frontend suites; git diff --check and source-based diff review. Fresh read-only reviewer checks spec, scoped source changes and validation evidence.
- [x] Hash all protected production collections; no active runs and >10min before next release. Graceful restart and atomic frontend swap, retain rollback.
- [x] Local root live read-only dashboard + public student boundary checks; all original collection hashes and 9 plans unchanged. Screenshots saved.
- [x] Update README/report/ledger; clean only task-owned disposable fixtures; df before/after.

Completion: source review closed all three Important findings; 226 backend / 38 frontend tests and H5 build passed. Activated 2026-10-24 10:14 +08 at idle; protected collections unchanged. Local authenticated root UI, real read-only metrics, desktop/mobile layout and public HTTPS assets/boundary checked. No school requests or extra bookings. Evidence: development/work/njfu-admin-dashboard-20261024.
