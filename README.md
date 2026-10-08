# NetEm WAN Lab

A browser-based WAN impairment emulator for firewall, routing, SD-WAN and failover labs.

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

- **One quality control per WAN**
  - Each technology preset represents nominal 100% conditions.
  - A 0–100% quality slider progressively worsens latency, jitter, loss and available bandwidth.
  - Status is shown as Excellent, Good, Fair, Poor, Critical or Down.

- **Asymmetric bandwidth simulation**
  - Independent nominal download and upload capacity per preset.
  - Useful for DSL, residential broadband, cellular and satellite links where upstream capacity is commonly lower than downstream capacity.

- **GUI preset editor**
  - Preset baseline values can be edited from a dedicated Presets menu.
  - Project defaults can be restored from the same screen.

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

As quality is reduced, the emulator progressively increases latency, jitter and packet loss and reduces both downstream and upstream capacity. The quality score is **relative to the selected access technology**: 100% Satellite remains a satellite link; it does not become equivalent to DIA.

The Presets menu lets you change the 100% baseline values used by this calculation.

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
- Quality slider
- Derived quality status
- Derived latency, jitter, packet loss, download and upload values

The Presets menu exposes the editable nominal values for each access technology. WAN aliases, the selected preset and the quality level are stored in `config.json`. When startup profile restoration is enabled, those selections are reapplied after restart.

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

This fork currently focuses on improvements useful for persistent SD-WAN and network architecture labs, including startup recovery, persisted WAN identities, editable access-technology presets, a per-WAN quality model, and asymmetric bandwidth shaping.

If you are looking for the original project or its appliance offering, please refer to the upstream repository and Techkarma documentation.

---

## License

The project remains licensed under the **MIT License**. See [LICENSE](LICENSE).

The upstream attribution above is retained to make the origin of the fork and subsequent changes clear.
