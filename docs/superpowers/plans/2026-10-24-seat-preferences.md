# Personal Seat Preferences Implementation Plan

**Goal:** Personal favorites, custom tags and reusable ordered groups without changing saved bookings/plans.
**Architecture:** Account-local metadata with independent CAS revision; shared seat-library UI emits draft choices only.
**Tech Stack:** Existing Flask/PyMongo/Python, Vue/uni-app, Node tests. No new dependency.
**Spec:** ../specs/2026-10-24-seat-preferences-design.md
**Execution:** Inline TDD, same attached worktree. No commits/push/deployment. Evidence/ledger: development/work/seat-preferences-20261024/.

## Global Constraints
Preserve current worktree first feature and original production checkout/data/services. Metadata edits do not change account/credential/scheduling revisions, plans, exceptions or runs. Only the owner and existing local root access a library. No shared/predefined tags/groups. Group application is a draft snapshot, followed by existing explicit apply/save.

## Review Focus
1. Metadata writes must not invalidate prepared jobs, manual previews or concurrent ordinary plan saves.
2. Async load/save and account/session changes must not leak or overwrite another user's data/drafts.
3. Group CRUD and tag edits must not silently change seat order in existing drafts or saved plans.
4. Unknown seats, empty/oversized groups and malformed metadata must fail without partial writes.
5. Offline reads and private metadata editors must preserve usable drafts and the existing sync boundary.

## Task 1: Metadata rules/storage/API
Files: local_app/seat_preferences.py, models.py, store.py, accounts.py, web.py; tests/test_seat_preferences.py.
- [x] Write behavior tests, observe missing feature fail.
- [x] Validate complete preference snapshots; GET defaults without writes.
- [x] Independent metadata CAS; unchanged scheduling/credential fields and locks.
- [x] Owner/root routes; stale data/invalid payload/privacy tests pass.

## Task 2: Shared seat library
Files: services/seat_preferences.mjs, services/local_api.js, components/SeatLibrary.vue; frontend library tests.
- [x] Write failing normalization/filter/snapshot/component/controller tests.
- [x] All/favorites/groups views, custom tags and group editing; controlled draft snapshots.
- [x] Session/account guards, stale-write reload and dirty-editor protection.
- [x] Offline cached selection with online-only metadata writes.

## Task 3: Integrate existing selectors
Files: pages/account_info/account_info.vue, components/DateExceptions.vue and existing frontend harnesses.
- [x] Write parent tests before integration; group use changes pending selection only.
- [x] Preserve normal/today/date-exception apply/save/preview behavior and saved draft recovery.
- [x] Library saves update only library fields in account snapshots, never account revision or plan drafts.

## Task 4: Verify/review/handoff
- [x] Full backend: `.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v`.
- [x] Full frontend: `node --test tests/frontend/*.test.mjs`.
- [x] H5: `npm run build:h5` in AL-FrontEnd.
- [x] Fake-school isolated browser; real saved/readback metadata and unchanged plans/credentials/run count.
- [x] Fresh read-only review; material findings get RED→GREEN fixes and final suite.
- [x] Retain feature-only patch, source identities and evidence; clean owned rebuildable H5 output and stop fixture processes; no activation.
