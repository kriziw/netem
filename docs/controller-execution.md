# Test controls and execution ownership

NetEm is the controller. Tests and site programs automatically start the traffic they require, await active users, supervise execution and clean up only their owned run. The simulator's local browser is an execution/experience monitor.

Native configuration lives in **Settings → Traffic paths & network**, **Application experience targets**, and **Traffic agent & target**. These pages use NetEm's shell, branding, cards, CSRF protection and task feedback. The agent returns JSON state and accepts authenticated commands; it never renders operator HTML for NetEm. Both components must be updated together. Old /simulator bookmarks redirect to native NetEm destinations; POST and asset relaying are removed.

Manual workloads are an advanced option on Tests for standalone calibration. Active tests block manual start/adjust/stop and configuration changes, so their requirements stay authoritative. Paths retain a common verified inventory used by test preflight. Saved unverified interfaces remain visible and can be retried without being selectable.

| Control | Controller action | Executor behavior |
| --- | --- | --- |
| Tests generic Run, custom Run, Scenarios Run | Common scenario runner | When idle, start the active site's full workload (Office/50 users without a site), acknowledge run ID and wait for active users before impairment phases |
| Site Run test / Run all | Site plan with explicit workload steps | Execute the defined start/adjust/stop steps; wait for active users before continuing past start |
| Tests → Advanced manual traffic | Authenticated workload start API and path preflight | Execute the selected workload on its source-bound selected traffic path |
| Adjust | Workload adjustment API | Change the existing executing workload; never start another workload |
| Pause phase / Resume | Hold/resume controller phase clock | Continue traffic and heartbeats under the current impairment |
| Stop & restore / normal test completion | Stop owned run by matching ID, restore original WAN/MTU | Stop only traffic this test started; borrowed pre-existing traffic remains running |
| WAN quality/bandwidth/fault controls | Apply WAN impairment | Affect executing traffic; do not independently start a workload |
| Network setup / scoring / maintenance controls | Native forms → JSON configuration commands | Validate and enqueue changes; these are configuration actions, not workload starts |

NetEm checks the exact executing run every five seconds, including while paused
or waiting on assertions. Lost/mismatched execution fails the test and restores
the original WAN rather than continuing an idle phase timeline. Test-owned starts
carry a 30-second controller lease; the agent stops that exact run if NetEm's
heartbeat disappears. A stale stop cannot terminate a replacement run. Borrowed
workloads do not acquire an automatic-stop lease.

Direct NetEm tests stay on their configured WAN and bypass unrelated appliance
license checks. Clean baseline paths permit full workloads but refuse WAN scenarios;
site/multi-WAN steering plans still require the appliance path. Explicit custom
workload steps retain their configured behavior rather than getting a duplicate
automatic workload start. Startup and WAN-restoration failures are reported as failures.

Saved but unverified interfaces remain visible in the controller's Traffic paths
screen with a warning and Verify/retry action. They cannot be selected for a workload
until verification succeeds.
