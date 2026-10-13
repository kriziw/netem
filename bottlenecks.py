"""Where a calibration's performance limits come from.

Each step's readings become findings: what limits the WAN, the NetEm host, the hypervisor under it, the shaper's
buffer, the load generator, and the traffic users feel (latency and loss for sparse flows). Each finding names its
evidence and the next check to make. This module only judges what was measured; app.py gathers the readings.
"""

import calibration_trace

STEAL_WARN, STEAL_BAD = 2.0, 5.0          # hypervisor CPU steal, percent of the window
CPU_BUSY_WARN, CPU_BUSY_BAD = 80.0, 90.0  # NetEm host CPU busy, percent
PSI_WARN, PSI_BAD = 5.0, 20.0             # pressure stall (share of the last 10 s)
SAMPLER_LATE_MS = 1000.0                  # NetEm's own sampling this late makes the rates unreliable
SHAPER_OVERFLOW_PCT = 2.0                 # drops at the shaper, percent of offered packets
DELIVERED = 0.90                          # the WAN delivered this share of what was expected
LATENCY_WARN_MS, LATENCY_BAD_MS = 5.0, 15.0
WAN_RATE_REACHED = 0.95
LOOP_STALL_BAD_MS = 500.0                # the simulator's event loop held this long is bad
QUIET_MIN_SECONDS = 3                    # quiet WAN seconds at or above this count are reported
BUSY_CLOSED_LOOP = 0.9                   # paced users busy this share of the window: the path set the rate
SIM_CPU_WARN, SIM_CPU_BAD = 70.0, 90.0   # the simulator process's CPU share of one core, percent
WAIT_SLOW_MS = 100.0                     # a paced download this slow to start sending is a slow target

SEVERITY_RANK = {"info": 0, "warn": 1, "bad": 2}
# The cause reported for a failing step is the most severe finding of the first of these categories present.
CAUSE_ORDER = ("hypervisor", "netem_host", "measurement", "simulator", "target", "shaper", "path", "latency", "generator")
# A step whose findings already name one of these is not also blamed on the generator by default.
EXPLAINED = {"hypervisor", "netem_host", "measurement", "simulator", "target", "path", "generator",
             "unattributed"}
# What a quiet WAN second is blamed on, by the cause the trace found for it.
QUIET_CAUSES = (
    ("loop", "simulator", "with the simulator event loop held", "Fix the loop stall in the simulator findings."),
    ("target", "target", "with the target answering slowly or not at all", "Check the target VM load and its event loop."),
    ("waiting", "path", "with requests still waiting on the WAN", "The WAN was slow to deliver what was requested: check the shaper and path findings."),
    ("idle", "generator", "with no requests in flight", "The generator stopped asking for traffic without its loop being held: check its pacing."),
)
CAUSE_TITLES = {
    "hypervisor": "Hypervisor CPU steal on the NetEm VM",
    "netem_host": "NetEm host (CPU, pressure or NIC)",
    "measurement": "NetEm's own measurement was late",
    "shaper": "Shaper buffer overflow",
    "latency": "Latency added under load",
    "generator": "Load generator or path",
    "simulator": "Traffic Simulator stalled (its event loop was held up)",
    "target": "Controlled target slow or not answering",
    "path": "Path or shaper: the WAN carried less than was sent",
}


def _max(rows, key):
    values = [row[key] for row in rows if row.get(key) is not None]
    return max(values) if values else None


def finding(step, category, severity, title, evidence, action):
    return {"step": step, "category": category, "severity": severity, "title": title,
            "evidence": evidence, "action": action}


def step_findings(step, shaped_mbps):
    """The findings for one step, from its verdict and the readings taken during its window."""
    diagnostics = step.get("diagnostics") or {}
    target = step.get("target_mbps")
    achieved = step.get("achieved_mbps")
    efficiency = step.get("efficiency")
    host_rows = diagnostics.get("host_timeline") or []
    findings = []

    if shaped_mbps and achieved is not None and achieved >= shaped_mbps * WAN_RATE_REACHED and target > shaped_mbps * WAN_RATE_REACHED:
        findings.append(finding(target, "shaper", "info", f"The WAN carried its configured {shaped_mbps:g} Mbit/s",
                                f"Offered {target} Mbit/s; the WAN carried {achieved} Mbit/s.",
                                "The shaping sets the limit above this step. Raise the WAN rate to find more."))

    steal = _max(host_rows, "cpu_steal_pct")
    if steal is not None and steal >= STEAL_WARN:
        findings.append(finding(
            target, "hypervisor", "bad" if steal >= STEAL_BAD else "warn",
            f"CPU steal on the NetEm VM up to {steal:.1f}%",
            f"The hypervisor withheld CPU from this VM for up to {steal:.1f}% of the window.",
            "Check the Proxmox host: CPU overcommit, other guests, and this VM's CPU type and pinning."))

    cpu = _max(host_rows, "cpu_busy_pct")
    if cpu is not None and cpu >= CPU_BUSY_WARN:
        findings.append(finding(
            target, "netem_host", "bad" if cpu >= CPU_BUSY_BAD else "warn",
            f"NetEm host CPU busy up to {cpu:.0f}%",
            f"Forwarding and sampling used up to {cpu:.0f}% of the CPU in the window.",
            "The NetEm host is CPU-bound here. Check the softirq load on the bridge and the telemetry sample rate."))

    pressure = {name: _max(host_rows, key) for name, key in
                (("CPU", "psi_cpu"), ("memory", "psi_memory"), ("disk I/O", "psi_io"))}
    pressure = {name: value for name, value in pressure.items() if value is not None and value >= PSI_WARN}
    if pressure:
        worst = max(pressure.values())
        findings.append(finding(
            target, "netem_host", "bad" if worst >= PSI_BAD else "warn",
            "Tasks waited for " + ", ".join(sorted(pressure)),
            "Pressure stall: " + ", ".join(f"{name} {value:.1f}%" for name, value in sorted(pressure.items())),
            "The NetEm host is short of that resource while this step runs."))

    nic = _max(host_rows, "nic_drops_ps")
    if nic is not None and nic > 0:
        findings.append(finding(
            target, "netem_host", "warn", f"NIC drops on the NetEm host up to {nic:.1f}/s",
            "The host's WAN interfaces dropped or errored packets during the window.",
            "Check the virtio NIC queues, offload settings and the bridge; these drops are below the shaper."))

    late = _max(host_rows, "sampler_lag_ms")
    if late is not None and late >= SAMPLER_LATE_MS:
        findings.append(finding(
            target, "measurement", "bad", f"NetEm's sampler was late by up to {late:.0f} ms",
            "The sampler woke late, so this step's rates are less reliable.",
            "Read the CPU and steal findings for this step; the host was not keeping up."))

    drop_pct = step.get("drop_pct")
    if drop_pct is not None and drop_pct > SHAPER_OVERFLOW_PCT and efficiency is not None and efficiency >= DELIVERED:
        shaper = diagnostics.get("shaper") or {}
        tail, dropped = shaper.get("tail_drops"), shaper.get("dropped_packets")
        if tail is not None and dropped:
            split = (f"{tail} tail drops at the queue's memory limit and {max(dropped - tail, 0)} CoDel early drops "
                     f"that keep its delay near the target")
        else:
            split = "the split between tail and CoDel drops was not read"
        findings.append(finding(
            target, "shaper", "warn",
            f"The shaper dropped {drop_pct:.1f}% of packets while the WAN delivered",
            f"Drops at the shaper with {efficiency * 100:.0f}% of the expected rate delivered: {split}.",
            "Tail drops mean the queue's memory limit is short for TCP bursts. Each drop costs a retransmission."))

    sparse = diagnostics.get("sparse") or {}
    loss = sparse.get("ot_loss_pct")
    if loss is not None and loss > 0:
        findings.append(finding(
            target, "shaper", "bad" if loss >= 1.0 else "warn",
            f"OT telemetry lost {loss:.2f}% of its packets",
            f"Sparse UDP traffic under this step's load: loss {loss:.2f}%, jitter p95 "
            f"{_fmt(sparse.get('ot_jitter_p95_ms'))} ms, p95 {_fmt(sparse.get('ot_p95_ms'))} ms.",
            "Sparse traffic is losing packets behind bulk traffic. See the shaper and host findings for this step."))

    if sparse and "skipped" not in sparse and "error" not in sparse and loss is None:
        findings.append(finding(
            target, "measurement", "info", "No sparse users ran in this step",
            "The traffic generator ran no OT-style sparse users, so loss and jitter under this load were not measured.",
            "Check the simulator's sparse slots for this run."))
    probe = diagnostics.get("probe") or {}
    if probe.get("success") and probe.get("latency_ms") is not None and probe.get("configured_delay_ms") is not None:
        excess = probe["latency_ms"] - probe["configured_delay_ms"]
        if excess > LATENCY_WARN_MS:
            findings.append(finding(
                target, "latency", "bad" if excess > LATENCY_BAD_MS else "warn",
                f"Probe latency {excess:.1f} ms beyond the configured delay",
                f"{probe['latency_ms']:.1f} ms round trip against {probe['configured_delay_ms']:g} ms configured.",
                "Additional delay may include queueing, baseline path latency or endpoint processing. Compare an unloaded baseline and the shaper backlog."))

    trace = (diagnostics.get("trace") or {}).get("summary") or {}
    findings.extend(_simulator_findings(target, trace, shaped_mbps, achieved))
    seen = {item["category"] for item in findings}
    if efficiency is not None and efficiency < DELIVERED and not seen & EXPLAINED:
        findings.append(finding(
            target, "generator", "warn", f"The WAN carried {efficiency * 100:.0f}% of the target rate",
            "No host, simulator or path limit was seen in this step, so the load generator or the path is limiting.",
            "Check the Traffic Simulator VM's CPU and its path to the target."))
    return findings


def _transfer_action(shaped_mbps, achieved):
    """What to check when downloads were slow to transfer: the shaper, when the WAN was at its configured rate."""
    if shaped_mbps and achieved is not None and achieved >= shaped_mbps * WAN_RATE_REACHED:
        return ("The WAN was at its configured rate, so the downloads shared it: the shaper set the transfer time, "
                "not the receive side. Raise the shaping to test higher.")
    return "The bytes arrived slowly once started: check the receive side's CPU and the path to the target."


def _simulator_findings(target, trace, shaped_mbps=None, achieved=None):
    """Findings from the simulator's own figures: its event loop, status polls, CPU, the target, the paced downloads,
    the quiet seconds, and what it completed against the target. A shortfall is attributed by whether the paced users
    were busy, not by comparing bytes with the WAN: both count the same bytes."""
    found = []
    loop = trace.get("loop_max_ms")
    if loop is not None and loop >= calibration_trace.LOOP_STALL_MS:
        found.append(finding(
            target, "simulator", "bad" if loop >= LOOP_STALL_BAD_MS else "warn",
            f"Traffic Simulator event loop held up to {loop:.0f} ms",
            f"{trace.get('loop_stall_seconds') or 0} seconds with a stall over {calibration_trace.LOOP_STALL_MS:.0f} ms. "
            f"Status took up to {_fmt(trace.get('status_ms_max'))} ms, the DEM sampler up to {_fmt(trace.get('dem_ms_max'))} ms "
            f"and database writes up to {_fmt(trace.get('write_ms_max'))} ms.",
            "Every workload user waits while the loop is held. Move the largest of those timings off the loop."))
    if trace.get("poll_errors"):
        found.append(finding(
            target, "simulator", "bad", f"{trace['poll_errors']} status polls got no answer within 2 s",
            "The management request timed out; network, API, worker or event-loop delay can cause this.",
            "Read the event-loop finding for this step: the simulator was not answering NetEm."))
    cpu = trace.get("sim_cpu_pct_max")
    if cpu is not None and cpu >= SIM_CPU_WARN:
        found.append(finding(
            target, "simulator", "bad" if cpu >= SIM_CPU_BAD else "warn",
            f"Traffic Simulator process CPU up to {cpu:.0f}% of one core",
            f"Its mean over the window was {_fmt(trace.get('sim_cpu_pct_mean'))}%. Receiving the paced downloads runs on that core.",
            "A saturated receive side slows every download it reads. Use fewer users per simulator, or more simulators."))
    slow = trace.get("target_max_ms")
    unanswered = trace.get("target_no_answer_seconds") or 0
    if unanswered or (slow is not None and slow >= calibration_trace.TARGET_SLOW_MS):
        found.append(finding(
            target, "target", "bad" if unanswered else "warn",
            f"Controlled target answered its health check slowly (up to {_fmt(slow)} ms)",
            f"{unanswered} seconds with no answer. This check traverses the traffic path, so the target and network contributions are not separated.",
            "Check the target VM's load and its event loop; the simulator's probe uses the same route."))
    quiet = trace.get("quiet_seconds") or 0
    if quiet >= QUIET_MIN_SECONDS:
        offsets = ", ".join(f"{offset:g} s" for offset in trace.get("quiet_offsets") or [])
        for key, category, phrase, action in QUIET_CAUSES:
            count = trace.get(f"quiet_{key}_seconds") or 0
            if count:
                found.append(finding(target, category, "warn", f"{count} of {quiet} quiet seconds {phrase}",
                                     f"Quiet WAN seconds at {offsets} into the window.", action))
    busy = trace.get("busy_fraction")
    wait = trace.get("wait_ms_mean")
    transfer = trace.get("transfer_ms_mean")
    if busy is not None and busy >= BUSY_CLOSED_LOOP and wait is not None and transfer is not None:
        if wait >= WAIT_SLOW_MS and wait >= transfer:
            found.append(finding(
                target, "target", "warn", f"Downloads waited {wait:.0f} ms for their first byte",
                f"Mean first byte {wait:.0f} ms against {transfer:.0f} ms to transfer, with the paced users busy "
                f"{busy * 100:.0f}% of the window.",
                "The first byte arrived late. Check path delay and target processing independently."))
        else:
            found.append(finding(
                target, "path", "warn", f"Downloads took {transfer:.0f} ms to transfer once started",
                f"Mean first byte {wait:.0f} ms, then {transfer:.0f} ms of transfer, with the paced users busy "
                f"{busy * 100:.0f}% of the window.",
                _transfer_action(shaped_mbps, achieved)))
    sent = trace.get("sim_mbps_mean")
    if sent is not None and target and sent < DELIVERED * target:
        shortfall = f"The simulator completed {sent:.1f} of {target} Mbit/s"
        users = trace.get("paced_users")
        basis = (f"{_fmt(trace.get('in_flight_mean'))} requests in flight on average across about {users} paced users"
                 if users else "The paced users")
        if busy is None:
            found.append(finding(
                target, "unattributed", "warn", shortfall,
                "This simulator does not report paced requests in flight, so the generator and the path cannot be told apart.",
                "Update the simulator: its paced-download counters show whether users waited on the path or rested."))
        elif busy >= BUSY_CLOSED_LOOP:
            found.append(finding(
                target, "path", "warn", f"{shortfall}: paced users were busy {busy * 100:.0f}% of the window",
                f"{basis}. Users waited on a download almost the whole time, so the {target} Mbit/s was never offered: "
                f"the path set the rate.",
                "Read the first-byte and transfer findings for this step to see whether the target or the receive side was slow."))
        else:
            found.append(finding(
                target, "generator", "warn", f"{shortfall}: paced users rested {100 - busy * 100:.0f}% of the window",
                f"{basis}. Users finished downloads early and waited before the next, so the generator did not offer the target.",
                "Check the generator's pacing: users should keep offering while the path has room."))
    return found


def analyse(result):
    """Findings for a whole calibration, most severe first, and the cause at the step that stopped the ladder."""
    steps = result.get("steps") or []
    shaped = result.get("wan_rate_mbps")
    findings = []
    for step in steps:
        findings.extend(step_findings(step, shaped))
    if steps and any(not ((step.get("diagnostics") or {}).get("probe") or {}).get("success")
                     and ((step.get("diagnostics") or {}).get("probe") or {}).get("skipped")
                     for step in steps):
        findings.append(finding(
            None, "measurement", "info", "No ICMP probe is configured for this WAN",
            "Latency under load is not measured for this calibration.",
            "Add an ICMP probe for this WAN under the probes settings to see latency under load."))
    findings.sort(key=lambda item: -SEVERITY_RANK[item["severity"]])
    failed = next((step for step in steps if step.get("status") == "fail"), None)
    cause = None
    if failed is not None:
        present = {item["category"] for item in findings if item["step"] == failed.get("target_mbps")
                   and item["severity"] != "info"}
        cause = next((category for category in CAUSE_ORDER if category in present), "generator")
    return {"findings": findings, "bottleneck": cause,
            "bottleneck_title": CAUSE_TITLES.get(cause) if cause else None}


def _fmt(value):
    return "—" if value is None else f"{value:.1f}"
