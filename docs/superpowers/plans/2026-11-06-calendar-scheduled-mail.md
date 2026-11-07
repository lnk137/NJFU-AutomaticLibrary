# Calendar, Scheduled Segments and Mail Implementation Plan

**Goal:** Deliver the three accepted features in the existing isolated candidate with legacy behavior protected.

**Architecture:** Parallel agents own separate domains; the coordinator owns runtime/web integration and cancellation protocol verification. Saved effective schedule and durable school receipts feed the calendar and independent mail worker.

**Tech Stack:** Existing Python/Flask/PyMongo, Vue/Uni-app and Node test runner; Python standard-library SMTP; existing dependency environments.

## Execution

- [x] Record source-only baseline, protected main hashes and task-owned artifact ledger.
- [x] Scheduled-plan agent: add stable multi-segment validation/resolution/execution, preserve scalar IDs, connect dashboard/conflicts/power and focused plan editor; test legacy and uncertain-stop paths.
- [x] Calendar agent: add bounded, account-owned read-only calendar API/service/component and integrate page entry; test actual-use dates, exceptions, status and empty/failure states.
- [x] Mail agent: add secure SMTP settings loader, verified recipient preferences, durable bounded delivery/result/reminder worker and settings component; test deduplication, expiry, restart, failures and privacy with fake SMTP.
- [x] Coordinator: wire optional services and APIs, inspect official cancellation evidence and add authenticated cancellation entry without speculative school writes.
- [x] Independently review each domain for accepted scope, then review integrated code quality; fix material findings.
- [x] Run final backend/frontend suites and H5 build against the same frozen candidate; validate desktop/iPad/phone flows in an isolated browser.
- [x] Save exact changed-file patch/hashes/report, verify protected main hashes, stop fixtures and clean only task-owned reconstructable build/temp paths.

User explicitly selected parallel subagents. Shared integration files (`runtime.py`, `web.py`, `local_api.js`, `profile.vue`, README) belong to the coordinator; other ownership is stated in each dispatch. No commits or deployment are part of this plan.
