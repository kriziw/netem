# Resilience Lab Architecture

NetEm WAN Lab is a vendor-neutral WAN resilience validation platform built on transparent Linux bridging and traffic control.

The design principle remains:

> the network path creates a controlled condition; the appliance under test reacts using its own health checks, routing/policy logic and analytics.

## Current capability layers

### 1. Impairment engine

Linux tc/netem provides latency, jitter, random/correlated loss, duplication, corruption, reordering and asymmetric bandwidth shaping.

Runtime faults add bidirectional blackhole, downstream-only blackhole, upstream-only blackhole and normal restore. Blackholes preserve carrier state so SLA/health logic must detect a failed data plane rather than relying on interface-down state.

### 2. Path MTU testing

NetEm can temporarily constrain MTU on the WAN bridge plus inner/outer members and restore the original values later.

This is path-MTU constriction, not a full PMTUD-blackhole implementation that selectively suppresses ICMP/ICMPv6 feedback.

### 3. Guided and custom tests

Built-in tests include Progressive brownout, SLA failover, Flaky underlay and Availability stress / DDoS-impact simulation.

Custom scenarios are persisted in config.json.

Supported actions:

- quality
- fault
- mtu
- wait
- assert

A scenario is limited to 30 stages. Timed delays are 0–3600 seconds.

Conditional stages can wait/assert on expected SLA state, active-probe success, active-probe latency, measured downstream/upstream Mbit/s and measured downstream/upstream PPS.

Conditional timeout is limited to 1–600 seconds and polling to 0.25–5 seconds. Failure behavior can stop execution or continue while retaining a failed final result.

### 4. Generic SLA model

A persisted vendor-neutral SLA profile defines maximum injected latency, jitter and packet loss.

NetEm evaluates the current effective impairment and runtime fault against that profile.

This remains an expected/injected SLA result, not the appliance's own observed SLA.

### 5. Active measurement engine

Independent bounded measurements support ICMP echo, TCP connect, HTTP/HTTPS response and DNS A-query response.

Probe guardrails:

- maximum 20 configured probes
- 2–3600 second interval
- 0.2–10 second timeout
- optional inner/outer interface binding
- persisted results
- failure/recovery transition events
- Lab Session association

#### Transparent-path scope

A normal NetEm WAN bridge is intentionally unnumbered. Automatic-source probes therefore follow the NetEm host routing table.

They are useful independent service measurements and can be associated with a WAN for correlation, but they are not represented as proof that the selected transparent path was traversed.

Inner/outer interface binding is available when the interface has usable Layer-3 routing/source addressing. A future remote probe agent is the preferred method for true appliance-side independent path measurement.

### 6. Persistent telemetry

SQLite storage lives in runtime/telemetry.db.

Approximately every two seconds the server records per WAN:

- downstream Mbit/s
- upstream Mbit/s
- downstream PPS
- upstream PPS
- injected latency
- injected jitter
- injected packet loss
- quality
- expected SLA pass/fail
- runtime fault
- active Lab Session ID

Active-probe results are stored in the same database.

Default retention is 168 hours.

Analytics can query 15-minute, 1-hour, 6-hour, 24-hour and 7-day ranges. The history API bucket-aggregates long time ranges to keep responses bounded.

### 7. Lab Sessions and evidence

A Lab Session groups one validation objective.

While active, the session ID is associated with runtime events, WAN telemetry, probe samples, scenario runs and assertion events.

Completing a session generates a report snapshot containing:

- PASS / FAIL / UNSCORED result
- scenario completion status
- assertion outcomes and observed values
- WAN telemetry summary
- expected-SLA-fail sample count
- probe success rate
- average/P95/max probe response time
- event summary and evidence timeline

The HTML report is printable and the same evidence is available as JSON.

An interrupted service restart marks the in-progress session as interrupted instead of pretending it resumed cleanly.

### 8. Safe security and diagnostics

Current safe capabilities include the standard harmless EICAR artifact, benign HTTP callback sink, availability-stress scenario without transmitting attack traffic, and bounded tcpdump capture.

Packet capture guardrails:

- configured WAN interfaces only
- one capture at a time
- maximum 120 seconds
- maximum 20,000 packets
- 256-byte snap length
- explicit stop/download

CAP_NET_RAW is required for packet capture and may also be required by the OS for explicit interface-bound probe sockets.

## Current API surface

Read-oriented integration endpoints include:

- /api/v1/state
- /api/v1/telemetry
- /api/v1/history
- /api/v1/probes
- /api/v1/events
- /metrics
- completed Lab Session report JSON

The API model is vendor-neutral.

## Injected, measured and observed

The platform separates three states:

    Injected by NetEm      Measured independently      Observed by appliance
    -----------------      ----------------------      ---------------------
    80 ms delay            HTTP response 96 ms         future vendor SLA 84 ms
    3% loss                probe reachability          future vendor loss 3.2%
    expected SLA FAIL      measurement failed          future member unhealthy

The third column is the next major integration milestone.

## Multi-vendor adapter principle

Vendor integrations should be adapters, not dependencies of the impairment engine.

A normalized adapter should expose member/link health, observed latency/jitter/loss, appliance SLA state, selected path and transition timestamps.

Fortinet is the recommended first adapter for the current lab. Cisco, Palo Alto, Juniper, VMware/VeloCloud, Versa and others should map into the same common model.

## Persistence model

Persisted configuration:

- WAN topology
- access profiles
- line-rate overrides
- quality/custom impairment state
- custom scenarios
- active-measurement definitions
- generic SLA profile

Persisted runtime evidence:

- runtime/events.jsonl
- runtime/sessions.json
- runtime/telemetry.db
- runtime/captures/

Transient by design:

- active blackholes
- temporary MTU changes
- active scenario execution
- active packet-capture process
- active Lab Session process state

Destructive transient conditions are not restored automatically after an application restart.

## Release management

Release Please manages semantic release state through version.txt, CHANGELOG.md, release-please-config.json and .release-please-manifest.json.

The appliance updater independently treats origin/main as the stable update channel and only installs fast-forward updates.

## Next architecture milestones

See docs/ROADMAP.md.

Current priorities are:

1. vendor adapter framework, Fortinet first;
2. parallel multi-WAN scenarios;
3. bounded traffic generation;
4. session comparison analytics;
5. qdisc/update preflight and self-verification;
6. remote probe agent;
7. shared-lab authentication and broader scale.
