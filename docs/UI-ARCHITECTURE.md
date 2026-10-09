# UI Architecture

NetEm WAN Lab is organized around operational outcomes rather than implementation primitives.

The primary interaction model is:

> see live state → choose a test or WAN → apply a controlled condition → measure the outcome → assert requirements → retain evidence.

## Primary navigation

- **Command Center** — live topology, WAN pulse, quick controls, current test/session context and recent activity.
- **Tests** — guided resilience/security/diagnostic workflows plus advanced custom scenario definitions.
- **Analytics** — persistent + live WAN telemetry, active measurements, expected SLA and injected/measured comparison.
- **Sessions** — named validation runs and completed evidence reports.
- **Settings** — topology, access profiles, integrations, updates and system configuration.
- **Help & documentation** — lower-sidebar searchable wiki plus contextual Guide links.

Advanced WAN configuration remains available contextually rather than occupying primary navigation.

## Command Center

The Command Center is intended to remain open during a test.

Each WAN shows the selected access technology and nominal line rate, injected latency/loss/quality, expected SLA state, measured downstream/upstream throughput, traffic-driven flow animation, and a rolling throughput sparkline.

Clicking a WAN opens a drawer for quality degradation with expected-effect preview, nominal bandwidth override, bidirectional/one-way blackholes, restore, MTU constraint, and links to advanced packet behavior and packet capture.

## Progressive disclosure

Common goals are shown before Linux/networking primitives. For example, “Fail a WAN” appears before low-level loss/qdisc configuration, while correlated loss, duplication, corruption and qdisc diagnostics remain available under Advanced WAN settings.

## Tests

The Tests workspace combines the former Scenarios and Traffic & Security concepts.

Current guided tests include Progressive brownout, SLA failover, Flaky underlay, Availability stress, EICAR validation, benign beacon and bounded packet capture.

Custom scenarios support timed and conditional stages.

### Scenario actions

- quality
- fault
- mtu
- wait
- assert

### Condition sources

- expected SLA state
- active-probe success/latency
- measured traffic rate/PPS

Assertions create structured PASS/FAIL events and are included in Lab Session reports.

## Global live interaction

The UI adds live behavior only when it represents real state:

- traffic flow animation is driven by measured counter deltas;
- WAN sparklines use measured throughput;
- rate deltas show short-term change;
- running tests expose current stage and conditional wait state;
- a global Activity rail refreshes runtime events;
- Ctrl+K opens global navigation/WAN access.

## Analytics

Analytics combines three perspectives:

1. **Injected** — NetEm runtime impairment state.
2. **Measured** — Linux traffic counters and active probes.
3. **Observed** — future SD-WAN vendor adapter state.

### Persistent telemetry

A background worker stores WAN and probe samples in SQLite at runtime. Stored WAN fields include downstream/upstream Mbit/s, downstream/upstream PPS, injected latency/jitter/loss/quality, expected SLA, runtime fault and active Lab Session ID.

Default retention is seven days. Analytics can load historical ranges and append live browser samples without losing history on refresh.

### Active measurements

Supported probe types are ICMP, TCP connect, HTTP/HTTPS and DNS query.

Automatic probe source uses NetEm host routing. Because the normal transparent WAN interfaces are unnumbered, this is not treated as proof that a specific transparent WAN was traversed. Optional interface binding is available when usable L3 routing/source addressing exists.

## Lab Sessions

A Lab Session gives one validation objective a stable identity.

While active, session ID is attached to runtime events, WAN telemetry rows, active-probe samples and scenario/assertion results.

Completing a session snapshots an evidence report.

Report scoring:

- PASS — at least one assertion ran and no assertion/test failed;
- FAIL — assertion or scenario failed;
- UNSCORED — no explicit assertion ran.

Reports include assertion evidence, scenario results, WAN statistics, probe statistics and correlated events. HTML is printable and JSON is available for automation.

## APIs

Current read-only integration surfaces include:

- /api/v1/state
- /api/v1/telemetry
- /api/v1/history
- /api/v1/probes
- /api/v1/events
- /metrics
- completed Session report JSON routes

## Vendor-neutral principle

Core impairment, measurement, scenario and evidence models remain vendor-neutral.

A future adapter layer should normalize member/link health, observed latency/jitter/loss, appliance SLA state, selected path and transition timestamps.

Fortinet is the recommended first adapter, but Cisco, Palo Alto, Juniper, VMware/VeloCloud, Versa and others should feed the same common model.

## Next architecture milestones

See docs/ROADMAP.md. The next priorities after the measurement/evidence milestone are:

1. normalized vendor adapters, Fortinet first;
2. parallel multi-WAN scenarios;
3. bounded traffic generation;
4. session comparison analytics;
5. qdisc/update self-verification;
6. remote probe agent for true appliance-side independent measurements.

## Backward compatibility

- /dashboard renders the Command Center.
- /scenarios and /traffic-security redirect to Tests.
- /lab redirects to Tests.
- existing control routes and config.json remain compatible.
