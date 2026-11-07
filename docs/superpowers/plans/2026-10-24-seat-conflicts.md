# Internal Seat Conflict Reminder Plan

**Spec:** ../specs/2026-10-24-seat-conflicts-design.md
**Goal:** root-only, read-only conflict reminders without changing reservations/plans.
**Constraints:** same attached worktree/dependencies; no production access, school calls, scheduling changes, commits, deployment or installs. Evidence: development/work/seat-conflicts-20261024/.

## Review Focus
1. Use-date/interval semantics, backups, exceptions, Friday bounds and future release eligibility agree with existing execution code.
2. Account privacy and absent/invalid/unavailable data cannot produce a false all-clear; no credentials or saved metadata leak.
3. Heavy overlap is bounded, truncation is explicit, primary collisions remain visible, and dashboard refresh cannot schedule or write anything.
4. The root panel preserves existing loader/session guards, student/public denial and all prior features.

## Task 1: Pure conflict detection
- [x] Add behavior tests; observe RED for the missing detector.
- [x] Reuse effective run resolution and detect ordered-seat overlaps with bounded results.
- [x] Verify targeted tests GREEN, including unchanged inputs and boundary cases.

Interface: validated authorized plan snapshots in; sanitized `{start_date,end_date,checked_accounts,items,has_more}` out.

## Task 2: Read-only dashboard integration
- [x] Add isolated store/API tests, observe RED for missing reminder output.
- [x] Feed validated plans to detector; expose unchecked count and preserve existing root boundary.
- [x] Verify collections unchanged and no school/scheduler calls.

Interface: existing root-only dashboard response gains `seat_conflicts`; no new endpoint or polling.

## Task 3: Compact root panel
- [x] Add real Vue render tests, observe RED for missing root reminders.
- [x] Show date/seat/people/interval/rank, primary vs backup, empty/unchecked/truncation and expansion.
- [x] Reuse existing view-account navigation; no adjustment controls.

## Task 4: Validation/review/handoff
- [x] Full backend `.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v`.
- [x] Full frontend `node --test tests/frontend/*.test.mjs`.
- [x] H5 `npm run build:h5`.
- [x] One fresh read-only review; Important/Blocker fixes RED→GREEN plus final full suite.
- [x] Isolated browser root visibility and student privacy with data unchanged and zero submissions.
- [x] Feature-only patch/source identities/report; shut down fixtures and clean task-owned H5 output after preserving build identity. Keep worktree/evidence; no activation.
