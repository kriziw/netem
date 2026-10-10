# NetEm WAN Lab

A browser-based WAN impairment emulator for firewall, routing, SD-WAN and failover labs.

> **Lab use only.** This fork is maintained and extended by **Kristofer Wohlgang** for network architecture and SD-WAN lab use.

This repository is a fork of **Techkarma NetEm** by [techkarma-no](https://github.com/techkarma-no/techkarma-netem). The original project provides the core Flask UI, transparent Linux bridge design and `tc/netem`-based shaping model. This fork keeps that foundation and extends it for persistent, repeatable virtual lab use.

## Resilience platform: top 10

The current development direction is vendor-neutral: impairments are applied to the network path itself rather than relying on any one SD-WAN vendor's API.

1. **Scenario engine** — repeatable brownout, failover, one-way failure and availability-stress sequences.
2. **Runtime fault injection** — bidirectional or one-way blackholes while Ethernet link state remains up.
3. **Advanced packet impairments** — correlated loss, packet duplication, corruption and reordering in addition to delay/jitter/loss/bandwidth.
4. **Live observability + vendor-neutral integration** — live interface telemetry, runtime event history, JSON state/telemetry APIs and Prometheus metrics.
5. **Safe security & traffic events** — harmless EICAR delivery, a benign beacon callback sink and a DDoS-impact scenario that simulates availability degradation without generating attack traffic.
6. **Custom scenario builder** — persist reusable quality/fault/MTU sequences without changing application code.
7. **Path MTU testing** — temporarily constrain a WAN path to exercise MTU-sensitive applications and tunnels.
8. **Generic SLA evaluator** — compare injected latency/jitter/loss against vendor-neutral pass/fail thresholds before checking the SD-WAN appliance's own telemetry.
9. **Persistent history & export** — retain lab events across service restarts and export them as JSON or CSV.
10. **Bounded packet capture** — short diagnostic PCAPs on configured WAN interfaces with duration, packet-count and snap-length limits.

The current UI organizes these capabilities into dedicated operational workspaces rather than a single Lab Tools page. The implementation deliberately avoids coupling the core to Fortinet, Cisco, Palo Alto, Juniper, VMware/VeloCloud or another vendor. Vendor-specific adapters can be layered on top of the normalized API later.

See [docs/RESILIENCE-LAB.md](docs/RESILIENCE-LAB.md) for the impairment architecture, [docs/UI-ARCHITECTURE.md](docs/UI-ARCHITECTURE.md) for the interface model, and [docs/ROADMAP.md](docs/ROADMAP.md) for the prioritized next capabilities.

## Enterprise UI

Optional appliance branding supports private names, logos, favicons, palettes
and locally hosted fonts without putting corporate assets in the repository.
See [the branding guide](docs/BRANDING.md).

The interface uses progressive disclosure so the full feature set is available without making the normal workflow feature-centric:

- **Command Center** — primary live workspace with clickable WAN paths, traffic-flow state, sparklines, quick quality/bandwidth/fault/MTU controls and recent activity.
- **Tests** — guided brownout, failover, unstable-link, safe security and packet-capture workflows. Advanced scenario JSON remains available only when needed.
- **Analytics** — rolling live throughput/PPS/impairment charts, generic SLA evaluation and persistent event history.
- **Sessions** — named validation runs that correlate runtime events across tests and manual actions.
- **Settings** — topology, access profiles, integrations, release/update management and other appliance-level configuration.
- **Help & documentation** — searchable in-app wiki, moved out of primary navigation and available from the lower sidebar plus contextual Guide links.

A global **Activity** rail is available from every page, and **Ctrl+K** opens a command palette for fast navigation and WAN access. The UI has no external CSS, JavaScript or font dependency, so it remains usable on isolated lab networks.

### Showroom display

Running `python app.py` serves the existing operator interface on **8081** and a
separate, read-only showroom dashboard on **8082**. Open
`http://<netem-host>:8082/` on a showroom screen and use the browser's full-screen
mode (F11). No login or external assets are required on the local lab network.

The dashboard answers three questions: who the client site is (industry,
sub-industry, function, size, criticality, targets and typical WAN lines, plus
the lab session); what runs on the network (the current test with its phases and
time to the next phase, the simulated workload and live traffic and health per
WAN); and what users get (experience against the targets, where and why problems
happen, SD-WAN steering, and a per-phase report when a test ends). It refreshes every
two seconds, automatically rotates pages when there are more WANs than fit, and
shows unavailable measurements as gaps rather than zero. Connection failures
blank live rates and label retained state as last known; reconnection is automatic.

The viewer runs in the same process as the operator UI so runtime changes appear
immediately without a second set of background workers. Its separate Flask app
exposes only the dashboard, its two assets and a presentation-only snapshot API.
Operator routes and configuration/secrets are not exposed on the showroom port;
all methods other than GET/HEAD are rejected.

Optional environment variables (add `Environment=...` lines to the systemd service):

- `NETEM_SHOWROOM_PORT=8082` — choose a different port; `0` disables the viewer.
- `NETEM_SHOWROOM_HOST=0.0.0.0` — choose a bind address, such as a showroom-facing IP.

Allow the showroom port through the appliance firewall for the showroom network.
The operator interface on 8081 remains fully functional and should be reachable
only by operators through your network/firewall configuration. The read-only
listener does not impose access restrictions on the separate operator port.
Custom WSGI deployments must provide their own listener for `app.showroom_app`
in the same process with the existing runtime workers; the dual listener is
started by the documented `python app.py` entry point.

### Measurement, persistence and evidence

The platform now includes a first-party measurement/evidence layer:

- **Active measurements** — bounded ICMP, TCP-connect, HTTP/HTTPS and DNS probes with persisted results and failure/recovery events.
- **Persistent telemetry** — SQLite WAN/probe time series in `runtime/telemetry.db`, sampled server-side and retained for seven days by default.
- **Conditional scenarios** — `wait` and `assert` stages can react to expected SLA state, active-probe results or measured traffic.
- **Session reports** — completed Lab Sessions produce PASS/FAIL/UNSCORED evidence with assertions, test results, WAN statistics, probe success/latency percentiles and JSON/printable report output.

Active probes are honest about topology scope: automatic-source probes follow the NetEm host routing table. On a fully transparent WAN with unnumbered bridge members this does not by itself prove that the selected WAN path was crossed; interface binding requires a usable L3 source/route.

---

## What this fork adds

- **Persistent WAN topology**
  - Optional automatic recreation of configured Linux bridges when the application starts.
  - Prevents WAN paths disappearing after a VM reboot.

- **Persistent WAN profile state**
  - WAN aliases, selected access preset and quality level are stored per link.
  - The selected profile can be reapplied automatically after reboot.

- **WAN technology presets**
  - DIA
  - DSL
  - Broadband
  - 4G
  - 5G
  - Satellite

- **Quality control + custom overrides per WAN**
  - Each technology preset represents nominal 100% conditions.
  - A 0–100% quality slider uses staged, access-specific degradation curves rather than reducing every metric linearly.
  - DIA, DSL, fixed broadband, mobile and satellite links can therefore deteriorate in different ways.
  - Status is shown as Excellent, Good, Fair, Poor, Critical or Down.
  - Latency, jitter, packet loss, download and upload are adjustable with sliders.
  - Moving any individual parameter slider switches that WAN to **Custom** mode.
  - In Custom mode the quality slider is visually muted but still usable; moving it again removes the manual overrides and returns to quality-derived values.

- **Asymmetric bandwidth simulation**
  - Independent nominal download and upload capacity per preset.
  - Each WAN can override its nominal download/upload line rate without changing the selected access technology.
  - A **Reset to profile defaults** control clears both per-WAN bandwidth overrides in one action.
  - Bandwidth overrides remain part of the normal profile state; they do **not** put the WAN into Custom mode.
  - Quality degradation is calculated from the selected WAN line rate, while the profile continues to define latency, jitter, loss and degradation behaviour.
  - Useful for DSL, residential broadband, cellular and satellite links where upstream capacity is commonly lower than downstream capacity.

- **GUI preset editor**
  - Preset baseline values can be edited under Settings → Access Profiles.
  - Each preset can select its degradation behaviour: DIA, DSL, broadband, mobile or satellite.
  - Project defaults can be restored from the same screen.

- **In-app updates**
  - Settings → Application & Updates checks the configured stable Git channel.
  - Updates are fast-forward only and refuse to run when tracked files have local changes.
  - After updating, the process restarts through systemd.

- **Safer service behavior**
  - Flask debug mode is disabled for normal application startup.

The technology presets are representative lab profiles rather than claims about any specific ISP or carrier. They are intended as useful starting points. Edit their nominal values under Settings → Access Profiles, then change each WAN's simulated condition from WAN Links.

---

## Typical use cases

- Fortinet SD-WAN labs
- Cisco SD-WAN testing
- Palo Alto / Prisma SD-WAN testing
- Juniper WAN testing
- Dual-ISP failover
- SLA / health-check tuning
- Broadband vs DIA behavior
- 4G / 5G backup links
- Satellite WAN simulation
- Packet loss and jitter testing
- Bandwidth-constrained application testing

---

## Architecture

Each emulated WAN is a transparent Layer-2 path:

```text
Test device / firewall
        |
    inner NIC
        |
   +-----------+
   | br-wanX   |
   +-----------+
        |
    outer NIC
        |
Upstream / ISP router
```

The application uses:

- Linux bridges for transparent forwarding
- `tc netem` for latency, jitter and packet loss
- `tbf` for bandwidth limiting

Two WAN links are supported by the current GUI:

```text
WAN 1: inner NIC <-> br-wan1 <-> outer NIC
WAN 2: inner NIC <-> br-wan2 <-> outer NIC
```

---

## Installation

Debian 12 is the recommended base.

### 1. Install dependencies

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip iproute2 bridge-utils git iputils-ping

# Optional: enables bounded PCAP capture from Tests
sudo apt install -y tcpdump
```

### 2. Clone this fork

```bash
cd /opt
sudo git clone https://github.com/kriziw/netem.git
sudo chown -R $USER:$USER netem
cd netem
```

### 3. Create the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install flask
deactivate
```

### 4. Run manually

```bash
cd /opt/netem
.venv/bin/python app.py
```

Open:

```text
http://<vm-ip>:8081/
```

---

## systemd service

For a lab appliance, running NetEm as a service is recommended.

Create:

```text
/etc/systemd/system/netem.service
```

with:

```ini
[Unit]
Description=NetEm WAN Emulator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/netem
ExecStart=/opt/netem/.venv/bin/python app.py
Restart=on-failure
RestartSec=3

# CAP_NET_ADMIN is required for bridges/qdiscs/MTU changes.
# CAP_NET_RAW is only required for the optional bounded packet-capture feature.
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now netem
sudo systemctl status netem
```

---

## First-time setup

Open the **Setup** page and configure the two sides of each WAN.

Example:

```text
WAN1 inner: enp6s19
WAN1 outer: enp6s20

WAN2 inner: enp6s21
WAN2 outer: enp6s22
```

The application creates:

```text
br-wan1
br-wan2
```

### Startup persistence

The Setup page includes two independent options:

**Restore WAN bridges when the application starts**

Recommended for persistent labs. Linux bridges are runtime objects and otherwise disappear after a reboot.

**Restore selected presets and quality on startup**

Recommended for repeatable labs. When enabled, each WAN's persisted access preset and quality level are reapplied after reboot. Disable it if you want the topology restored but the links to start without shaping.

---

## WAN presets

The built-in access-profile set includes representative presets:

| Profile | Nominal delay | Nominal jitter | Nominal loss | Download | Upload |
| --- | ---: | ---: | ---: | ---: | ---: |
| DIA | 5 ms | 1 ms | 0% | 1000 Mbit/s | 1000 Mbit/s |
| DSL | 25 ms | 8 ms | 0.1% | 100 Mbit/s | 20 Mbit/s |
| Broadband | 15 ms | 5 ms | 0.1% | 300 Mbit/s | 50 Mbit/s |
| 4G | 45 ms | 20 ms | 0.5% | 80 Mbit/s | 20 Mbit/s |
| 5G | 20 ms | 8 ms | 0.2% | 300 Mbit/s | 50 Mbit/s |
| Satellite | 300 ms | 30 ms | 0.5% | 100 Mbit/s | 20 Mbit/s |

These values are deliberately generic. Actual broadband, cellular and satellite performance varies significantly by provider, access technology, RF conditions, congestion, geography and service tier.

For example, a GEO satellite profile will generally need far more delay than LEO satellite service. The preset should therefore be treated as a quick starting point rather than a standards-based definition.

### Quality model

The preset table defines the **100% / nominal** state. Each configured WAN stores one quality value:

| Quality | Status |
| ---: | --- |
| 90–100% | Excellent |
| 75–89% | Good |
| 50–74% | Fair |
| 25–49% | Poor |
| 1–24% | Critical |
| 0% | Down |

As quality is reduced, the emulator uses a **staged, access-specific curve with exponential easing between breakpoints**. The quality score is **relative to the selected access technology**: 100% Satellite remains a satellite link; it does not become equivalent to DIA.

The model deliberately avoids changing every metric at the same time or by the same percentage. For example:

- DIA remains very clean while healthy, but degradation becomes deliberately steep once quality falls into the poor range. With the default 1 Gbit/s DIA preset, 40% quality is approximately 40 ms delay, 18 ms jitter, 3% loss and 350/350 Mbit/s.
- DSL can show rising jitter/errors before a substantial line-rate reduction.
- Shared broadband tends to show queueing/jitter before material loss.
- 4G/5G capacity and jitter typically move earlier as RF conditions or congestion worsen; sustained loss becomes prominent later.
- Satellite degradation emphasizes latency variation/jitter first, then capacity, with sharper loss at poor quality.

These are lab-oriented heuristics, not carrier SLAs or standards-defined mappings. Settings → Access Profiles lets you change both the 100% baseline values and the degradation behaviour used by a preset.

---

## How shaping works

Delay, jitter and packet loss are applied on the configured **inner interface**.

Bandwidth is directional:

- **Download** is limited on inner-interface egress, toward the firewall/test device.
- **Upload** is limited on outer-interface egress, toward the WAN/upstream router.

Conceptually:

```bash
# Impairment + download limit
tc qdisc add dev <inner-nic> root netem delay ...
tc qdisc add dev <inner-nic> parent ... tbf rate <download>

# Independent upload limit
tc qdisc add dev <outer-nic> root netem
tc qdisc add dev <outer-nic> parent ... tbf rate <upload>
```

The Command Center and advanced WAN controls expose:

- Access preset
- Independent nominal download/upload bandwidth selectors
- Quality slider
- Derived quality status
- Manual sliders for latency, jitter, packet loss, download and upload
- Automatic **Custom** mode when an impairment/performance slider is changed
- Integer Mbit/s bandwidth values for compatibility with the traffic shaper

The nominal bandwidth selectors are deliberately separate from Custom mode. For example, a DIA profile can be configured as a 200/100 Mbit/s circuit while still remaining at 100% quality and using DIA latency/jitter/loss characteristics. Reducing quality then degrades performance from that 200/100 Mbit/s baseline rather than from the preset's original 1000/1000 Mbit/s line rate.

For example, a WAN can remain on the 5G preset at 100% quality, then have only jitter manually increased. The WAN becomes Custom while the remaining values stay at their current derived values. Moving the quality slider again deliberately discards those custom values and recalculates the full profile from the selected preset and quality.

Settings → Access Profiles exposes the editable nominal values for each access technology. WAN aliases, selected preset, per-WAN nominal bandwidth overrides, quality level, mode, and any active custom overrides are stored in `config.json`. When startup profile restoration is enabled, that state is reapplied after restart.

---

## Releases and in-app updates

Releases are managed with **Release Please** and semantic versions. The current version is stored in `version.txt` and is displayed throughout the UI. Release Please also maintains `CHANGELOG.md`, creates release PRs, tags merged releases and creates GitHub Releases.

The release workflow runs on pushes to `main`. Use Conventional Commit prefixes when merging changes:

```text
feat: add a new lab capability
fix: correct WAN restore behavior
perf: improve telemetry polling
docs: document an integration
chore: maintenance
```

A `feat:` normally drives a minor version bump, `fix:` a patch bump, and a breaking change a major bump.

The repository contains:

```text
.github/workflows/release-please.yml
release-please-config.json
.release-please-manifest.json
version.txt
CHANGELOG.md
```

The workflow uses `RELEASE_PLEASE_TOKEN` when that repository secret exists, otherwise it falls back to the workflow's GitHub token. If the fallback token is used, GitHub Actions must be permitted to create pull requests in the repository settings.

### In-app updater

The **Updates** page treats `origin/main` as the stable update channel regardless of which local branch the appliance was originally installed from. This fixes older installations that remained on a now-merged/deleted feature branch.

The page shows:

- installed semantic version
- stable semantic version from `origin/main`
- installed commit
- number of stable commits available
- tracked-local-change state

The updater:

- fetches `origin/main`
- refuses to update when tracked files have local modifications
- refuses diverged histories
- installs only a Git fast-forward
- exits after a successful update so systemd can restart it

For automatic restart after an in-app update, the service should use:

```ini
Restart=on-failure
```

or `Restart=always`.

Runtime state such as `config.json`, `.venv/`, persistent event history and PCAP captures is ignored by Git and is not replaced by application updates.

---

## Verify bridges

To inspect the current Layer-2 topology:

```bash
bridge link
brctl show
```

Example:

```text
br-wan1
  enp6s19
  enp6s20

br-wan2
  enp6s21
  enp6s22
```

---

## Security

The application directly controls Linux networking and therefore requires elevated networking capabilities.

Recommended practices:

- Keep the management interface separate from the emulated WAN interfaces.
- Do not expose the Flask application directly to the public Internet.
- Put remote access behind an authenticated reverse proxy or trusted access layer.
- Prefer Linux capabilities over running the application as unrestricted root where practical.
- If packet capture is enabled, the service additionally needs `CAP_NET_RAW`; otherwise omit it.
- Packet capture is bounded to configured WAN interfaces, a maximum of 120 seconds, 20,000 packets and a 256-byte snap length.

---

## Upstream project and attribution

This project is derived from:

**Techkarma NetEm**  
Original repository: https://github.com/techkarma-no/techkarma-netem  
Original author/project: Techkarma / techkarma-no

The original project established the WAN emulator concept, Flask interface, bridge handling and Linux `tc/netem` implementation on which this fork is based.

This fork currently focuses on improvements useful for persistent SD-WAN and network architecture labs, including startup recovery, persisted WAN identities, editable access-technology presets, staged access-specific quality models, manual impairment sliders, asymmetric bandwidth shaping, and in-app updates.

If you are looking for the original project or its appliance offering, please refer to the upstream repository and Techkarma documentation.

---

## License

The project remains licensed under the **MIT License**. See [LICENSE](LICENSE).

The upstream attribution above is retained to make the origin of the fork and subsequent changes clear. The application UI identifies this as a lab-use fork maintained and extended by Kristofer Wohlgang, while crediting Techkarma NetEm as the project foundation.

### Traffic Simulator integration validation

The simulator integration uses direct HTTPS connections with certificate fingerprint validation before the API key is sent. Self-signed lab certificates can use a discovered fingerprint or trust on first connection. Verify certificate changes before replacing the trusted fingerprint in Integrations. HTTP redirects are rejected, and API errors redact the configured key.

Custom scenarios execute `traffic_generator` actions (`start`, `adjust`, `stop`) through the simulator API. DEM conditions use the simulator's `endpoint_experience.score` for `experience_score`; unavailable or truncated DEM cannot pass an assertion. Workloads started by a scenario are stopped during scenario cleanup, including failure/cancellation; a scenario that only adjusts an existing workload leaves that workload running.

For the dual-NIC simulator, controlled target routing, FortiGate policy/SNAT and verification steps, see the [Traffic Simulator deployment guide](https://github.com/kriziw/netem-traffic-simulator#installation--controlled-target). Simulator API keys are stored separately from `config.json`; the browser controls require session form tokens. NetEm remains a management-network lab application and has no general user authentication layer.

Run regression coverage with `python -m unittest discover -s tests -v`.

### Telemetry accuracy and measurement scope

- Download = inner-port TX; upload = outer-port TX. Rates use counter deltas
  over monotonic elapsed time, in decimal Mbit/s and packets/s. They represent
  traffic transmitted by the bridge ports, including background/control traffic,
  rather than a capacity speed test or application goodput. Driver counters and
  offloads can affect packet granularity; Ethernet FCS and physical-layer overhead
  are not part of the standard byte counters.
- The first observation, failed reads, counter resets, interface replacement or
  remapping, and service restarts establish a baseline. They show missing rates
  rather than artificial zero traffic or recovery spikes. Idle is a valid zero
  delta; carrier-down is displayed as failed. Historical averages and session
  summaries exclude newly recorded invalid rate intervals.
- `/api/v1/telemetry` includes a process `sampler_id`, per-link monotonic timestamps,
  interface indices and `counters_valid`. Unreadable raw counters are JSON `null`;
  Prometheus omits unavailable byte counters. `/api/v1/history` returns `null`
  rates for buckets without valid measurements. Existing database history is
  retained, but validity cannot be recovered retrospectively for older samples.
- Delay, jitter, loss, quality and model SLA are requested impairment-model
  values, not measured path health and not proof that the kernel applied the
  configuration. The Command Center labels these explicitly. The tc view reports
  installed qdisc parameters; active probes report measured response times.
- ICMP measures echo round-trip time. TCP measures resolution plus connection;
  HTTP includes resolution, connection, TLS (for HTTPS) and the response status
  line; DNS measures a resolver transaction. These have different meanings and
  should not be combined into one latency metric. Automatic probes follow the
  NetEm host routing table and can use management routing: assigning a WAN ID
  does not establish that a probe crossed that transparent WAN. Verify the path
  or use an endpoint behind the SD-WAN appliance for end-user measurements.

Counter semantics: [Linux interface statistics](https://www.kernel.org/doc/html/latest/networking/statistics.html).
ICMP semantics: [iputils ping manual](https://man7.org/linux/man-pages/man8/ping.8.html).
