# NetEm WAN Lab — Quickstart Guide

NetEm WAN Lab is a network resilience platform for firewall, routing and SD-WAN
labs. It sits transparently between the appliance under test and its WAN networks,
applies controlled impairments, and measures application outcomes with the companion
Traffic Simulator.

## Install

Use the [installation instructions in README.md](README.md#installation) for
dependencies, the Python environment and service setup. The maintained repository is:

```bash
git clone https://github.com/kriziw/netem.git
cd netem
```

## Open the interfaces

- Operator interface: `http://<netem-host>:8081/`
- Read-only customer showroom: `http://<netem-host>:8082/`

Keep the operator interface on the lab management network. See the README for
showroom configuration and deployment details.

## Configure the lab

1. Open **Settings → Setup** and map each WAN's inner and outer interfaces.
2. Connect the Traffic Simulator and controlled target under **Settings**.
3. Verify the selected traffic path through the appliance.
4. Start a session, choose a site profile and run a test from **Tests**.
5. Open the showroom on the customer display to follow application outcomes.

Each transparent WAN uses two interfaces connected by a bridge. Download and upload
shaping follow their respective egress directions; see the in-app architecture guide.

## Existing appliances

Older installations may retain the `techkarma-netem` service name, `/opt/techkarma-netem`
directory or `techkarma` service account. Use the names configured on that appliance.
A product wording update does not rename services, move installations or change accounts.

## Documentation and support

Use the in-app **Help & documentation**, [README.md](README.md), and the
[project issue tracker](https://github.com/kriziw/netem/issues).
Original code credits and licence notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
and [LICENSE](LICENSE).
