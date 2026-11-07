# Native optimization integration review

## Goal and current state

Review the uncommitted candidate in `library-submit-db` against the current primary checkout. Both checkouts start at `4acd461a915c19ce9ebe65440ab9e494c9900ba0`; the primary contains newer retry and form-import fixes that must be preserved. This is file-level integration, without a Git commit or branch reset.

The candidate contains two independently scoped changes:

1. A prepared-session fast path that removes two repeated Mongo reads while retaining the at-send permission, deadline, clock and durable claim checks.
2. A C++ authority and Python management/scheduler sandbox. It currently requires a private test Mongo database, hardcodes loopback TLS transport, sends a synthetic payload, and matches fake-school receipts. It has no production school-protocol adapter, manual/busy-seat retry parity or verified production sleep/wake integration.

## Integration boundary

Integrate only reviewed, independently tested behavior. Preserve existing plans, access, credentials, reservations, history, public UI, 24-account capacity (updated by the user during review), and the currently running scheduler. No real school request, booking, cancellation, catch-up or plan mutation is part of this task.

Promote the C++ implementation and its executable isolation checks into a self-contained experimental source directory, with explicit fake-only entrypoints. Do not connect it to `Runtime`, the public gateway, or the live scheduler. Do not weaken private-database, loopback, durability, revocation, receipt or non-replay guards to make it appear production-ready. Record the remaining production activation gates in the final review.

Merge the small database fast path by applying only its necessary lines to the latest primary files. Preserve the primary retry policy and form-import behavior. Add its focused regression tests and update the existing operational documentation.

## Validation and review

- Freeze source identities, Git state, available space, production PIDs/health and per-field account/history/access hashes before changes.
- Independently review the C++/management boundary and the Mongo fast path. Resolve confirmed merge-blocking defects in the intended merge scope before promotion.
- Reproduce the database read-budget regression before the change, then run its safety tests and existing relevant retry/import tests after it.
- Rebuild the promoted C++ source using existing CLT, SQLite and libcurl; run its private TLS/Mongo protocol, scheduler, persistence, recovery and peer-lifecycle regressions. Exercise all external/production endpoint guards.
- Run the complete current backend suite. No frontend rebuild or public redeploy is needed when frontend bytes do not change.
- Recheck the precise source diff and production protection evidence. Retain source, logs, origin/build identities and recovery evidence; clean only this task's verified disposable build/runtime files.

## Limits

Fake-service timings do not establish actual school acceptance or stable production speed gains. Source integration of the C++ sandbox is distinct from switching the live executor. Production activation requires the real encrypted request/cookie/token contract, school receipt mapping, existing feature/retry parity, restart/wake behavior, and an interleaved latency comparison to pass in isolation first.
