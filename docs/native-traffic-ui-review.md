# Traffic control integration review

The previous implementation exposed a second navigation system and rendered agent HTML through NetEm. Its configuration flows competed with the existing native test, inventory and workload controls. Manual actions could invalidate a test-owned run, and saved unverified interfaces were absent from the native path inventory.

## Navigation and ownership

| Surface | Intended operator journey | Integration decision |
| --- | --- | --- |
| Command Center | See WAN conditions, execution and experience; start or manage a test | Run/manage test is primary; path settings are contextual; no separate simulator app |
| Tests / site programs | Choose an outcome; execute it and observe phases | Automatic workload requirements and owned cleanup; manual traffic behind Advanced; no overrides during execution |
| Scenarios / custom plans | Define explicit traffic and impairments | Existing managed runner remains authoritative; no competing manual control |
| Showroom | Present current site, execution and outcome to visitors | Read-only; controller and agent remain in sync through supervised execution |
| Platform Health | Distinguish service/connectivity faults from intended impairments | Native links to test execution and relevant path/agent settings |
| Appliances | Maintain vendor inventory/readiness and choose appliance | Retain native inventory and preflight; guided saved appliance interfaces also appear as unverified paths |
| Traffic paths | Select workload route and inspect independent checks | Native setup and verified selection; retained drafts; one inventory shared with preflight |
| Settings | Configure reusable environment and targets | Three native sections with shared navigation and consistent task feedback |
| Analytics | Inspect measured outcomes and network conditions | Measurement explanations updated to current worst-critical-app scoring where shown |
| Sessions / reports | Compare completed runs and explain outcomes | Existing outcome/history contracts preserved |
| Integrations | Connect the executing agent on management network | Connection configuration remains native and separate from workload data routing |
| Branding / updates / topology / access profiles | Configure platform presentation and operating environment | Existing native settings flows retained; agent maintenance added alongside them |

## Error and interaction states

Configuration is idle-only and disabled while a test/workload/task runs. The server also enforces ownership. Invalid submissions retain entered fields, including after asynchronous form submission. Agent task results poll without replacing edited forms; completed tasks refresh clean forms. Agent outages produce an explicit connection/retry state. Saved interfaces need not pass a path test; their warning remains visible until verified. Agent and target updates use their management connections and preserve the existing validated privileged queue.

## Validation

Tests cover the native NetEm shell, JSON-only contract, retired HTML relay, local monitor restrictions, authenticated commands, configuration validation, saved drafts, active program ownership and existing executor leases. Browser inspection uses a localhost mock agent; Linux CI additionally exercises privileged network policies and installed packaging. Physical appliance traversal still requires the lab's WAN counters.
