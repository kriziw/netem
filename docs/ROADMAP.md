# NetEm WAN Lab: available now and planned improvements

SQLite persistence is already implemented. This roadmap separates current capabilities from additional work; planned items are not release-date commitments.

## Available now

- **Persistent history:** SQLite WAN, host and probe measurements in `runtime/telemetry.db`, sampled approximately once per second with seven-day rolling retention. Analytics supports historical ranges. Session report snapshots live in `runtime/sessions.json` (up to 100 sessions). Detailed test records and downloads live in `runtime/test-records/` and are not automatically deleted.
- **Application traffic and experience:** simulated users and continuous application checks through configured simulator paths to the controlled target, with measured application outcomes in tests and the showroom.
- **Active measurements:** ICMP, TCP-connect, HTTP/HTTPS and DNS probes, saved results and failure/recovery events. Automatic host routing does not prove that a transparent WAN was crossed; interface binding needs usable Layer-3 routing.
- **Conditional tests and evidence:** timed stages, `wait`, `assert`, workload `start`/`adjust`/`stop`, named phases, saved session reports and downloadable per-test records. Conditions support expected SLA, probes, traffic, application experience and measured steering. Waits/assertions have bounded timeouts and stop/continue handling.
- **Appliance inventory and FortiGate monitoring:** persistent inventory, management health/licence observations, alerts and test-start checks. Other vendors support inventory and routing; detailed vendor-reported SD-WAN SLA/path selection is still planned.
- **Verified paths and controlled target:** workload route verification, source-bound path health checks and HTTP/UDP application measurements.
- **Shaping diagnostics:** injected settings beside kernel-reported shaping, queue/drop measurements and host health. Temporary MTU changes and directional/bidirectional outages are available.
- **Component updates:** shared update check for NetEm, Traffic Simulator and controlled target, separate installs, and NetEm local-change/fast-forward safeguards.

## Planned improvements

### Appliance-reported SD-WAN behaviour

Extend vendor integrations with WAN SLA metrics, selected paths and transition events. Existing FortiGate management health/licence monitoring is the foundation, not a future first adapter. Add equivalent monitoring for further vendors.

### Parallel WAN actions and richer orchestration

Simultaneous multi-WAN impairments, branching and capture triggers. Sequential condition-based waits and assertions already work.

### Session comparison and evidence management

Side-by-side comparisons of completed runs, configurable retention, guided backup/restore and packet-capture report attachments. Storage, report snapshots and test-record downloads already exist.

### Additional traffic and fault tools

Guarded iperf3 workflows, PCAP replay and dedicated PMTUD-blackhole simulation. Application traffic generation, MTU changes and runtime outages already exist. An MTU change is not a dedicated PMTUD-blackhole implementation.

### Stronger shaping and update checks

Comprehensive requested-versus-installed shaping validation and automatic service-permission/restart-policy preflight. Kernel shaping readback and Git update safeguards already exist.

### Additional remote probe locations

Independently deployed probe-only agents at further locations. Simulator-to-controlled-target checks already exist; host-routed probes still cannot establish a transparent WAN path on their own.

### Shared-lab access and scale

Operator sign-in, roles, scoped NetEm API access and dynamic WAN setup beyond the current two-WAN form. Existing simulator API credentials and FortiGate monitoring tokens are separate from future operator access control.

## What the next observation layer adds

Appliance-reported SLA/path events would correlate injected conditions and application outcomes with the appliance's own detection, path-selection and recovery decisions. Measured application performance and inferred traffic steering already exist; vendor-reported SD-WAN decisions remain additional work.
