# Date exceptions

## Goal and agreed scope
Implement only the first agreed feature in an isolated worktree: skip the next scheduled use day, skip a date range, override one use day's interval/seats, or explicitly add an otherwise inactive weekday. Students edit their own account; local root can edit an account. No new root approval step is added to ordinary web actions. Form import, favorites, collision UI and notifications are outside this change.

## Design
- Store `date_exceptions` separately from `plan` on each account. Missing data means no exceptions; no existing account or plan is migrated or rewritten. A date has either `skip` or a complete `override` snapshot (`begin`, `end`, ordered `seat_ids`). Base plan changes preserve these snapshots.
- Existing master enable/access/live gates remain authoritative. Resolve by actual Asia/Shanghai use date, before weekday filtering, so an explicit override can add an inactive weekday. Use the existing school interval validation, including Friday clipping and minimum duration.
- Account-owned edits use the existing account lock and expected account revision. Save only exceptions and increment that revision, invalidating unsubmitted prepared work. Do not alter task identities, old results, school records, credentials, or the base plan. Reject additions/overrides for dates whose scheduled release has started. Reject edits from three seconds before release through thirty seconds afterward, including adjacent-day releases, and recheck after reads immediately before writing; the three-second margin covers the runtime's two-second Mongo operation budget. Explicit removal may discard an expired local setting without replaying its date. Keep at most 366 date settings, with each range limited to 366 dates.
- Backend scheduler and administrator preview use the same effective resolver and next-run search. A long leave must not be reported as an invalid weekly plan. The frontend preview follows the same date fixtures and clearly preserves unsaved base-plan edits.
- Add one compact date-exceptions section to the plan page, with skip-next, leave-range and one-day editor. Changed seats are an ordered snapshot of at most five catalog seats. Removing an upcoming exception restores the base schedule for that date. Dirty editors are protected on navigation/account changes.
- Exceptions require the campus backend to be online in this version. Existing offline base-plan synchronization preserves exceptions and is not subjected to a root review gate.

## Validation and safety
Use the existing isolated Mongo fixture, school doubles and frontend component tests. Cover account ownership, stale revisions, master-off, date/year boundaries, Friday rules, inactive weekdays, long leave, prepared-task invalidation, restoration and no replay. Run all backend/frontend tests and H5 build; inspect the isolated UI. Do not access production Mongo or school APIs, change running services, install dependencies, commit, push or deploy.

## Baseline and artifacts
Native managed worktree starts at `4acd461a915c19ce9ebe65440ab9e494c9900ba0`. Necessary current uncommitted source was copied without production data; its hashes are in `development/work/date-exceptions-20261024/baseline-manifest.json`. Shared existing dependencies are symlinks. Keep validation, review, feature-only diff and source identity evidence; remove only task-owned rebuildable output after validation.
