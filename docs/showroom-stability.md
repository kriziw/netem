# Showroom sampling and compact layout

The read-only display previously accumulated its two-minute traffic history only
from browser polls. A failed HTTP request inserted an unknown point permanently,
even if NetEm recorded the corresponding measurement. Snapshots now include up
to 250 allowlisted recorded rate samples per WAN within the last 120 seconds.
Reconnection replaces the browser history with those samples. Explicit invalid
samples and sampler intervals longer than three seconds still break the line;
missing measurements are never filled with zeros or inferred traffic. Current
rate tiles require fresh current telemetry even when old chart data exists.
Both current-sample and chart-history reads use a 50 ms SQLite lock bound.

Bridge inspection and repair can wait for host commands and the bridge lock.
They now run in a dedicated background worker rather than delaying the one-second
telemetry sampler. Explicit pre-test bridge checks remain synchronous.

Missing application scores now distinguish missing measurement fields and invalid
evidence from insufficient transaction counts, with reasons in the expandable
application details. Acceptance rules and scoring thresholds are unchanged.

Compact styling uses CSS viewport dimensions (including OS scaling), with main
columns retained above 1050 pixels, smaller card padding/gaps and shorter charts.
Hidden optional measurements do not reserve vertical space in compact mode.
The three-column client banner now uses two columns for its two sections. WAN
cards do not fade/rotate when all configured WANs already fit on the screen.

Update the Traffic Simulator companion as well: its status endpoint must only
read published live measurements rather than queueing aggregation work when a
publisher is delayed. Companion fixes also isolate report aggregation from live
publication and distinguish history-sampler errors from lost transactions.
