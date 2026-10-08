# Resilience Lab Architecture

NetEm WAN Lab is evolving from a WAN impairment frontend into a vendor-neutral SD-WAN resilience test platform.

The design principle is simple: **the network path should create the condition; the SD-WAN appliance should react to it using its own health checks, policies and analytics.** This keeps the lab equally useful for Fortinet, Cisco, Palo Alto, Juniper, VMware/VeloCloud and other implementations.

## Top 5 feature areas

### 1. Scenario engine

Current MVP:
- progressive brownout
- SLA failover / data-plane blackhole
- flaky one-way underlay
- availability-stress / DDoS-impact simulation
- automatic restoration of the persisted WAN profile when the scenario finishes or is stopped

The scenario engine is intentionally transient. It does not overwrite the saved WAN profile.

Future:
- user-editable scenario builder
- arbitrary stage durations
- parallel actions across WAN1/WAN2
- scheduled runs
- pass/fail assertions
- import/export of scenario JSON

### 2. Runtime fault injection

Current MVP:
- bidirectional blackhole
- downstream-only blackhole
- upstream-only blackhole
- restore normal configured profile

Blackholes leave the virtual link up. This is useful for validating that an SD-WAN implementation detects a failed data plane using SLA/health probes rather than relying only on interface carrier state.

Future:
- physical-link down/up
- intermittent flap profiles
- MTU/PMTUD blackholes
- DNS-only failure
- gateway-selective blackhole

### 3. Advanced packet impairments

Current MVP uses native Linux `tc/netem` for:
- latency
- jitter
- random packet loss
- correlated loss
- packet duplication
- packet corruption
- packet reordering
- asymmetric bandwidth shaping

Advanced packet impairments are manual overrides and therefore switch a WAN to Custom mode.

Future:
- Gilbert-Elliott / burst-loss presets
- queue-size / bufferbloat controls
- ECN/queue behavior
- direction-specific latency/loss/jitter
- reusable impairment templates

### 4. Live observability and vendor-neutral integration

Current MVP:
- browser-polled interface byte and packet counters
- live downstream/upstream throughput calculation
- live PPS
- runtime event history
- `GET /api/v1/state`
- `GET /api/v1/telemetry`
- `GET /metrics` in Prometheus exposition format

The core API is intentionally read-only at this stage. This keeps it safe to expose inside a lab while creating a stable integration point.

Future vendor adapters can consume vendor APIs and place observed SD-WAN state alongside injected NetEm state, for example:

```text
Injected by NetEm          Observed by SD-WAN
------------------         ------------------
Latency 80 ms              SLA latency 83 ms
Loss 3%                    SLA loss 3.2%
WAN1 blackhole             Member unhealthy
Scenario stage 4           Traffic moved to WAN2
```

The adapter layer should remain optional so the core product does not become Fortinet-, Cisco-, Palo Alto- or Juniper-specific.

### 5. Safe security and traffic events

Current MVP:
- standard harmless EICAR anti-malware test artifact
- benign HTTP callback sink for C2/beacon visibility tests
- availability-stress scenario that emulates the WAN impact of a DDoS/saturation event without transmitting attack traffic

The project deliberately does **not** embed malware or expose an unrestricted packet-flood launcher.

Future:
- bounded lab-only traffic generator
- explicit target allowlists
- RFC1918/lab-subnet restriction by default
- hard PPS/Mbit/s caps
- hard duration limits
- emergency stop
- connection-pressure profiles
- DNS anomaly generator
- sanitized PCAP replay
- IDS/IPS-safe signature test library

## Feature areas 6-10

### 6. Custom scenario builder

User-defined scenarios are stored in `config.json` and use the same transient runtime engine as built-ins. The MVP accepts validated JSON steps with three actions:

- `quality` — 0-100%
- `fault` — normal, bidirectional blackhole, downstream blackhole or upstream blackhole
- `mtu` — 576-9000 bytes, or 0 to restore

A scenario is limited to 30 steps and each individual wait is capped at one hour.

### 7. Path MTU testing

The current implementation can temporarily lower the MTU of the WAN bridge and its inner/outer member interfaces. The original MTUs are retained in memory and restored when requested or when a scenario finishes.

This is intentionally described as **path MTU constriction**, not yet as a full PMTUD blackhole. A future implementation can use nftables to drop oversized IPv4/IPv6 packets and selectively suppress ICMP/ICMPv6 feedback for more exact PMTUD failure testing.

### 8. Generic SLA evaluator

A persisted vendor-neutral SLA profile defines maximum:

- latency
- jitter
- packet loss

NetEm compares these limits with the effective impairment it is injecting. Any active runtime fault also fails the generic data-plane check.

This is an **expected/injected SLA state**, not measured vendor telemetry. Future vendor adapters should display the appliance's measured SLA alongside NetEm's expected result.

### 9. Persistent history and export

Runtime events are appended to `runtime/events.jsonl` and reloaded when the application starts. The Lab Tools UI can export recent history as:

- JSON
- CSV

History includes scenario activity, faults, MTU changes, SLA changes, captures and safe security-test events. The runtime directory is excluded from Git.

### 10. Bounded packet capture

When `tcpdump` is installed, Lab Tools can capture on the configured inner or outer WAN interface.

Guardrails in the MVP:

- configured WAN interfaces only
- maximum 120-second duration
- maximum 20,000 packets
- 256-byte snap length
- one capture at a time
- explicit stop
- download of the resulting PCAP

The service needs `CAP_NET_RAW` in addition to `CAP_NET_ADMIN` for this optional feature.

## Release management

The repository uses Release Please with the `simple` release strategy.

Release state is source controlled in:

- `version.txt`
- `.release-please-manifest.json`
- `release-please-config.json`
- `CHANGELOG.md`

The GitHub Actions workflow runs against `main`. Release Please collects Conventional Commits, opens/updates a release PR, updates the version/changelog in that PR, and creates the GitHub tag/release when the release PR is merged.

The NetEm updater independently treats `origin/main` as the stable appliance update channel. This means a VM originally installed from an old feature branch can still fast-forward to the stable mainline after that feature branch is merged or deleted.

---

## External projects and dependencies

The current MVP does not vendor another GitHub project because the implemented functions map directly to Linux `tc/netem`, Linux interface counters and Flask.

Where an existing mature tool is clearly better than reimplementing functionality, the preferred integration model is to **wrap an installed tool rather than copy its code into this repository**. Likely candidates for later phases include:

- `iperf3` for controlled throughput/load generation
- `tcpreplay` for sanitized PCAP replay
- a dedicated packet generator for bounded PPS testing

Any traffic-generation integration should remain opt-in and constrained to explicitly configured lab targets.

## Persistence model

Persisted:
- WAN topology
- selected access profile
- nominal bandwidth override
- quality
- manual Custom impairment state
- editable presets

Transient by design:
- runtime faults
- scenario execution state
- runtime event history
- browser telemetry history
- security-test requests

This prevents a temporary destructive lab condition from unexpectedly returning after an application or VM restart.

## Multi-vendor principle

Vendor integrations should be implemented as adapters, not as dependencies of the impairment engine.

A future adapter interface should expose a small common model:

```json
{
  "vendor": "example",
  "members": [
    {
      "name": "WAN1",
      "health": "up",
      "latency_ms": 22,
      "jitter_ms": 4,
      "loss_pct": 0.2,
      "selected": true
    }
  ]
}
```

That allows the UI to compare injected vs observed behavior without assuming a particular vendor schema.
