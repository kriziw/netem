# NetEm WAN Lab Roadmap

NetEm is moving from a WAN-impairment GUI toward a vendor-neutral network-resilience validation platform:

> see what is happening → choose a test → inject a controlled condition → measure the outcome → assert requirements → retain evidence → compare results.

## Implemented measurement/evidence foundation

### Active measurement engine

Continuous bounded measurements now support ICMP echo, TCP connect, HTTP/HTTPS response, and DNS A-query response. Probe intervals are limited to 2–3600 seconds and timeouts to 0.2–10 seconds. Results are persisted, associated with Lab Sessions, and failure/recovery transitions are logged as runtime events.

Important topology limitation: transparent WAN ports normally have no Layer-3 address. Automatic probe source therefore uses the NetEm host routing table and is not claimed to prove that the selected transparent WAN was crossed. Interface binding works when that interface has usable Layer-3 routing. A remote probe-agent design is recommended later for true appliance-side independent measurement.

### Persistent SQLite telemetry

The runtime telemetry database stores downstream/upstream Mbit/s and PPS, injected delay/jitter/loss/quality, expected SLA state, runtime fault, session ID, and active-probe samples.

WAN sampling is approximately every two seconds with 168-hour default retention. Analytics can load 15m/1h/6h/24h/7d ranges and then append live browser samples.

### Session reports + assertions

Completed Lab Sessions now produce evidence reports with PASS / FAIL / UNSCORED result, scenario run status, assertion outcomes, WAN telemetry summaries, probe success rate and latency percentiles, correlated events, printable HTML, and JSON export.

Reports are snapshotted into the session record.

### Conditional scenarios

Scenario actions now include quality, fault, mtu, wait, and assert.

Conditions can evaluate expected SLA PASS/FAIL, active-probe success/latency, measured downstream/upstream Mbit/s, and measured downstream/upstream PPS. Conditional stages have bounded timeout/polling and can stop or continue after failure.

## Recommended next milestone

### 1. Vendor adapter framework — Fortinet first

Create a normalized adapter contract for WAN member health, observed latency/jitter/loss, SLA state, selected path, and transition timestamps. Implement Fortinet first for the existing lab, then reuse the same contract for Cisco, Palo Alto, Juniper, VMware/VeloCloud and Versa.

This unlocks real metrics such as failure-detection time, path-move time, total failover convergence, recovery convergence, and injected vs measured vs appliance-observed differences.

### 2. Parallel multi-WAN scenarios

Allow one scenario to manipulate more than one WAN: primary failure while backup degrades, dual brownout, staggered recovery, and bounded multi-link chaos profiles.

### 3. Bounded traffic generator

Wrap iperf3 with configured/allowlisted targets, explicit duration, bandwidth/PPS caps, upload/download/bidirectional modes, burst/microburst profiles, emergency stop, and Lab Session association.

### 4. Session comparison analytics

Compare completed runs by assertion outcome, timing, probe percentiles, WAN traffic behavior, expected SLA behavior, and later vendor-observed failover/recovery timing.

### 5. Runtime qdisc + updater preflight

Verify actual kernel qdisc state after Apply and compare requested vs installed shaping. Before updates, verify service-user write permissions, clean/fast-forward Git state, and compatible systemd restart policy.

### 6. Remote probe agent

Add a lightweight probe agent behind the firewall/SD-WAN device for true appliance-side path measurement without assigning IP addresses to NetEm's transparent bridge members.

### 7. Shared-lab platform capabilities

Later: authentication and role separation, scoped API tokens, audit trail, config/profile/session backup and restore, dynamic WAN count beyond two, and additional vendor adapters.

## Recommended order

1. Fortinet adapter framework
2. Parallel multi-WAN scenarios
3. Bounded traffic generator
4. Session comparison analytics
5. qdisc/updater preflight
6. Remote probe agent
7. Additional vendor adapters
8. Authentication / API tokens
9. Dynamic WAN count

The key architectural shift is now in place: NetEm can inject, measure, assert and retain evidence. The next milestone should correlate that evidence with the SD-WAN appliance's own observed behavior.
