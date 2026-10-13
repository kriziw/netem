"""Is the NetEm host itself at its limits? Its CPU, memory, pressure, NIC and disk readings are stored with each
telemetry sample, so a disturbed measurement can be told apart from a real WAN or appliance effect.

This module only reads and assesses. The file roots are parameters, so tests can use fixture files."""
import os
from pathlib import Path

PROC = Path("/proc")
SYS_NET = Path("/sys/class/net")
NIC_FIELDS = ("rx_dropped", "tx_dropped", "rx_errors", "tx_errors")

# Lab thresholds as (busy, at limit). Most readings are worse when higher; mem_available_pct and disk_free_pct
# are worse when lower (LOWER_IS_WORSE).
LIMITS = {
    "cpu_busy_pct": (80.0, 90.0),
    "cpu_steal_pct": (2.0, 5.0),
    "psi_cpu": (5.0, 20.0),
    "psi_memory": (5.0, 20.0),
    "psi_io": (5.0, 20.0),
    "mem_available_pct": (15.0, 5.0),
    "disk_free_pct": (10.0, 3.0),
    "sampler_lag_ms": (250.0, 1000.0),
    "collect_ms": (500.0, 1000.0),
    "nic_drops_ps": (1.0, 10.0),
}
LOWER_IS_WORSE = {"mem_available_pct", "disk_free_pct"}
LABELS = {
    "cpu_busy_pct": "CPU busy",
    "cpu_steal_pct": "CPU steal (hypervisor)",
    "psi_cpu": "CPU pressure",
    "psi_memory": "Memory pressure",
    "psi_io": "Disk I/O pressure",
    "mem_available_pct": "Memory available",
    "disk_free_pct": "Disk free",
    "sampler_lag_ms": "Sampling lateness",
    "collect_ms": "Sample duration",
    "nic_drops_ps": "NIC drops and errors",
}
UNITS = {key: "%" for key in LIMITS}
UNITS.update(sampler_lag_ms=" ms", collect_ms=" ms", nic_drops_ps="/s")


def read_cpu_counters(proc=PROC):
    """Cumulative CPU time in jiffies from the aggregate line of /proc/stat, or None."""
    try:
        first = (proc / "stat").read_text().splitlines()[0].split()
    except (OSError, IndexError):
        return None
    if not first or first[0] != "cpu" or len(first) < 9:
        return None
    user, nice, system, idle, iowait, irq, softirq, steal = (int(value) for value in first[1:9])
    total = user + nice + system + idle + iowait + irq + softirq + steal
    return {"total": total, "idle": idle + iowait, "iowait": iowait, "steal": steal}


def cpu_percentages(before, after):
    """Busy, steal and I/O-wait shares of the CPU time between two readings."""
    if not before or not after:
        return {}
    span = after["total"] - before["total"]
    if span <= 0:
        return {}
    busy = 100.0 - 100.0 * (after["idle"] - before["idle"]) / span
    steal = 100.0 * (after["steal"] - before["steal"]) / span
    iowait = 100.0 * (after["iowait"] - before["iowait"]) / span
    return {"cpu_busy_pct": round(busy, 1), "cpu_steal_pct": round(steal, 1), "cpu_iowait_pct": round(iowait, 1)}


def read_meminfo(proc=PROC):
    try:
        text = (proc / "meminfo").read_text()
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            values[name.strip()] = int(fields[0])
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total or available is None:
        return {}
    return {"mem_available_pct": round(100.0 * available / total, 1)}


def read_pressure(proc=PROC):
    """Pressure stall information: the share of the last 10 seconds in which some task waited for CPU, memory or I/O."""
    readings = {}
    for resource in ("cpu", "memory", "io"):
        key = "psi_" + resource
        readings[key] = None
        try:
            text = (proc / "pressure" / resource).read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if not line.startswith("some "):
                continue
            for token in line.split()[1:]:
                name, _, value = token.partition("=")
                if name == "avg10":
                    try:
                        readings[key] = float(value)
                    except ValueError:
                        pass
    return readings


def nic_total(sys_net, interfaces):
    """Cumulative drops and errors on the WAN interfaces, or None when none can be read."""
    total, seen = 0, False
    for name in interfaces:
        for field in NIC_FIELDS:
            try:
                total += int((sys_net / name / "statistics" / field).read_text())
            except (OSError, ValueError):
                continue
            seen = True
    return total if seen else None


def disk_free_pct(path):
    try:
        info = os.statvfs(path)
    except (OSError, AttributeError):  # no statvfs on Windows
        return None
    return round(100.0 * info.f_bavail / info.f_blocks, 1) if info.f_blocks else None


def sample_host(state, interfaces, now, proc=PROC, sys_net=SYS_NET, disk_path=None):
    """One reading of the host. `state` holds the previous cumulative counters and `now` is a monotonic clock.
    Returns the reading and the state to pass to the next call."""
    cpu = read_cpu_counters(proc)
    reading = dict(cpu_percentages(state.get("cpu"), cpu))
    reading.update(read_meminfo(proc))
    reading.update(read_pressure(proc))
    total = nic_total(sys_net, interfaces)
    previous = state.get("nic")
    reading["nic_drops_ps"] = None
    if total is not None and previous and now > previous["at"] and total >= previous["total"]:
        reading["nic_drops_ps"] = round((total - previous["total"]) / (now - previous["at"]), 2)
    reading["disk_free_pct"] = disk_free_pct(disk_path) if disk_path else None
    next_nic = {"total": total, "at": now} if total is not None else previous
    return reading, {"cpu": cpu or state.get("cpu"), "nic": next_nic}


def assess(reading):
    """The worst level across the readings, with the readings that reached it. Missing readings are skipped."""
    reasons, known = [], False
    for key, (busy, limit) in LIMITS.items():
        value = reading.get(key)
        if value is None:
            continue
        known = True
        lower = key in LOWER_IS_WORSE
        at_limit = value <= limit if lower else value >= limit
        at_busy = value <= busy if lower else value >= busy
        if at_limit or at_busy:
            reasons.append({"key": key, "label": LABELS[key], "value": value, "unit": UNITS[key],
                            "state": "at_limit" if at_limit else "busy"})
    if not known:
        return {"status": "unknown", "reasons": []}
    reasons.sort(key=lambda item: item["state"] != "at_limit")
    if any(item["state"] == "at_limit" for item in reasons):
        status = "at_limit"
    else:
        status = "busy" if reasons else "ok"
    return {"status": status, "reasons": reasons}


def summarize(rows, interval_s, window_s):
    """Window summary for the Platform Health page. Rows are host readings, oldest first, each with its status
    and reasons already decoded."""
    latest = rows[-1] if rows else None

    def seconds(predicate):
        return sum(1 for row in rows if predicate(row)) * interval_s

    readings = []
    for key, (busy, limit) in LIMITS.items():
        values = [row[key] for row in rows if row.get(key) is not None]
        if not values:
            continue
        readings.append({"key": key, "label": LABELS[key], "unit": UNITS[key],
                         "current": latest.get(key) if latest else None,
                         "worst": min(values) if key in LOWER_IS_WORSE else max(values),
                         "busy": busy, "limit": limit})

    intervals = []
    limited = False  # the previous reading was busy or at limit: a healthy reading ends an interval
    for row in rows:
        if row["status"] not in ("busy", "at_limit"):
            limited = False
            continue
        labels = [item["label"] for item in row["reasons"]]
        last = intervals[-1] if intervals else None
        if last and limited and row["timestamp"] - last["end"] <= 2 * interval_s:
            last["end"] = row["timestamp"]
            if row["status"] == "at_limit":
                last["status"] = "at_limit"
            last["reasons"] += [label for label in labels if label not in last["reasons"]]
        else:
            intervals.append({"start": row["timestamp"], "end": row["timestamp"], "status": row["status"],
                              "reasons": labels})
        limited = True

    late_limit = LIMITS["sampler_lag_ms"][1]
    collect_limit = LIMITS["collect_ms"][1]
    return {
        "status": latest["status"] if latest else "unknown",
        "reasons": latest["reasons"] if latest else [],
        "samples": len(rows),
        "window_seconds": window_s,
        "interval_seconds": interval_s,
        "at_limit_seconds": seconds(lambda row: row["status"] == "at_limit"),
        "busy_seconds": seconds(lambda row: row["status"] == "busy"),
        "late_seconds": seconds(lambda row: (row.get("sampler_lag_ms") or 0) >= late_limit
                                or (row.get("collect_ms") or 0) >= collect_limit),
        "readings": readings,
        "intervals": intervals[-10:],
        "current": latest,
    }
