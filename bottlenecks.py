"""Where a calibration's performance limits come from.

Each step's readings become findings: what limits the WAN, the NetEm host, the hypervisor under it, the shaper's
buffer, the load generator, and the traffic users feel (latency and loss for sparse flows). Each finding names its
evidence and the next check to make. This module only judges what was measured; app.py gathers the readings.
"""

STEAL_WARN, STEAL_BAD = 2.0, 5.0          # hypervisor CPU steal, percent of the window
CPU_BUSY_WARN, CPU_BUSY_BAD = 80.0, 90.0  # NetEm host CPU busy, percent
PSI_WARN, PSI_BAD = 5.0, 20.0             # pressure stall (share of the last 10 s)
SAMPLER_LATE_MS = 1000.0                  # NetEm's own sampling this late makes the rates unreliable
SHAPER_OVERFLOW_PCT = 2.0                 # drops at the shaper, percent of offered packets
DELIVERED = 0.90                          # the WAN delivered this share of what was expected
LATENCY_WARN_MS, LATENCY_BAD_MS = 5.0, 15.0
WAN_RATE_REACHED = 0.95

SEVERITY_RANK = {"info": 0, "warn": 1, "bad": 2}
# The cause reported for a failing step is the most severe finding of the first of these categories present.
CAUSE_ORDER = ("hypervisor", "netem_host", "measurement", "shaper", "latency", "generator")
CAUSE_TITLES = {
    "hypervisor": "Hypervisor CPU steal on the NetEm VM",
    "netem_host": "NetEm host (CPU, pressure or NIC)",
    "measurement": "NetEm's own measurement was late",
    "shaper": "Shaper buffer overflow",
    "latency": "Latency added under load",
    "generator": "Load generator or path",
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
        findings.append(finding(
            target, "shaper", "warn",
            f"The shaper's buffer overflowed ({drop_pct:.1f}% of packets) while the WAN delivered",
            f"Drops at the shaper with {efficiency * 100:.0f}% of the expected rate delivered: TCP bursts exceed the buffer.",
            "Each drop costs a retransmission. Check the sparse loss and latency in this step for who pays for it."))

    sparse = diagnostics.get("sparse") or {}
    loss = sparse.get("ot_loss_pct")
    if loss is not None and loss > 0:
        findings.append(finding(
            target, "shaper", "bad" if loss >= 1.0 else "warn",
            f"OT telemetry lost {loss:.2f}% of its packets",
            f"Sparse UDP traffic under this step's load: loss {loss:.2f}%, jitter p95 "
            f"{_fmt(sparse.get('ot_jitter_p95_ms'))} ms, p95 {_fmt(sparse.get('ot_p95_ms'))} ms.",
            "Sparse traffic is losing packets behind bulk traffic. See the shaper and host findings for this step."))

    probe = diagnostics.get("probe") or {}
    if probe.get("success") and probe.get("latency_ms") is not None and probe.get("configured_delay_ms") is not None:
        excess = probe["latency_ms"] - probe["configured_delay_ms"]
        if excess > LATENCY_WARN_MS:
            findings.append(finding(
                target, "latency", "bad" if excess > LATENCY_BAD_MS else "warn",
                f"Probe latency {excess:.1f} ms beyond the configured delay",
                f"{probe['latency_ms']:.1f} ms round trip against {probe['configured_delay_ms']:g} ms configured.",
                "The extra delay is queueing. Compare with the shaper backlog in the per-second readings."))

    host_causes = {item["category"] for item in findings}
    if efficiency is not None and efficiency < DELIVERED and not host_causes & {"hypervisor", "netem_host", "measurement"}:
        findings.append(finding(
            target, "generator", "warn", f"The WAN delivered {efficiency * 100:.0f}% of the expected rate",
            "No host limit was seen in this step, so the load generator or the path is limiting.",
            "Check the Traffic Simulator VM's CPU and its path to the target."))
    return findings


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
