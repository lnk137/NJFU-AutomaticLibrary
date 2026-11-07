# Calendar, scheduled segments and email notifications

## Authorized outcome

Develop in the existing isolated worktree. Add a calendar of effective use dates, daily/weekly scheduled multi-segment booking, and opt-in automatic email for booking results and reminders before each accepted interval. Reminders offer a cancellation entry; cancellation always requires an explicit user action and only affects the selected school reservation. No response preserves it. Personal templates, multi-segment form imports and copied result messages are excluded.

## Compatibility and execution

Legacy single-interval plans retain their representation and execution identity. New multi-interval plans contain independently ordered seats, chronological non-overlapping intervals, and stable per-segment identities. Honor per-day skip/override, access, revisions, clock, release deadlines, existing bounded retry rules and root-only conflict notices. Uncertain results, global rejections and rate limits stop the remaining intervals. Never cancel successful siblings or replay past/interrupted work.

Calendar reads saved evidence only; planned, skipped, confirmed, failed, uncertain and cancelled entries are distinct. Do not infer school success from a configured plan. A student sees only their account; root chooses an account. Date edits reuse the existing temporary-adjustment workflow.

## Mail and cancellation

Notification settings are independent of booking-plan revisions. Verify a recipient address before enabling delivery; expose result/reminder switches and a default 30-minute reminder (configurable). Send only account-owned information. Persistent deduplication and bounded delivery operate outside the release-time critical path. Recover safely after restart; do not flood old results or send expired reminders. SMTP uses encrypted transport and a private local configuration, never credentials in logs/API/UI. Invalid mail configuration must not stop the scheduler. Real mail credentials and actual recipient acceptance remain separate from fake-SMTP verification. A running execution backend is needed to send timely reminders.

Links open authenticated pages and never mutate through GET or email-link preview. Cancellation rechecks exact owner/record identity, availability and live permission, confirms one selected interval, and reads back the school outcome before claiming success. Do not invent a school endpoint or policy. If the authenticated cancellation protocol cannot be verified from source, deliver an honest official-school cancellation entry and record the integration limit rather than enabling a guessed destructive request.

## Acceptance

Use existing dependencies, isolated temporary Mongo/vault, fake school and SMTP with network denied. Each implementation domain adds meaningful regression tests. Verify legacy plans, date exceptions, favorites, form flow and successful reservations remain unchanged. Review the final combined source, run relevant then complete Python/Node suites, H5 build and isolated desktop/iPad/phone browser acceptance. Preserve evidence and an exact increment. No production-data access/mutation, live school booking/cancellation, external email, service restart/deployment, dependency install or Git commit/push.
