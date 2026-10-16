# Stability review through NetEm 1.4.2

This review follows three reported symptoms: HTTP 400 on a failed update submission, HTTP 502 despite a subsequent successful update, and a stopped Traffic Simulator alternating with Disconnected. It examines the last ten merged NetEm PRs and the simulator status contract. No live host logs or packet capture were available; specific proxy behavior is inferred from the restart ordering and the reported result, not directly observed.

## Last ten merged PRs

| PR | Change | Assessment relevant to these failures |
| --- | --- | --- |
| #154 | Release 1.4.2 | Version/changelog metadata only |
| #153 | UI responsiveness and presentation caching | Introduced pending/expired snapshots with `connected=false`; the dashboard and Tests interpreted that as a failed connection. Diagnosis expiry could also replace an independently valid simulator connection. |
| #152 | Release 1.4.1 | Version/changelog metadata only |
| #151 | CLI database ownership | Repairs root provisioning ownership and drops to the runtime identity; unrelated to updater acknowledgement or simulator polling |
| #150 | Release 1.4.0 | Version/changelog metadata only |
| #149 | Users/RBAC/OIDC, phase controls, unsaved tests | CSRF enforcement exposed the NetEm install form's missing token in JSON-delivered update cards. The HTML-only token injector never processes those fragments. Also produced partial pending-phase DEM dictionaries that templates could attempt to format as numbers. |
| #147 | Release 1.3.7 | Version/changelog metadata only |
| #146 | Showcase narration stabilization | Scoped to presentation of overdue evidence; no update/connection request changes found |
| #144 | Release 1.3.6 | Version/changelog metadata only |
| #145 | Capability documentation | Documentation/presentation text; no updater or simulator request implementation changes |

The behavioral changes had isolated regression coverage, but that coverage missed the authenticated **JSON card refresh → install** flow and the distinction between connection health and cached evidence freshness. Passing shaping/engine acceptance does not validate those UI interactions. The added tests exercise these combined paths.

## Reproduced causes and fixes

### HTTP 400 before installation

`_update_cards.html` omitted `integration_csrf` from the NetEm install form. A complete HTML page received an injected token in `access_control.py`, but `/updates/components` returns JSON containing HTML. Once the card was replaced, its form had no token. The RBAC guard rejected it before Git ran.

The shared partial now emits the token explicitly. The browser also recovers it from the existing Check for updates form when consuming an older card. The server still rejects missing/invalid tokens and unauthorized roles. An authenticated regression test fetches the actual fragment, extracts its token and submits the real route; it checks that missing-token and Viewer submissions never invoke Git.

### HTTP 502 while installation succeeds

On successful Git merge the handler scheduled process exit after 1.5 seconds, then continued through Git status, simulator controller/settings reads (six-second socket timeout) and full-page rendering. The process could exit before acknowledgement reached the browser. A reverse proxy can report 502 in that window; the user's observed restart and successful installation are consistent with this ordering.

The route now returns an immediate JSON `202` acknowledgement after merge, before simulator reads, extra Git inspection or full-page rendering. Old browser clients receive a small HTML restart marker instead. JSON refusals explicitly state `update_state=rejected`; they do not restart.

The browser no longer treats an unacknowledged HTTP error as proof that nothing restarted. After an opaque 400/502, lost response or interrupted response body, it checks the running service without resubmitting the POST. Two consecutive replies must name the same new process and the expected version or a newer semantic version. An unclear outcome ends as **Update not confirmed**, with instructions to check before retrying; it never claims that nothing restarted. Explicit refusals retain **Update not installed**.

`/updates/status` now reports the version captured when the process started. Previously it read `version.txt` from disk, which Git can change while the old process is still running. Login/setup/permission walls now offer sign-in/setup instead of endless reconnect polling. The status timeout covers the response body, and general page-version refresh pauses while the update screen is active.

### STOPPED / Disconnected oscillation

There were three distinct problems:

1. Cold/expired cache results had no status and `connected=false`, which the UI painted as Disconnected even if a successful refresh was pending.
2. An expired diagnosis could supply its empty fallback to the Corporate traffic widget, replacing an independently available simulator status.
3. NetEm's status socket timeout was two seconds, while Traffic Simulator 1.3.0's `Controller.query_dem()` can wait up to five seconds for an aggregation result. `Controller.status()` reaches that path for idle status and when its live snapshot is unavailable. This timeout mismatch can reject a valid slow response. It predates the new cache but compounds its expiry behavior.

The corrected behavior is:

| Situation | Display/data behavior |
| --- | --- |
| No result yet, refresh pending, or connection status too old | CHECKING (neutral), not an asserted disconnect; status API returns 202 |
| Recent successful STOPPED status while refreshing | Keep the last confirmed STOPPED label for up to 15 seconds, with a last-confirmed/refreshing explanation |
| Measurements older than three seconds | Remove score, availability, response times and current user count; never preserve stale live evidence as a confirmed result |
| Actual failed status request | DISCONNECTED and the concrete error remain visible; API returns 503 |
| Expired diagnosis with a separately valid simulator connection | Diagnosis findings become unavailable; the connection state comes from the independent status read |

The background status timeout is seven seconds to allow the simulator's existing five-second aggregation wait plus connection/response overhead. This remains off the UI request path. Seven seconds is a socket-operation timeout, not a total DNS/TLS/request deadline. Expiry and bounded background concurrency still protect the UI if a read stalls.

Pending DEM dictionaries now include nullable fields expected by both page templates. No score is exposed during phase handover or evidence refresh. The tests render these states and compile the resulting inline JavaScript, covering failures a Jinja-only syntax check would miss.

## Validation and limits

Added coverage includes authenticated fragments and CSRF, fast install acknowledgement, explicit refusals, 502 followed by successful restart, ambiguous 400, interrupted bodies, version mismatch, alternating processes, authentication walls, pending/stale/failed connections, independent diagnosis freshness, and page rendering with pending measurements. Existing role, phase, session, unsaved retention and network-engine checks remain part of validation.

These fixes target reproduced software defects. They do not establish that every intermittent connection error is cosmetic: real TLS, routing, CPU, database or simulator failures still need their concrete error/log evidence. They also do not change the already-installed v1.4.0 update screen retroactively; after deploying the fix, reload the browser to load the corrected client.

Sources: NetEm merge commits referenced above; [Traffic Simulator 1.3.0 status/aggregation implementation](https://github.com/kriziw/netem-traffic-simulator/blob/11ea363849b96972589263906d9f5261db0679eb/trafficgen/engine.py); [MDN fetch response/error semantics](https://developer.mozilla.org/en-US/docs/Web/API/Window/fetch); [Flask JavaScript/JSON guidance](https://flask.palletsprojects.com/en/stable/patterns/javascript/).
