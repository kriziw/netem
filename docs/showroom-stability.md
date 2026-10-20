# Customer showcase

The live showcase answers exactly three questions:

1. **What's happening:** current test and phase, WAN health, measured download/upload
   and recorded traffic graphs, explicitly labelled injected delay/jitter/loss or
   outages, and where application traffic goes.
2. **Impact on score:** one target-aligned score dial, request completion and app
   response time, plus compact scores for voice/video, business apps and transfers.
3. **Impact on users:** three plain-language implications for calls/meetings,
   business tasks and files/backups. Confirmed application violations explain
   possible consequences (calls breaking up, retrying tasks, slower transfers).
   An injected fault never supplies evidence of user impact. Complete healthy
   application checks can earn green status; missing evidence, pauses, outstanding
   work and disconnected measurements cannot.

Application-level transactions, confidence diagnostics and expandable measurement
lists stay off the customer screen. Operator data, reports and scoring rules are
unchanged. The banner retains client context and shows the current simulator user count when available.
Configured branding and colours remain in use.

## Countdown and baseline

After the configured announcement countdown, show the live three-part dashboard
immediately, including while the simulator starts and initial application evidence
is incomplete. Label the normal starting interval **Measuring baseline** and fill
real WAN/application measurements as they arrive. There is no second readiness
screen covering the baseline. Preserve the scenario's actual baseline duration
and phase ordering; the view does not advance or invent a baseline measurement.
Withhold cached scores from an earlier run while startup awaits acknowledgement.
Existing labelled previous-phase continuity remains neutral during later phases.

## Screen layout

Use three columns on laptops and desktops; rearrange the same sections on narrow
portrait screens, with a compact landscape arrangement for short screens. Keep
one to three WANs visible together. Extra WANs beyond the supported three still
use the existing rotating pages. Reduce padding, repeated metadata and graph
height on smaller viewports. No overflow clipping or ellipsis hides user effects.

Actual Chromium rendering was checked for three WANs and confirmed-impact and
pending-evidence states at 1920x1080, 1440x900, 1366x768, 1280x720, 1280x600,
1024x768, 960x540, 800x600, 800x900 and 390x844 CSS pixels. Check document and
panel scroll dimensions as well as screenshots: fitting the outer page alone
can conceal overlapping panel content. Extremely small windows, arbitrary long
custom labels or unusual branding fonts may require further layout adaptation.

## Recorded telemetry and application responsiveness

Snapshots include up to 250 allowlisted recorded rate samples per WAN within
120 seconds. Reconnection replaces browser-poll history with these recordings.
Explicit invalid samples and sampler intervals over three seconds still break
the graph; current tiles require fresh telemetry even if old chart data exists.
Latest-sample and chart-history SQLite reads have a 50 ms lock bound.

Bridge inspection/repair runs in a dedicated background worker, keeping command
and bridge-lock waits out of the one-second rate sampler. Explicit pre-test
checks remain synchronous. Missing-score details in the operator data distinguish
invalid evidence and missing fields from insufficient transaction counts.

Update the Traffic Simulator companion as well: live status must read published
measurements without queueing aggregation work when the publisher is delayed.
Companion fixes also isolate report aggregation from publication and distinguish
recoverable history-sampler errors from lost application transactions.
