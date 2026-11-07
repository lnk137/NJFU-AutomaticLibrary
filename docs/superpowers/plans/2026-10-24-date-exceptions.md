# Date Exceptions Implementation Plan

**Goal:** Add date-specific scheduling without changing existing plans or reservations.
**Architecture:** Separate account exception snapshots; effective date resolver shared by scheduler and reporting; compact account-owned editor.
**Tech Stack:** Existing Python/Flask/PyMongo, Vue/uni-app and Node tests; no new dependency.
**Spec:** ../specs/2026-10-24-date-exceptions-design.md
**Execution:** Native inline, TDD. No commits/push/deployment. Ledger and evidence: development/work/date-exceptions-20261024/.

## Global Constraints
Only isolated worktree/source and isolated test data may change. Existing source, production plans/history, school APIs and running services remain untouched. No global approval gate. Missing exceptions preserve old behavior. Exceptions follow use dates in Asia/Shanghai.

## Review Focus
1. Upcoming edits must never reactivate completed/missed tasks or cancel a school reservation.
2. Weekly off-day overrides and long leave must agree between preview, scheduler and reporting.
3. Independent exception saves must preserve base-plan drafts and stale-revision protection.
4. School Friday/minimum interval rules and ordered catalog seats apply to overrides.
5. Offline synchronization and late responses must preserve effective data and account isolation.

## Task 1: Effective date rules
Files: local_app/date_exceptions.py, local_app/models.py; tests/test_date_exceptions.py.
- [x] Write literal date fixtures and watch missing behavior fail.
- [x] Implement strict `validate_date_exceptions(data, catalog)` and `next_scheduled_run(account_id, revision, plan, now, date_exceptions=None)`; extend `resolve_run(..., date_exceptions=None)` without changing old callers.
- [x] Verify skip, restore, weekly override, disabled master, year boundary, Friday and long leave.

## Task 2: Persistence, API and integration
Files: local_app/{accounts,store,web,scheduler,dashboard}.py; tests/test_date_exception_service.py.
- [x] Write isolated tests before implementation; verify missing endpoint/save behavior fails.
- [x] Add account field/default and revision-checked `update_date_exceptions(account_id, data, expected_revision)` for skip_next, skip_range, override and remove.
- [x] Refuse released dates and active submission-window edits; no school requests or base-plan changes.
- [x] Integrate effective resolver into scheduler and administrator next-run reporting.
- [x] Verify owner/root boundaries, stale writes, long leave, old task protection and offline plan preservation.

## Task 3: Frontend and user journey
Files: services/{date_exceptions,drafts}.mjs, services/local_api.js, components/DateExceptions.vue, pages/account_info/account_info.vue; frontend date-exception tests.
- [x] Write parity fixtures and component/page behavior tests; watch them fail.
- [x] Add isolated date editor with ordered seats and draft protection, one action per save, no root approval.
- [x] Update next-window preview and safely merge only the exception revision/account response.
- [x] Verify old-page responses, account switch, independent draft preservation, long leave and real component render.

## Task 4: Verification and handoff
- [x] Backend: `.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v`.
- [x] Frontend: `node --test tests/frontend/*.test.mjs`.
- [x] Build: `npm run build:h5` in AL-FrontEnd with existing dependencies.
- [x] Inspect isolated fake-school UI; review changes against captured baseline, retain feature-only patch and original-source hash check.
- [x] Fresh read-only review; fix material findings with failing regression tests first.
- [x] Retain worktree/deliverable/evidence; clean task-owned rebuildable output and measure space. No activation or real booking.
