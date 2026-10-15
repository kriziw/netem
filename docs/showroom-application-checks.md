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
