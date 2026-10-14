# Shaping and calibration mechanics

Profile application is serialized and validates finite values and supported
jitter/reorder combinations before mutation. A partial direction failure attempts
to restore both previous directions. Failed rollback reports unknown actual state.
Verified identical profiles preserve queues and counters. Verified transitions
with the same hierarchy change qdiscs in place; topology changes rebuild them.
Removed impairments are explicitly cleared. Desired and applied state stay distinct.

Queue telemetry separates netem delay, TBF and FQ-CoDel, including reported
memory, early-congestion, overlimit and overmemory counters. Additional queue delay
uses only the rate queue backlog. Allocation memory is not a guaranteed wire-delay
budget. Injected loss inferred from counter differences remains an estimate.
Counter generations prevent rates spanning queue rebuilds.

Calibration requires a matching direct NetEm WAN. It measures unloaded and loaded
probes on the same target, during the active measurement interval. Directional
monotonic offered/admitted/dispatched/delivered counters establish actual demand.
Missing host evidence or insufficient dispatched demand cannot certify capacity.
Sparse DEM uses the exact run and interval; delay causes remain hypotheses.
Simulator #70 supplies the required demand contract; older versions cannot pass
capacity acceptance. Platform scoring #117 is independently reviewable/revertible.

Kernel CI uses disposable namespaces and exercises real tc transitions, no-op
preservation and impairment removal. Cross-repository CI merges pinned companion
revisions in a temporary checkout and measures real packets, directional rate
arrivals, resizing and observer-on/off cases. Results describe the CI runner only.
No disposable lab is available: appliance steering, supported deployed offload
variants, queue sojourn and hardware capacity ceilings remain unverified.

The user selected a coordinated maintenance-window upgrade. Do not use mixed
platform/simulator versions for business acceptance. The compatibility test
records the old platform's inability to enforce the new critical-failure result.


## Queue resource guards

Each shaped direction gives fq_codel a 64 MiB kernel-memory guard and a 65,536-packet guard. The intentional-delay queue has at least 65,536 packet slots and grows for larger configured bandwidth-delay products. These are resource ceilings, not preallocated buffers or queue-delay promises. The WAN rate remains enforced by TBF; CoDel retains its 5 ms target and 100 ms interval and may deliberately drop during persistent queueing.

The previous memory guard reused 25 ms of payload bytes as kernel allocation bytes (65,536 bytes at 18 Mbit/s; 131,250 at 42 Mbit/s). This could constrain bursts independently of average link utilization. The namespace regression compares those old memory guards with the generous guards at both rates and at 160/1,500-byte packet sizes.

Telemetry now records fair-queue memory-limit drops, packet-limit drops excluding memory-limit drops, and remaining drops (AQM/other). Linux counts memory-limit drops within its overlimit counter; they are not added twice. Missing kernel counters remain unknown. Remaining drops are not proof of a particular CoDel decision. Injected loss remains a separate counter-difference estimate.
