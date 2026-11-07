# Mac automatic booking wake window

Delivered and activated 2026-10-24. Next verified native wake: 2026-10-25 06:55 Asia/Shanghai. Acceptance: development/outputs/2026-10-24-mac-power-window-delivery.md.

## Intent and authorization
Install once into the existing application, then automatically wake this Mac at06:55 Asia/Shanghai each day, restore the existing backend/public relay and inspect campus connectivity before07:00. At07:05 evaluate results and allow normal sleep when safe; no daily manual start. The user chose AC/open lid and later noted accidental closing. No additional real booking, cancellation, late replay or plan/history rewrites.

## Current state
Mac17,3/arm64, macOS27.0.1, timezone Asia/Shanghai, AC attached. No project power installation or recurring wake rule exists. `sudo -n` is unavailable: a native macOS administrator prompt is required to install the system wake component. `public_run.py` currently holds caffeinate -dimsu forever. Other unrelated caffeinate/AweSun/ChatGPT assertions also inhibit sleep; never stop or reconfigure those. AC idle sleep is0; releasing this application's assertion alone does not guarantee immediate system sleep.

## Approach
Use native power scheduling plus an application supervisor. A plain app timer cannot wake a sleeping Mac; launchd calendar work catches up after wake, so the hardware event must be registered through pmset. No Codex heartbeat or new dependency is needed for a software feature.

A root-owned, fixed shell helper under `/Library/Application Support/NJFUAutoLib/` and a new LaunchDaemon maintain one **owner-tagged one-time wake event** for the next06:55 Beijing, converting to the host local format. A private data-only plist contains enabled/paused/wake time. The helper validates fields, never sources user data, never runs project/Python code as root, never changes global sleep settings or overwrites other recurring power events. Cancellation only targets its own exact owner/date. Installation is idempotent, refuses unknown existing objects, and requires one native system grant. Toggle changes thereafter need no password.

A per-user LaunchAgent runs `power_agent.py` in the existing .venv after login, surviving page/app closure. It starts or reuses the existing authorized service mode, with launcher locks preventing duplicates. It owns only its own timed awake assertion. Public runner `--power-managed` delegates awake control only while the installed supervisor heartbeat is valid; otherwise it falls back to its existing protection. Start/Stop commands explicitly resume/pause this supervisor so an intentional stop is never silently restarted.

## Daily behavior
- Before06:55: OS wake event armed and its readback verified; no extra school requests. Disabled/uninstalled remains current behavior.
-06:55–07:05: retain system wake, start/reuse backend/relay, retry bounded read-only direct TLS GET to the school origin until reachable. Do not manipulate Wi-Fi passwords, captive-portal accounts, certificate verification or global network settings. School login/preparation remains the existing scheduler's job; network reachability does not claim school authentication or success.
-06:59:50–07:00:40: no network probes or new Mongo/metadata writes from this feature; leave booking timing untouched while retaining the existing awake lease.
- At/after07:05: use fresh scheduler heartbeat and read-only persisted expected-run outcomes. Active/missing/failed/uncertain tasks or health/clock/database errors keep this application's wake lease and show why. Completed current-window tasks permit releasing the application's lease. Never issue sleepnow/shutdown or terminate other applications. Normal OS/other-app/user activity decides actual sleep; status makes existing blockers clear. No new POST to the school occurs.
- Opening/waking after the booking cutoff does not backfill. Expired queued work keeps the original scheduler behavior. Unexpected wall-clock jumps do not authorize sleep.

## Interface and boundaries
Local root profile gets a Mac power section:06:55 wake,07:00 readiness target,07:05 review, install/enable/disable, paused state, installed/armed/supervisor/service/network/awake status, last reason and simple self-check. Fixed default window in this version; no arbitrary shell or paths accepted. All APIs are local-root-only, existing CSRF/origin/session boundary preserved; public/student requests denied. Power config/status live separately from account Mongo collections and credentials,0700 directory/0600 files, bounded atomic writes and expected revisions. Initial state is disabled until real installation/readback succeeds.

Accidental lid closing is a detected risk, not a promised supported state: macOS can suspend the Mac on lid close; dependable unattended execution requires AC/open lid, or a supported closed-display setup. Display sleep is allowed. Never set global disablesleep or defeat FileVault/login. Power-off/logged-out operation and campus portal login automation are out of scope.

## Implementation boundaries
New pure policy/config module, small native installer/renderer, unprivileged supervisor/OS probes, backend power service and read-only runtime heartbeat, existing root profile component. Preserve existing scheduler/account/booking algorithm. Reuse .venv, Node modules and Mongo binary. No install/download/commit, global PATH/timezone/idle-sleep changes, force sleep or live test reservation.

## Verification and activation
TDD against fake clocks/processes/network/data; own-tag schedule cancellation, root script shell syntax and launch plists, denied root/public/student APIs, stale/failed states, pause/restart, no accidental live/sleep calls,63+frontend and244+backend regressions. Isolated UI for root power section; native permissions/awake schedule installation only after tests and an independent source/security review. Guarded idle loading preserves all production collections/plans. Verify native grant, launchctl registration, pmset owner/date readback and local/public status separately from an actual overnight sleep/wake receipt. No physical sleep test while other work is running.

## Primary references
- Apple pmset scheduling: https://support.apple.com/en-au/guide/mac-help/mchl40376151/mac
- Apple launchd sleep semantics: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html
- Apple clamshell prerequisites: https://support.apple.com/en-gb/102282
- Installed pmset(1)/caffeinate(8)/launchd.plist(5) are the platform command contract.
