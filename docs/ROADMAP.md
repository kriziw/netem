# NetEm WAN Lab Roadmap

This roadmap prioritizes capabilities that turn NetEm from a powerful impairment UI into a repeatable network-resilience validation platform.

## Product direction

The design goal is:

> See what is happening → choose the outcome to test → inject a controlled condition → observe the device response → retain evidence → compare results.

Core impairment remains vendor-neutral. Vendor-specific integrations are optional observers layered on top.

## Recommended next milestone: measured truth + evidence

### 1. Active measurement engine

Add first-party probes that measure what actually traverses the impaired path:

- ICMP latency/loss
- TCP connect timing
- HTTP/HTTPS transaction timing
- DNS query timing
- optional user-defined probe target per WAN

Why first: the current UI knows exactly what it injects, but latency/jitter/loss are not yet independently measured end-to-end. Active probes create the missing “measured” side of the platform.

### 2. Persistent telemetry store

Add lightweight SQLite time-series/session storage for:

- throughput and PPS
- injected delay/jitter/loss/quality
- active-probe results
- SLA state
- runtime faults
- scenario stages
- future vendor-observed state

Keep Prometheus as an optional external export rather than an internal dependency.

### 3. Session results and reports

Extend Lab Sessions with:

- baseline snapshot
- tests run
- event timeline
- captures attached to session
- expected SLA transitions
- measured probe transitions
- pass/fail assertions
- HTML/JSON/CSV report export
- later PDF export

This turns sessions into useful evidence instead of only event grouping.

## Next milestone: SD-WAN response correlation

### 4. Vendor adapter framework

Create a normalized adapter contract:

- member/link health
- observed latency
- observed jitter
- observed loss
- SLA pass/fail
- selected path
- path/policy change events

Adapters should be optional and isolated from the impairment engine.

### 5. Fortinet adapter first

Recommended first implementation because the current lab already uses FortiGate.

Potential inputs:

- FortiGate REST API
- FortiManager where appropriate
- FortiAnalyzer for event/log correlation

Primary output:

- SLA failure detection time
- path-selection/failover time
- recovery time
- injected vs FortiGate-observed metrics

Then add Cisco, Palo Alto, Juniper, VMware/VeloCloud and Versa using the same normalized model.

## Test orchestration

### 6. Assertions and conditional stages

Extend scenarios beyond fixed timers:

- wait until expected SLA fails
- wait until vendor SLA fails
- wait until traffic moves to another WAN
- assert failover within N seconds
- assert recovery within N seconds
- branch on pass/fail
- abort on timeout

### 7. Parallel multi-WAN scenarios

Allow one scenario to manipulate multiple WANs:

- degrade WAN1 while WAN2 remains healthy
- fail WAN1 then degrade WAN2
- simultaneous dual brownout
- staggered recovery
- multi-link chaos profiles with safe bounds

### 8. Test templates

Higher-level templates that generate scenario definitions:

- primary-link outage
- brownout before failure
- asymmetric outage
- high latency
- packet-loss burst
- MTU regression
- backup-link degradation during failover
- recovery/hysteresis validation

## Traffic generation and replay

### 9. Bounded traffic generator

Wrap iperf3 with guardrails:

- configured/allowlisted targets only
- explicit duration
- maximum bandwidth/PPS
- upload/download/bidirectional
- constant, burst and microburst patterns
- emergency stop
- session association

### 10. Sanitized PCAP replay

Wrap tcpreplay with:

- configured lab interfaces only
- rate cap
- duration/packet cap
- explicit user confirmation
- session association
- no unrestricted external target selection

## Analytics

### 11. Session-aware time-series analytics

Add:

- scenario annotations
- fault markers
- SLA transitions
- vendor path changes
- capture start/stop markers
- zoomable time range
- compare WAN1 vs WAN2
- compare two sessions

### 12. Automatic resilience metrics

Calculate:

- detection time
- failover convergence
- service interruption
- recovery convergence
- packet-loss window
- application transaction failure window
- SLA false-positive/false-negative cases

## Platform quality

### 13. Updater and deployment preflight

Before enabling Install Update, verify:

- repository writable by service user
- .git object store writable
- application tree writable
- runtime tree writable
- working tree clean
- fast-forward possible
- systemd Restart policy compatible

### 14. Runtime qdisc self-verification

After applying a profile:

- inspect actual qdisc hierarchy
- compare kernel state with requested state
- surface partial failures
- optionally self-heal safe inconsistencies

This is especially important for bandwidth shaping.

### 15. Authentication and API tokens

For broader/shared labs:

- local admin/operator/viewer roles
- scoped API tokens
- reverse-proxy-aware auth option
- audit trail for control actions

### 16. Backup/restore

Export/import:

- config
- access profiles
- custom tests
- SLA profiles
- integration settings
- sessions/history metadata

## Scale

### 17. Dynamic WAN count

Move from the current two-link UI to a topology model that can represent:

- 1–8 WANs
- arbitrary display names
- dynamic bridge/interface pairs
- multiple appliances/test segments later

Do this only after the core session/measurement model is stable, because it touches configuration, UI and telemetry models broadly.

## Recommended order

1. Active probes
2. SQLite telemetry
3. Session reports + assertions
4. Fortinet adapter
5. Conditional scenarios
6. Parallel multi-WAN scenarios
7. Bounded traffic generator
8. Session-aware analytics / comparison
9. qdisc self-verification + update preflight
10. Additional vendor adapters
11. Authentication / API tokens
12. Dynamic WAN count

The highest-value next step is not another impairment primitive. It is adding measured truth and evidence around the impairment engine that already exists.
