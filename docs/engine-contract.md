# Engine evidence and simulator compatibility

Deploy the simulator's `workload-utility-v2` contract before this platform update.
The platform sends per-site application deadlines, success targets and criticality
when the simulator advertises support. Required industry applications receive an
executable staff cohort, preserving configured device populations. The scenario
validator retains this policy when it compiles workload start steps.

Overall and traffic-group scores use the simulator's configured workload and
criticality contributions. Critical-application alerts are separate from the site
utility number. Showcase outcomes retain explanations and evidence validity. DEM
assertions refuse incomplete, truncated, stale, future-dated and wrong-run evidence.
Older simulators lacking completeness cannot produce a complete passing verdict.

Phase reports query an explicit run and half-open start/end interval. Failed phase
queries remain unavailable; rolling scores are never substituted. Final boundaries
are frozen before stopping/restoring the workload. Paused intervals are conservatively
incomplete. Group latency is explicitly named worst-application P95, not pooled P95.
Reports separate execution result from experience acceptance result.

Calibration requires the matching direct NetEm WAN. Appliance steering and clean
baseline paths cannot be labelled single-WAN capacity tests. Latency probes run
inside the measurement interval while load is active; sparse DEM uses that exact
interval. Missing host evidence cannot pass. Endpoint/path delays remain competing
hypotheses until independent evidence separates them.

Impairment transitions are serialized. Unsupported zero-delay jitter/reordering is
rejected before changes. Reapplying an identical profile preserves queues when its
observed qdisc configuration still matches. Partial direction failures trigger a
rollback to the previous profile; failed rollback is exposed as unknown actual
state rather than stale successful state. Runtime state retains desired/applied
status and an error. Normal changes still rebuild the hierarchy; uninterrupted
in-place topology changes require additional kernel-specific validation.

Backlog used for queue-delay estimation is the rate queue (FQ-CoDel or TBF), not a
sum with netem's delay/hierarchical backlog. Memory sizing is an allocation budget,
not a guaranteed maximum wire delay. Kernel CI exercises real qdisc application and
no-op preservation in a disposable namespace. Mixed packet sizes, kernel/offload
variants, queue sojourn time and production CPU ceilings still require lab testing.

Steering history resets when run/path identity changes. Direct-path attribution can
use its declared WAN; appliance-path attribution remains based on observed egress.
Steering reaction timing still uses completion-window observations, not packet-level
flow-switch events. Treat inferred single-carrier attribution as correlation.
