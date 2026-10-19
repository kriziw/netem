# FortiGate lab provisioning

Open **Integrations → Set up FortiGate lab**. Administrators can connect an inventory FortiGate using its HTTPS management address and REST API token, discover its configuration, and configure an IPv4 lab with **one, two or three WAN links**. Provisioning runs independently of the impairment engine. Other vendor adapters remain roadmap items; Fortinet SLA telemetry and selected-path event correlation also remain planned.

## Connect and discover

1. In **Appliances**, save a Fortinet appliance, management HTTPS origin, VDOM and API token. Tokens stay in private credential files and are never included in browser responses, previews, journals or exports. The token field stays blank on subsequent page loads.
2. Create a FortiGate REST API administrator restricted by trusted hosts to NetEm's management address. Discovery requires read access to system/network, routing and firewall configuration, plus system status. Applying needs interface/network, routing and firewall write permissions for the selected VDOM. A monitoring-only token cannot provision. Use a custom least-privilege profile rather than a generic full administrator. Permission names and access to global interfaces vary with FortiOS and VDOM setup; validate the account on the lab appliance.
3. Use a trusted HTTPS certificate, or save an independently verified SHA-256 certificate fingerprint and enable pinned self-signed trust in Appliances. There is no insecure "skip verification" option. Pin verification happens before sending the token; redirects are never followed. Authentication uses the `Authorization: Bearer` header, never a URL token.
4. Select the appliance and click **Discover appliance**. NetEm reads system identity, interfaces, routes, policies, address objects, SD-WAN, zones, software/hardware switches, DHCP servers, policy routes and central NAT mode. Unsupported hardware-switch tables may be absent on VM builds; other failures stop discovery. Oversized/truncated configuration is refused, rather than treated as complete.

The configuration model targets FortiOS **7.2, 7.4 and 7.6**, including the 7.6.7 lab version. The API schemas are cross-checked against Fortinet's official SDK and CLI reference. Automated tests use a simulated CMDB; real-appliance acceptance remains required before relying on a specific firmware/model combination.

## Confirm topology

Discovery suggests the management port when its address matches the API address, LAN/WAN ports from their roles, existing interface addresses and WAN gateways from SD-WAN members or static routes. Previously verified NetEm lab settings are suggested on subsequent discovery. The inventory's controlled target is offered as the health-check destination. **These are suggestions, not cable detection.** Confirm the management port manually when using DNS, NAT or secondary-address management.

Choose a dedicated management port, one LAN and one to three independent physical/VM WAN ports. Supply missing static IPv4 addresses and gateways. Choose the controlled target subnet beyond the WAN gateways and an ICMP host reachable through every WAN. Choose source NAT unless upstream routers already have return routes to the LAN. WAN order sets preference. SLA thresholds are latency 150 ms, jitter 30 ms and packet loss 2%.

The first adapter deliberately provisions scoped lab destinations, not a default route. The route and allow policy cover only the selected destination subnet. It does not create virtual NICs, Proxmox switches, NetEm impairment bridges, upstream routes, target services or simulator interface addressing. Cable each FortiGate WAN through its intended NetEm bridge; the simulator connects on the LAN side.

Both dedicated appliances and existing VDOMs are supported when selected ports are available. Switch/aggregate membership, DHCP references, policy routes, existing routes or policies tied directly to selected WAN ports, and unmanaged SD-WAN members are explicit conflicts requiring reconciliation. They are not automatically deleted or migrated. VRFs, secondary addresses, VLAN/aggregate/tunnel lab ports and central NAT require manual setup. Management ports/subnets and overlapping LAN/WAN subnets are protected.

## Review and apply

The preview shows before/after changes to:

- Selected port mode, IPv4 address, role and administrative status. Management access settings stay unchanged.
- Two named firewall address objects for LAN and lab destination.
- A named SD-WAN zone, one to three members with gateways, ICMP health check and an SLA rule with ordered WAN preferences.
- A static route through the SD-WAN zone and an outbound LAN-to-zone firewall policy, with optional source NAT and traffic logging.

Names use `netem-<inventory-id-prefix>`; numeric IDs are selected from free valid ranges (members 1–512, SD-WAN service rules 1–4000). Existing configuration is not adopted based on names alone. NetEm records fields it has verified and refuses subsequent overwrites if they were changed externally. Repeating an unchanged verified setup produces no changes. Adding/removing WAN links updates NetEm-owned SD-WAN configuration; a port removed from the lab retains its IP configuration.

Take a configuration backup in FortiGate and retain console access. Confirm the preview and click **Apply reviewed configuration**. A preview expires after 15 minutes, is bound to appliance revision and credentials, and can be approved once. NetEm checks that the lab and simulator workload are idle, then rediscovers configuration and refuses stale previews before writing. Lab starts, topology edits, database maintenance and update commands are blocked while the operation is running. Stop actions remain available.

FortiOS exposes SD-WAN as a singleton CMDB resource. NetEm merges the four child arrays with discovered unrelated objects, preserving their fields and order. Existing firewall and SD-WAN rules retain precedence; an earlier matching rule can override the appended lab rules. Review existing rules on the appliance. Do not edit device configuration concurrently with provisioning: FortiOS CMDB has no multi-resource transaction covering this sequence.

Writes run in a background operation with bounded individual API requests. The browser polls status; it does not retry writes. A durable journal marks each step before sending and after acknowledgement. Read-back verifies intended fields, SD-WAN object preservation and rule order. This verifies configuration, **not forwarding**. In Appliances, set the simulator data interface and the configured FortiGate LAN gateway, then use **Verify & select** to verify the controlled target through the appliance. The wizard does not switch the running traffic path.

## Partial failure and recovery

Changes are sequential, not atomic. Authorization failures, dependency errors, timeouts, process restarts and read-back mismatches can leave partial configuration. The status reports **needs review** or **interrupted**, preserving the last acknowledged/sending step. It never reports success merely because a request was accepted. Download the last recovery JSON from the wizard; it contains the reviewed before/after fields and attempted configuration, without raw device secrets. It is a recovery aid, not a full FortiGate backup and not an automatic rollback script.

Inspect live configuration through the management port or console before continuing. Restore the reviewed fields/delete newly created objects as appropriate, or restore the FortiGate backup, then discover again. Partially created names/IDs or external changes may need manual reconciliation before a fresh preview. There is no automatic rollback of uncertain writes. Job/ownership/recovery records persist in the inventory database; full discovery replies and unapplied plans stay only in memory. After a NetEm restart, discover and preview again.

## Routes and references

Admin-only endpoints: `GET /integrations/fortinet`, `GET /api/v1/integrations/fortinet`, `GET /integrations/fortinet/<appliance-id>/recovery.json`. Discovery, preview and apply use separate POST routes under `/integrations/fortinet/` with existing session/role enforcement and explicit CSRF form tokens.

Primary references:

- [Fortinet: REST API administrator](https://docs.fortinet.com/document/fortigate/7.6.2/administration-guide/399023/rest-api-administrator)
- [Fortinet: Using APIs](https://docs.fortinet.com/document/fortigate/7.6.6/administration-guide/940602/using-apis)
- [Fortinet: SD-WAN CLI reference](https://docs.fortinet.com/document/fortigate/7.6.2/cli-reference/838040159/config-system-sdwan)
- [Fortinet official Go SDK CMDB endpoints, reviewed commit da485084](https://github.com/fortinetdev/forti-sdk-go/blob/da485084657d1f6006651aeb760f3a006bcb6d7e/fortios/sdkcore/sdkfos.go)
- [Fortinet official Terraform SD-WAN schema, reviewed commit 1850f22a](https://github.com/fortinetdev/terraform-provider-fortios/blob/1850f22a5d5ee9325e3dc9807ffd94d9b8c10e37/fortios/resource_system_sdwan.go)
