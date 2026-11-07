# Dual-node read-only observation

## Goal and boundaries

First-stage Mac/Ubuntu environment inspection and comparable public school-interface measurements. This tool is manually invoked and independent of the booking Runtime. It does not start a scheduler, connect to MongoDB, read credentials, authenticate a school account, query personal records, reserve/cancel a seat, install dependencies, or change power/network/service settings. There is no execution authorization or production integration in this stage.

The current native build and several native clock/persistence primitives depend on macOS. A successful Ubuntu inspection or public GET does not establish native-executor compatibility or account booking readiness.

## Design

- Ship one standalone standard-library Python script, `diagnostics/dual_node_probe.py`, with `inspect`, `sample`, and offline `compare` commands. Reuse existing Python and curl installations. Inspect missing capabilities; do not install them.
- Use the exact unauthenticated `GET https://libseat.njfu.edu.cn/ic-web/sysConfig/public` already used for native transport warmup. Disable curl configuration/proxies, redirects, authentication, cookies, and retries. Keep TLS certificate/hostname verification enabled and use HTTP/1.1 to match the native path.
- Limit each request to three seconds, connection establishment to one second, response capture to a small fixed bound, each run to ten samples, and request starts to at least ten seconds apart. Default to six samples. Retain sanitized results and timing values, not response bodies or raw stderr.
- Validate HTTP 200, a JSON object, integer `code == 0`, list `data`, and one recognized `resvCode` value. Report CAPTCHA requirements separately. This evidence measures the public interface only; account authentication, trusted clock state, warmed native request performance, and booking success remain unverified.
- Protect every default 07:00 Beijing release and explicit custom `--release-time HH:MM:SS` with a five-minute window before and after. Account for the entire request budget before starting; handle adjacent dates and recheck each sample. Invalid schedules reject execution. This deliberately avoids the live preparation period during initial observation.
- Do not discover plans by reading the production database. Operators must supply any custom release times. The default guard cannot protect an undisclosed custom schedule; avoid running alongside manual bookings or other active preparations.
- Reports use explicit node labels, protocol/version and measurement parameters, timestamps, per-attempt outcomes, success counts, and median/maximum timings. There is no host/IP/credential dump. Output files are created exclusively with private permissions; existing files and symlinks are refused.
- Offline comparison rejects mismatched measurement parameters, stale or nonoverlapping windows, invalid evidence, or insufficient successful samples. Any network preference is advisory; it cannot grant execution rights. Two unsynchronized clocks make cross-node timing alignment uncertain.

## Manual commands

Run from the project directory. Output paths must be new files in an existing directory. Omit `--output` to emit JSON to stdout only.

```bash
.venv/bin/python -B diagnostics/dual_node_probe.py inspect --node mac --output /absolute/new/mac-environment.json
.venv/bin/python -B diagnostics/dual_node_probe.py sample --node mac --samples 6 --interval 10 --output /absolute/new/mac-samples.json
```

For custom release times, repeat `--release-time HH:MM:SS`; default 07:00 protection always remains active. Environment inspection itself makes no school network requests.

With an existing trusted SSH connection, stream the same script to Ubuntu without copying the repository or creating a remote script/environment:

```bash
ssh -T -oBatchMode=yes -oStrictHostKeyChecking=yes -oConnectTimeout=5 USER@UBUNTU_IP python3 -B - inspect --node ubuntu < diagnostics/dual_node_probe.py
ssh -T -oBatchMode=yes -oStrictHostKeyChecking=yes -oConnectTimeout=5 USER@UBUNTU_IP python3 -B - sample --node ubuntu --samples 6 --interval 10 < diagnostics/dual_node_probe.py
```

These commands intentionally require an already trusted host and available noninteractive authentication. Resolve missing access through the normal SSH setup; do not paste passwords/private keys into chat or disable host-key verification. The remote JSON contains no login/session data. Capture actual stdout into a new local evidence file using exclusive creation, and preserve SSH failures separately from school-network failures.

For a comparison, start the Mac and Ubuntu sample runs together and compare their newly saved reports promptly (freshness limit: 180 seconds):

```bash
.venv/bin/python -B diagnostics/dual_node_probe.py compare /absolute/mac-samples.json /absolute/ubuntu-samples.json --output /absolute/new/network-comparison.json
.venv/bin/python -B -m unittest discover -s tests -p 'test_dual_node_probe.py' -v
```

## Validation and evidence

Unit tests mock network calls and cover request restrictions, guard boundaries including midnight, invalid school responses, timeouts, bounded output, file safety, and refusal to compare invalid/stale evidence. Run only these relevant tests; the production files remain unchanged.

Before and after any actual Mac observations, read only the existing `/health` endpoint and listener PID. A healthy process does not prove future booking outcomes. A same-window Ubuntu measurement requires the target machine's SSH address or operator-run reports; never substitute a local fixture for actual Ubuntu evidence.

This task's retained evidence directory is `development/work/dual-node-readonly-20261105-p4jxcrqp/`. It contains the path manifest, actual environment/measurement reports, validation summary, service checks, and review. Unit-test temporary directories are disposable and must be cleaned; these evidence files are retained.

## Observed acceptance: 2026-11-05

The existing Sunlogin desktop/file session provided Ubuntu access. Its LAN and Tailscale SSH addresses timed out from this Mac; SSH authentication, host-key stores, network settings, and access permissions were not changed. Remote keyboard modifiers were unreliable, so the reviewed script was transferred intact with the existing file channel and invoked through a short command using shell filename completion.

Ubuntu reports 26.04 (desktop OS release: 26.04.1 LTS), x86_64, Python 3.13.5, G++ 15.2.0, curl/libcurl 8.18.0, and systemd 259. Existing Conda environment names were inspected without changing them. The selected Python has the core requests/pymongo/crypto packages but no waitress metadata; pkg-config did not find sqlite3 metadata. This does not establish that SQLite is absent from the system or other environments. System time synchronization reports enabled; absolute clock accuracy was not independently verified. Native execution remains unverified.

Actual reports are retained under `development/work/dual-node-readonly-20261105-p4jxcrqp/`: `environment.json` is Ubuntu inspection, `mac-environment-final.json` is Mac inspection, and `mac-aligned-samples.json` / `samples2.json` are the final paired measurements. `aligned-comparison.json` was evaluated while evidence was fresh. Both nodes returned six valid public responses. Median/maximum total milliseconds: Mac 73.036/147.983; Ubuntu 70.392/98.241. The configured comparison found no clear network preference. The earlier pair was correctly rejected for a start-time difference above ten seconds, and its evidence remains available.

Ubuntu retains the reusable script and original reports at `/home/wls/Desktop/njfu-probe-20261105/`. There is no new background service or automatic booking selection. Local `review.md`, `validation.json`, and before/after service observations record source/service preservation and limits. Twenty-one isolated tests passed; school transport in tests was mocked.

Linux native-executor portability and separate single-owner execution authorization remain future work. These measurements prove public GET operation on both hosts, not account authentication, native performance, or real booking success.
