# Form Import Plan

Spec: ../specs/2026-10-24-form-imports-design.md. Evidence: development/work/form-imports-20261024/.
Constraints: same worktree, existing deps, no production or school access, no install/commit/merge/activation. Only imported requests require root review.

## Review Focus
1. No plan/credential changes to existing accounts or reservation submission before the single whole-file confirmation; new accounts saved only after real identity validation, without re-enabling revoked access.
2. Same-day execution retains scheduled plans, uses live/clock/expiry/duplicate checks, and cannot replay after retry, concurrent confirms or interruption.
3. Expiring encrypted payloads, safe responses/logs/template, bounded XLSX/CSV parsing and root/local/CSRF/session boundaries.
4. Late validation/confirmation cannot overwrite newer configs, affect unrelated accounts, hold booking locks during network I/O or mutate within protected release windows.
5. Root UI preserves imported state across timeout/navigation without extra checkboxes/confirmation layers, and normal user flows/previous features remain unchanged.

## Task 1: Template and parsing
- [x] Read spreadsheet authoring contract; ship blank XLSX and validate reader round trip.
- [x] TDD bounded XLSX/CSV rows, types, dates/times, seats, formulas/duplicates/errors.

## Task 2: Durable validation/review/execution
- [x] TDD encrypted staging, new formal accounts, independent existing-account validation, today preflight, permissions/quiet windows.
- [x] TDD whole-file atomic claim, CAS scheduled changes, same-day original-plan preservation, idempotence, restart interruption and no replay.
- [x] Wire optional runtime service and local root routes; test origin/CSRF/student/public denial and no sensitive fields.

## Task 3: Root import UI
- [x] TDD API/file encoding/controller/render behavior and session races.
- [x] Integrate compact launcher/dialog, download/drop/select, row summaries, one whole-file confirmation and durable results.

## Task 4: Verify/review/handoff
- [x] Full backend/frontend suites and H5 build.
- [x] One fresh read-only review; fix Important/Blocker via regression tests then full suite.
- [x] Isolated browser file validation, formal account visibility, whole-file confirmation and results; unchanged unrelated plans, zero real submissions.
- [x] Source/build identities, feature-only patch/report, cleanup owned fixtures/authoring temps/H5 output; preserve worktree/template/evidence.
