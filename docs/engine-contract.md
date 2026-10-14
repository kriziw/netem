# Engine evidence and simulator compatibility

Upgrade the controlled target, simulator #70 and platform together during a
maintenance window. Resume acceptance only after all components are updated.
The platform sends per-site application targets and criticality only when the
simulator advertises workload-utility-v2. Required business applications receive
an executable staff cohort while device populations are preserved.

Utility, target compliance and evidence completeness are independent outputs.
Configured workload shares and criticality factors determine utility. Confirmed
critical violations fail business acceptance even when another application lacks
samples. Observation-only alerts cannot establish a failure or a pass. Truncated,
clock-invalid or mixed-policy evidence cannot establish acceptance.

Provisional utility includes weighted coverage and a possible full-workload score
range; incomplete evidence never passes. The API carries at-risk pending work,
critical alert severity, component explanations and the one-release legacy score.
See score-sensitivity.md and criticality-sensitivity.csv for deterministic scale
comparisons. Existing 70/80/90 floors are policy choices, not empirical calibration.

Phase reports use an explicit run and half-open start/end interval, freeze before
cleanup and exclude pauses with disjoint active segments. Query failures remain
unavailable; rolling scores are never substituted. Reports separate execution
completion from experience acceptance. Group latency is named worst-application
P95, not pooled P95. Assertions reject missing, stale, future, truncated and
wrong-run evidence. Older simulators lacking completeness cannot silently pass.

Steering state is scoped to run/path and, when supplied by the shaping update,
impairment generation. Direct-path attribution uses its declared WAN. Appliance
attribution uses observed egress. Packet source transitions carry observation
bounds separately from completion-window assessments; neither reports the
appliance's internal decision time. Diagnostic causes remain hypotheses.

Shaping, queue accounting and calibration are a separate PR and documented in
shaping-calibration-contract.md when installed. The scoring PR works without it;
impairment-generation timing requires that companion update.

No disposable lab is available. Actual appliance steering, deployed kernels and
offloads, queue sojourn, and generator/target/host capacity ceilings remain
unverified. Isolated Linux CI is synthetic evidence, not deployment certification.
