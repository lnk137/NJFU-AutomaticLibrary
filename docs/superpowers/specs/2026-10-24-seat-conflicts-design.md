# Internal seat conflict reminders

## Goal and scope
Show local root a compact reminder when different accounts' saved future scheduled plans share a seat during overlapping use times. No automatic plan changes, scheduling actions, school queries, reservations, notifications or new dependencies. Continue in the attached worktree and preserve features 1 and 2.

## Behavior and design
- Check actual use dates from tomorrow through seven days from now, in Asia/Shanghai. Include only authorized accounts with enabled valid plans and a release time still in the future. Historical dashboard filters do not change this live future window. Invalid active plans are counted as unchecked, never silently treated as conflict-free; disabled/unauthorized plans do not produce collisions.
- Reuse existing plan/date-exception validation and `resolve_run`: weekly days, skip/range exceptions, one-day overrides, actual ordered seats and Friday closing all apply. Favorites/groups not applied to a plan do not participate. Compare use times, independently of trigger times; touching endpoints are not overlaps.
- Include primary and backup seats. A pair of primary seats is labeled as primary overlap; any pair containing a backup is labeled as potential backup overlap. Both are advisory, never proof of a school booking. Each entry identifies date, seat, overlap interval and both people with their own time windows and seat ranks.
- Keep a pure detector separate from execution. Feed it the dashboard's already validated plan snapshots; no storage, account locks, runtime/scheduler or school clients. Add its bounded sanitized output to the existing local root dashboard response; retain current authentication, public-root denial, refresh and stale-session behavior.
- Avoid enumerating unbounded pairs: sort by use-time per date/seat, stop after finding the 51st conflict, return at most 50 plus `has_more`, and prioritize primary overlaps. Never claim an exact total when truncated. Only a compact first three rows are shown initially; existing-style expansion reveals the returned rows.
- The root-only panel shows its date range, checked and unchecked plan counts, empty state and reminder-only wording. No mutation controls. A view-plan link uses the existing account navigation.

## Validation
TDD for overlap vs touching/different dates/seats, primary/backup, different trigger times, weekly/Friday behavior, skips and overrides, passed release times and the seven-day boundary, bounded results and input immutability. Isolated real Mongo dashboard/API tests assert authorization, unchanged collections and zero school/execution calls. Real Vue render tests assert names/ranks/time/limits/empty and student privacy. Run complete backend/frontend suites and H5 build, then isolated browser readback with protected-data equality, stop task fixtures and clean owned rebuildable output. One independent read-only review; material findings receive regression tests before fixes. No merge or production activation.

## Open questions
None required for this limited reminder. Existing school reservations, manual actions and broader conflict resolution are outside this feature.
