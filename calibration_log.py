"""What a calibration step saw, so the cause of a failed or surprising step can be read from the log.

The judgement lives in calibration.py. This module only turns the raw readings (tc counters at both ends of the
window, per-second WAN and host samples, simulator state) into a one-line summary per step and a text report that
can be downloaded and read alongside the NetEm event log.
"""

import time


def _num(value, digits=1, suffix=""):
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def qdisc_deltas(before, after):
    """Packets and drops each qdisc handled between two `tc -s` readings. Each reading is a list of dicts with kind,
    handle, root, packets, drops, overlimits and backlog_bytes. Offered packets are approximated as sent plus dropped."""
    earlier = {(item["kind"], item["handle"]): item for item in before or []}
    deltas = []
    for item in after or []:
        name = f"{item['kind']} {item['handle']}"
        prior = earlier.get((item["kind"], item["handle"]))
        if prior is None or item.get("packets") is None or prior.get("packets") is None:
            deltas.append({"qdisc": name, "note": "not read at both ends of the window"})
            continue
        sent = item["packets"] - prior["packets"]
        dropped = (item.get("drops") or 0) - (prior.get("drops") or 0)
        overlimits = (item.get("overlimits") or 0) - (prior.get("overlimits") or 0)
        if sent < 0 or dropped < 0 or overlimits < 0:
            deltas.append({"qdisc": name, "note": "counters reset during the window"})
            continue
        sent_bytes = None if item.get("bytes") is None or prior.get("bytes") is None else item["bytes"] - prior["bytes"]
        offered = sent + dropped
        deltas.append({"qdisc": name, "root": bool(item.get("root")), "sent_packets": sent,
                       "dropped_packets": dropped, "overlimits": overlimits,
                       "drop_pct": round(100.0 * dropped / offered, 3) if offered else 0.0,
                       "backlog_bytes_end": item.get("backlog_bytes"), "sent_bytes": sent_bytes})
    return deltas


def step_line(verdict):
    """One line for the event log: what the WAN carried, each qdisc's drops, the host, and why the step failed."""
    parts = [f"{verdict['target_mbps']} Mbit/s {verdict['status'].upper()}",
             f"WAN achieved {_num(verdict.get('achieved_mbps'))} of expected {_num(verdict.get('expected_mbps'))} Mbit/s",
             f"host {verdict.get('host_status')}"]
    for direction, deltas in ((verdict.get("diagnostics") or {}).get("qdisc_delta") or {}).items():
        for delta in deltas:
            if "note" in delta:
                parts.append(f"{direction} {delta['qdisc']} {delta['note']}")
            else:
                parts.append(f"{direction} {delta['qdisc']} sent {delta['sent_packets']} "
                             f"dropped {delta['dropped_packets']} ({delta['drop_pct']}%)")
    shaper = (verdict.get("diagnostics") or {}).get("shaper") or {}
    if shaper.get("qdisc_mbps") is not None:
        parts.append(f"shaper counters carried {_num(shaper['qdisc_mbps'])} Mbit/s")
    if verdict.get("reasons"):
        parts.append("reasons: " + "; ".join(verdict["reasons"]))
    if verdict.get("warnings"):
        parts.append("warnings: " + "; ".join(verdict["warnings"]))
    return " · ".join(parts)


def _host_by_second(host_rows):
    return {int(row["timestamp"]): row for row in host_rows or []}


def _timeline_lines(diagnostics):
    start = (diagnostics.get("window") or {}).get("start")
    host = _host_by_second(diagnostics.get("host_timeline"))
    lines = ["    t(s)   down Mbit/s  up Mbit/s  down drops/s  down drop%  down backlog KB  "
             "host CPU%  steal%  sampler ms  host"]
    for row in diagnostics.get("wan_timeline") or []:
        second = host.get(int(row["timestamp"])) or {}
        offset = None if start is None else row["timestamp"] - start
        backlog = row.get("down_backlog_bytes")
        lines.append(
            f"  {_num(offset, 0):>6}  {_num(row.get('down_mbps')):>11}  {_num(row.get('up_mbps')):>9}  "
            f"{_num(row.get('down_queue_drops_ps')):>12}  {_num(row.get('down_drop_pct'), 2):>10}  "
            f"{_num(None if backlog is None else backlog / 1024.0, 0):>15}  "
            f"{_num(second.get('cpu_busy_pct')):>9}  {_num(second.get('cpu_steal_pct')):>6}  "
            f"{_num(second.get('sampler_lag_ms'), 0):>10}  {second.get('status') or '—'}")
    if len(lines) == 1:
        lines.append("  (no per-second WAN samples in the window)")
    return lines


def _simulator_line(label, state):
    if not state:
        return f"  Simulator {label}: not read"
    if "error" in state:
        return f"  Simulator {label}: unavailable ({state['error']})"
    return (f"  Simulator {label}: status {state.get('status')}, users {state.get('users')}, "
            f"dropped transactions {state.get('dropped_transactions')}, "
            f"requests/s {_num(state.get('requests_per_second'))}, p95 {_num(state.get('p95_ms'), 0)} ms, "
            f"availability {_num(state.get('availability_pct'))}%")


def text_report(result):
    """A plain-text report: the capacity, then for each step its verdict, the qdisc counters, the simulator state,
    a per-second table of the WAN and host, and the tc output at the end of the window."""
    lines = ["NetEm bandwidth calibration", f"WAN: {result.get('link_id')}",
             f"WAN rate: {_num(result.get('wan_rate_mbps'), 0, ' Mbit/s')} · settle {result.get('settle_s')} s · "
             f"measure {result.get('measure_s')} s"]
    if result.get("error"):
        lines.append(f"Error: {result['error']}")
    else:
        capacity = result.get("capacity_mbps")
        lines.append(f"Capacity: {_num(capacity, 1, ' Mbit/s') if capacity is not None else 'none passed'} · "
                     f"limited by: {result.get('limited_by')}")
    lines.append(f"Mode: {result.get('mode', 'shaped')}")
    if result.get("bottleneck_title"):
        lines.append(f"Limit at the stopping step: {result['bottleneck_title']}")
    if result.get("restore_error"):
        lines.append(f"Restoring the shaping failed: {result['restore_error']}")
    for item in result.get("findings") or []:
        where = "all steps" if item.get("step") is None else f"{item['step']} Mbit/s"
        lines.append(f"Finding [{item['severity'].upper()}] {where}: {item['title']}")
        lines.append(f"    evidence: {item['evidence']}")
        lines.append(f"    check next: {item['action']}")
    for verdict in result.get("steps") or []:
        diagnostics = verdict.get("diagnostics") or {}
        lines += ["", step_line(verdict)]
        for direction, deltas in (diagnostics.get("qdisc_delta") or {}).items():
            for delta in deltas:
                if "note" in delta:
                    lines.append(f"  {direction} {delta['qdisc']} {delta['note']}")
                else:
                    lines.append(f"  {direction} {delta['qdisc']} sent {delta['sent_packets']} packets, dropped "
                                 f"{delta['dropped_packets']} ({delta['drop_pct']}%), overlimits "
                                 f"{delta['overlimits']}, backlog at end {delta['backlog_bytes_end']} bytes")
        lines.append(_simulator_line("at start", diagnostics.get("simulator_before")))
        lines.append(_simulator_line("at end", diagnostics.get("simulator_after")))
        sparse = diagnostics.get("sparse") or {}
        if "skipped" in sparse or "error" in sparse:
            lines.append(f"  Sparse UDP (ot_telemetry): {sparse.get('skipped') or sparse.get('error')}")
        elif sparse:
            lines.append(f"  Sparse UDP (ot_telemetry) over the step: loss {_num(sparse.get('ot_loss_pct'), 2, '%')}, "
                         f"jitter p95 {_num(sparse.get('ot_jitter_p95_ms'))} ms, p95 {_num(sparse.get('ot_p95_ms'), 0)} ms, "
                         f"bursts {sparse.get('ot_bursts')}")
        probe = diagnostics.get("probe") or {}
        if probe.get("skipped"):
            lines.append(f"  Probe: {probe['skipped']}")
        elif probe:
            lines.append(f"  Probe at end: {'replied' if probe.get('success') else 'failed'} "
                         f"{_num(probe.get('latency_ms'))} ms ({probe.get('detail') or ''})")
        lines.append("  Per-second readings:")
        lines += _timeline_lines(diagnostics)
        for direction, text in (diagnostics.get("tc_end") or {}).items():
            lines.append(f"  tc -s qdisc show ({direction}), end of window:")
            lines += [f"    {line}" for line in (text or "(no output)").splitlines()]
    if result.get("events"):
        lines += ["", "NetEm events during the session:"]
        for event in result["events"]:
            stamp = time.strftime("%H:%M:%S", time.localtime(event.get("timestamp", 0)))
            lines.append(f"  {stamp} [{event.get('kind')}] {event.get('message')}")
    return "\n".join(lines) + "\n"


def shaper_drops(delta_by_direction, seconds):
    """Drops at the download shaper over the window, from the aggregate counters: the fair queue when present, else
    the rate limiter. Each reports the drops of the queue below it, so one queue is counted, not both."""
    deltas = [delta for delta in (delta_by_direction or {}).get("down") or [] if "note" not in delta]
    queue = (next((delta for delta in deltas if delta["qdisc"].startswith("fq_codel")), None)
             or next((delta for delta in deltas if delta["qdisc"].startswith("tbf")), None))
    if queue is None:
        return {"drop_pct": None, "drops_ps": None, "qdisc_mbps": None}
    sent_bytes = queue.get("sent_bytes")
    return {
        "drop_pct": queue["drop_pct"],
        "drops_ps": round(queue["dropped_packets"] / seconds, 2) if seconds > 0 else None,
        "qdisc_mbps": None if sent_bytes is None or seconds <= 0 else round(sent_bytes * 8 / 1_000_000 / seconds, 1),
    }

