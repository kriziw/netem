# Small sites, timing and downloadable test records

Upgrade the platform and simulator together. Application scores now include
valid small samples, labelled **limited sample**, while statistical acceptance
and critical-app rules remain unchanged. Missing applications remain unknown.
Measured workload coverage and sample-qualified evidence coverage are separate.
Application measurements expand to show scores, counts, timing and freshness.
The selected live window remains 15 seconds by default and respects phase
boundaries; no earlier phase fills gaps in the current phase. Full-phase reports
also show small-sample measurements without issuing a target pass.

**Application completion · P95** means request start to the complete response,
including upload, target processing, queueing and response transfer. It is not
network latency. **Target agent round-trip delay (RTD/RTT)** is a separate UDP
echo measurement from the simulator to the configured controlled target agent,
bound to the workload source. The simulator sends at most one small check per
second per workload. Checks never count as user transactions. An unanswered
check is missing RTD evidence, not zero delay. UDP classification can differ
from application traffic on an appliance.

Actual one-way network latency is unavailable with the current echo protocol.
It requires endpoint timestamp exchange and verified clock synchronization;
the platform never halves RTD to invent a latency measurement. WAN delay remains
the configured NetEm impairment. Timing probes and application observations
cover the same selected live interval; phase reports retain full-phase timing.

## Records

Every new scenario test receives a unique ID and durable record under
`runtime/test-records/<id>/`. Download its ZIP from **Tests → Test records**,
the last-test summary or the corresponding test in a session report. Downloads
use the operator surface; the separate public showroom does not expose files.
Existing tests completed before this feature cannot gain raw evidence
retroactively. Existing operator/proxy access restrictions apply.

The ZIP contains:

- A bundle manifest identifying partial/unfinished records and SHA-256 hashes of
  its evidence files. Record completeness is separate from test acceptance.
- A readable overview, execution/experience summary and original configuration.
- A journal of events and live measurements, including per-application evidence,
  workload settings, target identity/version, shaping/queue observations and steering.
- Phase boundaries, pauses, full-phase measurements, assertions and reactions.
- Paged simulator transactions, lifecycle events, target RTD checks and scoring
  contracts, plus an explicit export-completeness result.
- Full persisted WAN, host and active-probe samples within the test interval.

Credential fields and URL credentials/query strings are redacted before storage.
Records contain network addresses and site configuration for troubleshooting;
they are not public showroom exports. Wall-clock timestamps use Unix seconds
from each host, so cross-host correlation requires synchronized clocks.

Configuration and the journal are durable during a test, allowing an unfinished
record to be downloaded after interruption/restart. An unfinished test has no
final verdict and cannot be mistaken for a completed result. Raw agent evidence
is collected on normal completion/stop/failure, not retrospectively after a
platform crash. A disconnected or older simulator produces an explicit partial
record rather than a fabricated complete log.

Resource bounds are explicit: 64 MiB for the journal, 100 pages of 1,000 rows per
agent run/evidence kind, a 30-second total remote collection budget, and 100,000
rows per platform telemetry table per test. Truncation/errors are recorded and
prevent `record_complete=true`. Records stay on disk across restarts; this feature
does not automatically delete historical bundles. Include this directory in
normal backup/storage management. The old last-test summary remains compatible.
