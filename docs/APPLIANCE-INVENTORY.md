# Appliance inventory and platform monitoring

**Platform Health → Traffic steering** also configures direct NetEm/ISP and clean
router paths. Every path can carry the full HTTP/UDP simulated workload. Workloads
stay pinned while source-bound health probes independently check other paths.
See the [simulator traffic-path guide](https://github.com/kriziw/netem-traffic-simulator/blob/main/docs/TRAFFIC-PATHS.md)
for Proxmox NIC placement and router return routes. Direct modes keep appliance
monitoring active but do not require an unused appliance's license. Their route
readiness still cannot be overridden.

NetEm owns a persistent inventory in `runtime/inventory.db`. Add devices in
**Appliances** or import the Traffic Simulator's saved appliances. Virtual and
physical devices share the inventory model. FortiGate is the first monitoring
adapter; other vendors support inventory/routing and report unsupported monitoring.

## FortiGate on Proxmox

For a FortiOS 7.6.7 VM with FortiFlex:

1. Add the name, Fortinet vendor, VM type, model, firmware and optional Proxmox VM ID.
2. Configure the management HTTPS origin separately from the LAN traffic gateway.
3. Create a read-only FortiOS API administrator with the NetEm management address
   in its trusted hosts. Grant monitor access to system, performance and license
   status for the intended VDOM.
4. Enter its token. Use CA verification, or a verified SHA-256 fingerprint for a
   self-signed certificate.
5. Keep `vm` as a required entitlement. Add any security-service license keys
   required by your tests after inspecting the returned entitlements.
6. Enter the simulator data interface, appliance LAN gateway and controlled target
   IP in `198.18.0.0/15`. **Verify & select** validates routing through the simulator.

The read-only FortiOS monitor endpoints are `system/status`,
`system/performance/status` and `license/status` under `/api/v2/monitor/`.
Permissions, availability and fields vary by firmware. Missing/unfamiliar data
is unknown, never healthy. This observes the VM-reported FortiFlex entitlement;
it does not renew licenses, trigger FortiGuard updates, allocate points or query
the FortiFlex cloud portal. Validate the first observation against your actual VM.

Sources: [Fortinet monitor selectors](https://github.com/fortinet-ansible-dev/ansible-galaxy-fortios-collection/blob/main/plugins/modules/fortios_monitor_fact.py)
and [Zabbix's FortiGate HTTP license fields](https://github.com/zabbix/zabbix/blob/master/templates/net/fortinet/fortigate_http/template_net_fortigate_http.yaml).

## Observations and alerts

Normal application startup monitors independently of the browser:

- Vendor checks every 15 minutes, plus **Check now** and fresh test preflight.
- Simulator and independent target management/data-path checks every 30 seconds.
- Vendor observations become stale after 30 minutes; platform observations after
  90 seconds. Retained observations are not presented as current health.
- License expiry warning bands are 30, 14 and 7 days when the API supplies a date.
  FortiFlex without an expiry date is assessed from its reported status; no date
  is invented.
- CPU/memory at 85% produces a warning; 95% or reported conserve mode is critical.
  Missing measurements remain unknown.

Availability describes management API reachability, not power state or forwarding.
Denied API access means the appliance answered but access failed. Explicitly
expired/invalid required entitlements and critical health block starts. Optional
subscriptions and unknown API status produce warnings.

An explicit override needs a reason, applies to one start request (a whole site
plan when starting a plan), and is recorded with the active session's evidence.
It never bypasses route verification or permits switching during runs. Monitoring
does not stop existing tests. Intended WAN faults remain separate from management
health and licensing.

Persistent alerts emit events on detection/recovery and can be acknowledged
without clearing the fault or granting an override. Observations retain 90 days
independently of telemetry pruning. Archiving removes the credential and preserves
evidence. Active/pending devices must be switched away from before archiving.
Missing health/license data never produces a false recovery event; previous alerts
remain until an observation confirms recovery. Their last-seen timestamps show the
age of that confirmed condition; current readiness still treats missing data as
unknown and warns.

## Selection and compatibility

NetEm registers a constrained route through the authenticated simulator endpoint
`PUT /api/v1/network/appliances/<id>`, then uses existing
`POST /api/v1/network/select`. Registration is idempotent and does not change
routing. Selection uses the privileged queue, checks the controlled target and
restores the previous managed route on failure. Management routing is protected.

Successful selections use the simulator's reboot restoration and route watchdog.
NetEm reconciles with the simulator's actual saved route. Retained selections are
unverified when current state is unavailable. Pending route jobs block tests.
NetEm serializes its starts and inventory changes; the simulator independently
checks workload/administration state and the directly connected data LAN.

Update the simulator to a release with the inventory API before selecting devices
from NetEm. Existing workloads remain compatible with older releases; selection
explains when an update is required. Independent target-manager status uses
`GET /api/v1/target/health`; target credentials stay on the simulator.

## Credentials and deployment

Tokens live in private `runtime/appliance-secrets/<id>` files (0600 on Linux),
outside SQLite, browser responses, exports and raw API payloads. Changing API
origin/vendor removes the old token unless a new one is provided. Certificate pins
are checked before sending credentials. Redirects/proxies are not followed;
responses and timeouts are bounded.

The existing management-network access model remains. Form tokens protect
mutations and readiness overrides. Remote access needs the existing authenticated
management boundary. A custom WSGI deployment must start the background workers
in its single control process; there is no multi-process leader election.

Read-only APIs: `GET /api/v1/appliances` and
`GET /api/v1/appliances/<id>/history`.

For physical devices, management address and traffic gateway remain separate.
Additional ISP-router configuration requires a topology decision and is not
inferred from inventory.

## Remove an appliance from inventory

Administrators can choose **Remove** in the device list, or **Inspect → Remove from inventory**. The confirmation page identifies the device, describes the effect and requires its exact name before the removal POST is accepted. Opening/cancelling the page does not remove anything. Other roles cannot view the confirmation page or remove devices, and CSRF protection applies.

Removal archives the local inventory record, stops its monitoring and deletes the saved API token from NetEm. Historical observations, test/session reports and FortiGate provisioning/recovery records remain. It does not reset the appliance, undo its configuration, revoke the token on FortiGate or delete its saved definition in the Traffic Simulator. Explicit simulator import or manually adding the device can create a new inventory entry; enter its API token again.

A selected appliance cannot be removed, even when its last selection is unverified. First verify/select another appliance or a direct traffic path through **Platform Health**. A pending switch involving the device, active session/test/plan/capture, or in-progress FortiGate operation also blocks removal. The server rechecks these conditions when the confirmed request arrives. Removing an unused appliance is a local operation and does not require contacting an unreachable device or simulator.
