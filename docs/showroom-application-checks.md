# Application evidence for small-site demonstrations

Update the simulator first, then NetEm. The simulator advertises the
`continuous-application-checks-v1` capability. NetEm enables checks automatically
for user-mode starts with 1–25 users, including site plans and ordinary test
workloads. Employee counts and persona allocations stay unchanged. Explicit
`application_checks: false` in a custom start keeps checks off. Older simulators
receive the original payload without the unsupported field. Rate/calibration
starts do not acquire checks.

Checks run through the selected workload source and the appliance, alongside
employee tasks. They use bounded synthetic transactions; HTTP objects are limited
to 32 KiB. They contribute to application experience, not capacity validation.
Optional bulk-throughput targets still require adequately sized employee transfers.
The showroom identifies the user population plus continuous monitoring, the number
of applications measured and how many transactions came from application checks.
Detailed byte counts and check-worker errors remain available in status.

At startup, an active demonstration can show **Establishing application baseline**
for at most 20 seconds, ending earlier once evidence is complete. Confirmed failures
always take precedence. This is presentation only: tests keep their existing phase
timing and evidence boundaries; previous-phase samples are never reused to fill a
current-phase gap. Preparation is not repeated at every phase transition.

Once the main test screen has appeared, that display keeps preparation dismissed
for the rest of that test, including evidence gaps, phase changes and pause/resume.
A confirmed failure also dismisses preparation immediately and permanently for
that test. The next test can establish its own initial baseline. The public test
start timestamp identifies consecutive tests even when no idle snapshot arrives
between them; no internal run identifiers are exposed.

At a phase transition, the showroom can retain the last measured outcomes from
the immediately preceding phase until new-phase measurements arrive for each metric or group.
The cards and heading say **Previous phase**, name that phase, and use neutral
status colours. Each metric or group updates independently; new measurements,
including zero scores, replace its retained value immediately. A confirmed current
failure takes precedence over all retained values. No retained values cross test boundaries
or an intervening phase with no measurements. This browser presentation cache
does not change simulator windows, current-phase verdicts, assertions or reports.

Observed scores keep neutral verdicts until acceptance is established. Confidence
explanations and individual sample counts remain in expandable application
measurements; the main cards lead with measured performance or observed impact.
Unused traffic groups, unmapped route rows and unavailable optional RTT readings
do not fill the customer screen with placeholders. WAN cards remain visible, and
the operator's mapping and evidence diagnostics remain unchanged. Route labels only
appear when there is evidence identifying a WAN.

The monitoring schedule supplies regular samples in a healthy small-site lab.
Outages, missing target metadata, unknown WAN mapping and host overload still
require operator action; the display does not invent measurements or green results.

## Between-test demonstration summary

The session pause screen separates execution from resilience: tests demonstrated,
observed application impact, measured recovery and slowest application response.
A completed degradation demonstration may show application impact without being
presented as a failed demonstration. Execution errors and stopped tests remain
explicit and are not counted as completed demonstrations.

The expandable **Resilience requirements and measurements** section retains
met/not-met/unconfirmed counts, every test's requirement status, recorded unmet
requirements and conclusions. The lowest measured score, including a preliminary
zero with its evidence qualifier, stays in these details rather than dominating
the main screen. No resilience verdicts or scores are changed.

Recovery is confirmed only from a reached Recovery phase with a recorded passing
phase verdict. New summaries retain the existing phase-verdict calculation;
historical records without this verdict cannot establish recovery from a high
score alone. Below-target recovery and incomplete evidence remain explicit.

## Explaining missed application targets

Target-violation findings name each application, its measured value and the exact
configured threshold, including units: for example, `Voice: 3% packet loss
(target ≤ 1%)` or `ERP: 720 ms P95 (target ≤ 400 ms)`. Separate applications in the
same traffic group retain separate findings. Hard deadlines show the late count,
zero-late requirement and deadline; missing measurements or thresholds are stated
explicitly rather than replaced with invented values.

The platform recovers each application's full-window egress counts instead of
using the generic target finding's unknown path. A declared direct NetEm path or
one fully mapped WAN can support a **Likely contributor** when independent current
path evidence matches the violated requirement. Loss/success misses can match
injected loss of a meaningful magnitude; timing misses can match added delay or
variation accounting for at least a quarter of the measured timing. Transfer
misses can match same-direction saturation or sustained queue pressure. An
isolated queue-management drop does not establish a contributor.

These are causal hypotheses, not independently proven root causes. Mixed or
partly unknown paths, a path inferred only from aggregate traffic, unverified
shaping, invalid measurement windows and windows crossing a known impairment
change do not receive a likely-cause claim. The measured target miss remains
visible when its cause cannot be established. Packet delivery loss retains the
simulator's bounded-collection semantics; it can include packets arriving too
late, so it is not proof of physical packet loss.
