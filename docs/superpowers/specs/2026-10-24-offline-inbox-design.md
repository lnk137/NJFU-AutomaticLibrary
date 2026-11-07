# Offline authentication and configuration inbox

## Authorized scope
Public entry stays online while the campus Mac sleeps. Only currently authorized students with a previously school-verified account and an encrypted password on the Mac qualify. The school password remains encrypted on the Mac; the cloud receives a salted password verifier, sanitized account/configuration snapshots and queued plans. Existing root/public restrictions, plans, school records and 50 submit workers remain intact. No extra real reservation is allowed. The unfinished Mac power-window work is deferred.

## Authentication and permissions
- Public gateway probes the existing executor and prioritizes its real school login. Any executor HTTP rejection, CAPTCHA, protocol or school-network error is returned; it never becomes an offline success. Only an unavailable executor connection permits cached-verifier login.
- Offline eligibility is exported from the authoritative local allowlist, saved secret and successful school identity. Snapshot age is limited to 24 hours; sessions last at most 8 hours and bind to credential/access/auth-state generations. Revocation becomes effective as soon as it syncs; instantaneous changes cannot be known while the Mac is offline. Explain this inherent bound rather than promising instant revocation.
- A school authentication rejection blocks cached access until a successful school authentication clears it. Password hashes and sessions remain in private service storage and never reach browser/bootstrap/admin responses. Root never authenticates through the gateway.
- Offline sessions may read their own cached plan/results and queue a plan only. They cannot change passwords, authenticate new accounts, submit/cancel bookings or query the school. Resuming real school operations requires a new real school login.

## Durable inbox protocol
- A separate least-privileged HTTPS gateway uses a private SQLite WAL database. Each submission carries a UUID, account/credential/access generation, expected local revision and monotonic cloud version. One unacknowledged submission per account prevents ambiguous replacement; byte-identical retries return the same receipt.
- Local runtime publishes allowlist/verifier/sanitized snapshots and heartbeat; continuously leases queued plans, retries bounded transient errors, authenticates the locally saved school password, rechecks access/generation/expiry and validates school time/seat policy.
- Account plan, revision and last-inbox marker commit atomically in the existing account document. A separate local receipt makes acknowledgement retryable; replay after a crash recovers from the account marker. Expired, changed-generation and revision-conflicting messages cannot overwrite local data. No plan or receipt is deleted to reconcile a conflict.
- Individual accounts use independent futures and nonblocking account locks. One slow/failing school login cannot block other accounts or the scheduler. Shared network failures back off; explicit authentication refusals are terminal and visible per account.
- All queued configurations are pending until an acknowledgement derived from local committed readback. Server receipt, login and plan eligibility never imply school booking success.

## Release-time consistency
The worker checks the latest server version continuously, increases check frequency before the next release, and publishes last observed/applied versions. From 20 seconds before a release through 40 seconds after it, configuration application is deferred so prepared jobs remain unchanged. New receipts in that interval clearly say they apply to a later release. Existing local plans keep running when the cloud is unreachable; status must show that latest cloud state could not be verified, without globally pausing those plans. There is no late booking/backfill operation in this feature.

Sync I/O stops 15 seconds before release through 40 seconds after, allowing the bounded HTTP call to finish before payload warmup. A bounded announced heartbeat grace identifies this intentional pause. Public requests and worker control use separate loopback Waitress pools so slow student authentication cannot starve synchronization; concurrent cloud password-hash checks are capped at two.

## UI and observability
Reuse existing responsive student pages. Offline mode, stale school-query snapshots, pending/failed/applied state and the effective local plan remain distinct. Only local root sees executor heartbeat, last sync, pending/failed counts, authentication exceptions and per-account cloud/applied versions and readiness. No password/verifier/session tokens are returned. Existing history is unchanged.

## Implementation and verification
Use existing local Python/Flask/Werkzeug/Waitress/requests dependencies. The user approved matching locked packages only in `/srv/njfu-inbox/runtime`, not system Python. Native service, static assets and Nginx changes get exact rollback files. Test pure gateway/store behavior, fake executor refusal/offline/timeout cases, isolated local Mongo apply/crash/replay/conflict/revocation/Friday validation, multi-account delays and release deferral. Run existing backend/frontend regressions, review source/security boundaries, then load in an idle window and compare protected collection hashes. Do not physically sleep the Mac or send live school bookings to validate.
