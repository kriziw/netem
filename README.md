# NetEm WAN Lab

A browser-based WAN impairment emulator for firewall, routing, SD-WAN and failover labs.

> **Lab use only.** This fork is maintained and extended by **Kristofer Wohlgang** for network architecture and SD-WAN lab use.

This repository is a fork of **Techkarma NetEm** by [techkarma-no](https://github.com/techkarma-no/techkarma-netem). The original project provides the core Flask UI, transparent Linux bridge design and `tc/netem`-based shaping model. This fork keeps that foundation and extends it for persistent, repeatable virtual lab use.

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
  - Bandwidth overrides remain part of the normal profile state; they do **not** put the WAN into Custom mode.
  - Quality degradation is calculated from the selected WAN line rate, while the profile continues to define latency, jitter, loss and degradation behaviour.
  - Useful for DSL, residential broadband, cellular and satellite links where upstream capacity is commonly lower than downstream capacity.

- **GUI preset editor**
  - Preset baseline values can be edited from a dedicated Presets menu.
  - Each preset can select its degradation behaviour: DIA, DSL, broadband, mobile or satellite.
  - Project defaults can be restored from the same screen.

- **In-app updates**
  - A dedicated Updates page checks the configured Git remote.
  - Updates are fast-forward only and refuse to run when tracked files have local changes.
  - After updating, the process restarts through systemd.

- **Safer service behavior**
  - Flask debug mode is disabled for normal application startup.

The technology presets are representative lab profiles rather than claims about any specific ISP or carrier. They are intended as useful starting points. Edit their nominal values in the Presets menu, then change each WAN's simulated condition with the quality slider.

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
sudo apt install -y python3 python3-venv python3-pip iproute2 bridge-utils git
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

# The application needs CAP_NET_ADMIN to create bridges and qdiscs.
AmbientCapabilities=CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN
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

The Dashboard includes representative presets:

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

The preset table defines the **100% / nominal** state. The Dashboard stores one quality value per WAN:

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

These are lab-oriented heuristics, not carrier SLAs or standards-defined mappings. The Presets menu lets you change both the 100% baseline values and the degradation behaviour used by a preset.

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

The Dashboard exposes:

- Access preset
- Independent nominal download/upload bandwidth selectors
- Quality slider
- Derived quality status
- Manual sliders for latency, jitter, packet loss, download and upload
- Automatic **Custom** mode when an impairment/performance slider is changed
- Integer Mbit/s bandwidth values for compatibility with the traffic shaper

The nominal bandwidth selectors are deliberately separate from Custom mode. For example, a DIA profile can be configured as a 200/100 Mbit/s circuit while still remaining at 100% quality and using DIA latency/jitter/loss characteristics. Reducing quality then degrades performance from that 200/100 Mbit/s baseline rather than from the preset's original 1000/1000 Mbit/s line rate.

For example, a WAN can remain on the 5G preset at 100% quality, then have only jitter manually increased. The WAN becomes Custom while the remaining values stay at their current derived values. Moving the quality slider again deliberately discards those custom values and recalculates the full profile from the selected preset and quality.

The Presets menu exposes the editable nominal values for each access technology. WAN aliases, selected preset, per-WAN nominal bandwidth overrides, quality level, mode, and any active custom overrides are stored in `config.json`. When startup profile restoration is enabled, that state is reapplied after restart.

---

## In-app updates

The **Updates** page can check and install updates from the Git repository configured as `origin`.

The updater:

- follows the currently checked-out branch
- fetches that branch from `origin`
- refuses to update when tracked files have local modifications
- refuses diverged histories
- installs only a Git fast-forward
- exits the application after a successful update so systemd can restart it

For automatic restart after an in-app update, the service should use:

```ini
Restart=on-failure
```

or `Restart=always`.

Untracked runtime files such as `config.json` and `.venv/` are ignored by Git and are not replaced by application updates.

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
- Prefer `CAP_NET_ADMIN` over running the application as unrestricted root where practical.

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
