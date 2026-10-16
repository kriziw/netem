# UI responsiveness review

Reviewed NetEm v1.4.1 (`34d7f0d`) and Traffic Simulator v1.3.0 (`11ea363`). This is a source audit and controlled reproduction, not a measurement of a deployed lab's network or CPU.

## Why the message appeared

`static/actions.js` used one `busy` flag for navigation, submitted actions and automatic full-page refreshes. A page-version change triggers a background refresh. Clicking navigation during that GET displayed **An action is still completing** even though no command was running. The flag lasted until the entire response arrived, without a refresh timeout. A slow GET could also silently reject a form submission.

The background GET could be expensive:

| Request | Simulator work on the HTTP request path before this change |
| --- | --- |
| Command Center | Status (2-second socket timeout), readiness (8 seconds), appliance lookup when uncached (8 seconds) |
| Tests | Status and catalog (2 seconds each), readiness when uncached (8 seconds) |
| Diagnosis API and active showcase snapshot | An expired diagnosis cache triggered another build, including simulator status and other metadata |

These are per-operation/socket timeout settings, not a total end-to-end request deadline. Sequential work accumulates; reconnects also repeat TLS negotiation. A diagnosis worker already ran in the background, but HTTP readers could race it and each other when the short cache expired. Multiple browser/showcase screens amplified upstream reads.

The dashboard's telemetry/state poller also had no fetch timeout. One stalled fetch could prevent its next sample indefinitely. The showcase already had a five-second deadline.

Traffic Simulator's active-run status uses its published DEM snapshot and briefly copies run state under its lock. Its controller/settings endpoint performs more extensive network inventory work. The source audit does not establish that the management network itself is slow, nor that every simulator endpoint has constant latency.

## Change

- Separate background refresh cancellation from action/navigation locking. Navigation, edits and submissions can cancel the read; stale responses cannot overwrite their results. Real command submissions still prevent duplicates and wait for acknowledgement. Aborting a browser GET does not cancel any command or test.
- Serve simulator status, diagnosis and presentation metadata through shared background reads. Cold pages return pending/unavailable state immediately. Concurrent screens share an in-flight read, and the existing diagnosis worker publishes through the same cache. At most four presentation loads run concurrently, without an unbounded queue.
- Refresh live status/diagnosis after one second; discard it from presentation after three seconds. Readiness refreshes after 15 seconds and expires after 30; appliance/catalog metadata refreshes after 60 and expires after 120. `poll` data exposes age, load duration, refresh state and expiry on cached operator APIs. Expired measurements do not remain live evidence. Cached status is rechecked against the current run/phase on every read, and diagnosis caches change scope on phase/pause transitions.
- Scope caches to configuration path, integration settings and credential digest. Endpoint, key or TLS trust changes cannot reuse another source's data. Return independent copies so render-time edits cannot contaminate shared state.
- Keep fresh synchronous checks for test admission, execution, assertions and other control callers. Readiness metadata no longer delays the live diagnosis/recorder. Completed metadata reads change the page version so an initially pending catalog/path becomes visible automatically.
- Bound dashboard telemetry/state polling to five seconds, retry afterward, and abort requests when leaving the page.

Maintenance/configuration operations can still take time: native controller/settings reads have a 10-second socket timeout, repairs can wait for jobs, and test starts require actual preflight/acknowledgement. This change does not shorten those checks or treat an unconfirmed command as successful.

## Controlled reproduction

Run `NETEM_AUTH_REQUIRED=0 python tests/benchmark_ui_latency.py` from a checkout. For the baseline, run the same script from a v1.4.1 checkout. The script supplies a valid mock simulator status and injects 250 ms into each upstream call, with cold presentation/diagnosis state, one WAN and an active session. It drains its background work before exiting.

One representative run, all HTTP 200:

| Screen/API | v1.4.1 | Updated |
| --- | ---: | ---: |
| Command Center | 835 ms | 95 ms |
| Tests | 546 ms | 54 ms |
| Diagnosis API | 505 ms | 0.7 ms |
| Showcase snapshot | 253 ms | 1.4 ms |

Across those reads, upstream status requests fell from four to one (total simulator requests from eight to four). Updated cold responses show pending data until background reads complete; these timings demonstrate removal of simulator I/O from the screen response, not faster simulator execution. Rendering, local database/disk operations, browser performance, proxy behavior and real network conditions still affect deployed timings.

Regression tests hold simulator requests behind an event and require the operator pages, APIs and showcase to return before releasing it. Other tests cover concurrent readers, bounded workers, expiry, source isolation, fresh control reads, stale refresh rejection, edit preservation, duplicate submissions and polling recovery.

References: [Python HTTPConnection timeout semantics](https://docs.python.org/3/library/http.client.html#http.client.HTTPConnection), [MDN AbortController.abort](https://developer.mozilla.org/en-US/docs/Web/API/AbortController/abort).
