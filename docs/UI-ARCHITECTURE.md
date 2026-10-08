# UI Architecture

NetEm WAN Lab is designed as an operational resilience-testing platform rather than a configuration-centric frontend.

The user experience separates **operate**, **observe**, and **configure** workflows so future capabilities can be added without turning one page into a collection of unrelated cards.

## Navigation

### Operate

- **Overview** — primary live operations screen.
- **WAN Links** — configure link identity/capacity/quality and inject transient faults.
- **Scenarios** — scenario library, execution and custom scenario authoring.
- **Traffic & Security** — safe security-test events, bounded capture and future controlled traffic generation/replay.

### Observe

- **Analytics** — rolling live traffic/impairment charts, SLA state and event history.
- **Integrations** — REST/Prometheus surfaces and future vendor adapters.

### Configure

- **Settings** — system-level configuration hub.
  - Topology & interfaces
  - Access profiles
  - Application & updates
  - SLA configuration
  - API/integration references
  - Future retention/authentication/backup controls

## Design language

The UI uses a self-contained dark enterprise design system with no external font, CSS or JavaScript dependencies.

Principles:

- status and telemetry are more prominent than forms;
- configuration actions open focused drawers or dedicated settings pages;
- runtime/destructive impairments remain visually distinct;
- active faults are visible globally;
- future capabilities have reserved locations rather than being bolted into unrelated screens;
- the browser remains usable on a disconnected/offline lab network.

## Overview

Overview is intended to stay open during a test.

It includes:

- lab health KPIs;
- current scenario/capture state;
- a live path topology;
- injected latency/jitter/loss;
- live per-WAN traffic;
- rolling downstream charts;
- recent event timeline;
- links to WAN controls and deeper analytics.

The topology is deliberately vendor-neutral: the left endpoint is the appliance under test and the right endpoint is the upstream network.

## WAN Links

Each WAN is represented by a concise status card.

Detailed configuration is moved into a right-side drawer with tabs:

1. **Profile** — access technology and nominal line rate.
2. **Quality** — relative quality plus manual impairment controls.
3. **Advanced** — correlation, duplication, corruption and reordering.
4. **Faults** — blackholes and path MTU constraints.
5. **Diagnostics** — Linux interfaces, bridge and qdisc state.

This keeps monitoring visible while configuration is being changed.

## Scenarios

Scenarios have their own workspace rather than sharing a generic Lab Tools page.

Current model:

- built-in scenario library;
- visual stage timeline;
- target-WAN selection;
- active-scenario progress;
- custom JSON scenario builder;
- scenario/fault/MTU event timeline.

Future reserved model:

- graphical stage editor;
- parallel WAN actions;
- conditions such as "wait until SLA failed";
- pass/fail assertions;
- scheduled runs;
- scenario result reports.

## Traffic & Security

Security and traffic generation are separated from impairment controls.

Current safe capabilities:

- EICAR anti-malware test artifact;
- benign beacon/callback sink;
- availability-stress scenario;
- bounded tcpdump capture.

Reserved future capabilities:

- bounded iperf3 traffic generator;
- rate/target/duration guardrails;
- sanitized tcpreplay workflow;
- DNS anomaly profiles;
- safe IDS/IPS signature tests.

## Analytics

The live Analytics page keeps a rolling browser-side sample window and displays:

- downstream/upstream throughput;
- RX/TX PPS;
- injected latency/jitter;
- packet loss/quality;
- expected generic SLA state;
- persistent event history.

The current live charts intentionally use the existing REST APIs, so no new runtime dependency is required.

Future phases can replace or supplement polling with Server-Sent Events while keeping the same UI model.

## Injected vs observed

The long-term analytics model separates:

- **NetEm injected state** — what the impairment engine applied;
- **vendor observed state** — what an SD-WAN/firewall reports through an optional adapter.

A normalized vendor-adapter model can support Fortinet, Cisco, Palo Alto, Juniper, VMware/VeloCloud, Versa and others without coupling core NetEm operation to any vendor.

Future scenario reports can calculate:

- SLA detection delay;
- path-selection/failover delay;
- total reaction time;
- recovery convergence time;
- injected vs measured latency/jitter/loss.

## Data evolution

The current application exposes:

- `/api/v1/state`
- `/api/v1/telemetry`
- `/api/v1/events`
- `/metrics`

The browser maintains a rolling live view.

A future self-contained historical analytics layer should use SQLite for local time-series/session storage, while Prometheus remains an external export integration rather than a mandatory internal dependency.

## Backward compatibility

The old `/lab` route redirects to Scenarios.

The legacy `index` endpoint remains available at `/dashboard` for older bookmarks/integrations, but renders the new Overview experience.

Existing control routes and configuration formats are retained so the redesign does not require a migration of `config.json`.
