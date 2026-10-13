"""Platform self-test: does the NetEm platform operate as intended?

Each check compares what the host, its WAN interfaces and bridges, the applied shaping, the telemetry, and the
path through each WAN report with what the lab configured. The checks are pure: app.py gathers a facts dict and
these functions judge it, so every outcome can be tested. The same facts feed the diagnostics report, so each
verdict can be traced to the readings behind it.
"""
import json
import re
from datetime import datetime, timezone

PASS, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"
RANK = {SKIP: 0, PASS: 1, WARN: 2, FAIL: 3}
GROUP_TITLES = {
    "host": "NetEm host",
    "sampling": "Measurement",
    "clock": "Clock",
    "telemetry": "Telemetry",
    "database": "Telemetry database",
    "simulator": "Traffic Simulator",
    "software": "Software and bridges",
    "interfaces": "WAN interfaces",
    "shaping": "Applied shaping",
    "paths": "Paths (idle only)",
}
RATE_TOLERANCE = 0.02       # relative error allowed on an applied rate
DELAY_TOLERANCE_MS = 0.5    # absolute error allowed on an applied delay
LOSS_TOLERANCE_PCT = 0.01   # absolute error allowed on an applied loss
EXCESS_WARN_MS = 5.0        # latency beyond the configured delay that is still normal on an idle path
EXCESS_FAIL_MS = 15.0
SECRET_KEY = re.compile(r"key|token|secret|password|passphrase|credential", re.IGNORECASE)


def result(check_id, group, title, status, detail, measured=None, expected=None):
    return {"id": check_id, "group": group, "title": title, "status": status, "detail": detail,
            "measured": measured, "expected": expected}


def check_host(facts):
    host = facts.get("host") or {}
    status = host.get("status")
    reasons = "; ".join(f"{item['label']} {item['value']}{item['unit']}" for item in host.get("reasons", []))
    title = "Host headroom"
    if status == "ok":
        return [result("host.headroom", "host", title, PASS, "No reading is busy or at its limit.", status)]
    if status == "busy":
        return [result("host.headroom", "host", title, WARN, "Busy: " + reasons, status)]
    if status == "at_limit":
        return [result("host.headroom", "host", title, FAIL, "At limit: " + reasons, status)]
    return [result("host.headroom", "host", title, SKIP, "No host readings yet.")]


def check_sampling(facts):
    host = facts.get("host") or {}
    if not host.get("samples"):
        return [result("sampling.lateness", "sampling", "Sampler lateness", SKIP, "No samples in the window yet.")]
    late, minutes = host.get("late_seconds", 0), (host.get("window_seconds") or 0) // 60
    if late <= 0:
        return [result("sampling.lateness", "sampling", "Sampler lateness", PASS,
                       f"NetEm's sampler kept to time in the last {minutes} min.", late, 0)]
    status = WARN if late < 10 else FAIL
    return [result("sampling.lateness", "sampling", "Sampler lateness", status,
                   f"The sampler was a second or more late for {late:.0f} s in the last {minutes} min.", late, 0)]


def check_clock(facts):
    sync = (facts.get("clock") or {}).get("synchronized")
    title = "Clock synchronised"
    if sync is True:
        return [result("clock.ntp", "clock", title, PASS, "NTP reports the clock is synchronised.", True, True)]
    if sync is False:
        return [result("clock.ntp", "clock", title, WARN,
                       "NTP reports the clock is not synchronised; timestamps may drift.", False, True)]
    return [result("clock.ntp", "clock", title, SKIP, "NTP state cannot be read on this host.")]


def check_telemetry(facts):
    if not facts.get("links"):
        return [result("telemetry.freshness", "telemetry", "Telemetry freshness", SKIP, "No WAN links are configured.")]
    telemetry = facts.get("telemetry") or {}
    age, interval = telemetry.get("age_s"), telemetry.get("interval_s") or 2.0
    title = "Telemetry freshness"
    if age is None:
        return [result("telemetry.freshness", "telemetry", title, FAIL, "No telemetry sample exists for at least one WAN.")]
    if age > 3 * interval:
        return [result("telemetry.freshness", "telemetry", title, FAIL,
                       f"The newest sample is {age:.0f} s old; the sampler has stopped or is blocked.", round(age, 1), interval)]
    return [result("telemetry.freshness", "telemetry", title, PASS,
                   f"The newest sample is {age:.1f} s old.", round(age, 1), interval)]


def check_database(facts):
    database = facts.get("database") or {}
    check, free = database.get("quick_check"), database.get("disk_free_pct")
    integrity = "Telemetry database integrity"
    if check is None:
        results = [result("database.integrity", "database", integrity, SKIP, "Not checked.")]
    elif check == "ok":
        results = [result("database.integrity", "database", integrity, PASS, "SQLite quick check passed.", check, "ok")]
    else:
        results = [result("database.integrity", "database", integrity, FAIL, f"SQLite quick check reports: {check}", check, "ok")]
    space = "Free space for telemetry"
    if free is None:
        results.append(result("database.space", "database", space, SKIP, "Free space cannot be read."))
    else:
        status = FAIL if free < 5 else WARN if free < 15 else PASS
        results.append(result("database.space", "database", space, status,
                              f"{free:.1f}% of the telemetry disk is free.", free, 15))
    return results


def check_simulator(facts):
    simulator = facts.get("simulator") or {}
    title = "Traffic Simulator connection"
    if simulator.get("connected") is True:
        return [result("simulator.connected", "simulator", title, PASS, "The Traffic Simulator is connected.", True, True)]
    if simulator.get("connected") is False:
        detail = "The Traffic Simulator is not connected, so tests cannot run. " + (simulator.get("detail") or "")
        return [result("simulator.connected", "simulator", title, WARN, detail.strip(), False, True)]
    return [result("simulator.connected", "simulator", title, SKIP, "Not configured.")]


def check_software(facts):
    results = []
    forwarding = facts.get("forwarding")
    title = "IP forwarding"
    if forwarding == "1":
        results.append(result("software.forwarding", "software", title, PASS, "IP forwarding is enabled.", "1", "1"))
    elif forwarding is None:
        results.append(result("software.forwarding", "software", title, SKIP, "Cannot be read on this host."))
    else:
        results.append(result("software.forwarding", "software", title, FAIL,
                              "IP forwarding is off; traffic cannot pass the WAN bridges.", forwarding, "1"))
    problems = facts.get("bridge_problems")
    title = "WAN bridges"
    if problems is None:
        results.append(result("software.bridges", "software", title, SKIP, "Bridges cannot be read on this host."))
    elif not problems:
        results.append(result("software.bridges", "software", title, PASS, "Every WAN bridge is up with both ports attached.", [], []))
    else:
        summary = "; ".join(item["summary"] for item in problems)
        results.append(result("software.bridges", "software", title, FAIL, summary,
                              [item["link_id"] for item in problems], []))
    return results


def check_links(facts):
    results = []
    for link in facts.get("links", []):
        label = link.get("label") or link["id"]
        for iface in link.get("interfaces", []):
            check_id = f"interfaces.{link['id']}.{iface['role']}"
            title = f"{label} {iface['role']} link ({iface['name']})"
            if not iface.get("present"):
                results.append(result(check_id, "interfaces", title, FAIL,
                                      "Interface not found. It may be renamed, unplugged or missing after a reboot.", False, True))
            elif iface.get("carrier") is False:
                results.append(result(check_id, "interfaces", title, FAIL,
                                      "No carrier: check the cable, the peer port and the NIC.", False, True))
            elif iface.get("carrier") is True:
                results.append(result(check_id, "interfaces", title, PASS, "Link is up.", True, True))
            else:
                results.append(result(check_id, "interfaces", title, SKIP, "Carrier cannot be read."))
            errors = iface.get("errors_delta")
            errors_title = title + ", drops and errors"
            if errors is None:
                results.append(result(check_id + ".errors", "interfaces", errors_title, SKIP, "Counters cannot be read."))
            elif errors > 0:
                results.append(result(check_id + ".errors", "interfaces", errors_title, WARN,
                                      f"{errors} drops or errors on this interface during the test window.", errors, 0))
            else:
                results.append(result(check_id + ".errors", "interfaces", errors_title, PASS,
                                      "No drops or errors during the test window.", 0, 0))
    return results


def check_shaping(facts):
    results = []
    for link in facts.get("links", []):
        label = link.get("label") or link["id"]
        check_id, title = f"shaping.{link['id']}", f"{label} applied shaping"
        shaping = link.get("shaping") or {}
        if shaping.get("skipped"):
            results.append(result(check_id, "shaping", title, SKIP, shaping["skipped"]))
            continue
        problems, measured, expected = [], {}, {}
        for direction in ("down", "up"):
            item = shaping.get(direction)
            if not item:
                continue
            want, got = item["expected"], item["applied"]
            measured[direction], expected[direction] = got, want
            rate_want, rate_got = want.get("rate_mbit"), got.get("rate_mbit")
            if rate_want and (rate_got is None or abs(rate_got - rate_want) > max(0.5, RATE_TOLERANCE * rate_want)):
                shown = "none" if rate_got is None else f"{rate_got:g}"
                problems.append(f"{direction} rate: configured {rate_want:g} Mbit/s, applied {shown} Mbit/s")
            delay_want, delay_got = want.get("delay_ms") or 0.0, got.get("delay_ms") or 0.0
            if abs(delay_got - delay_want) > DELAY_TOLERANCE_MS:
                problems.append(f"{direction} delay: configured {delay_want:g} ms, applied {delay_got:g} ms")
            loss_want, loss_got = want.get("loss_pct") or 0.0, got.get("loss_pct") or 0.0
            if abs(loss_got - loss_want) > LOSS_TOLERANCE_PCT:
                problems.append(f"{direction} loss: configured {loss_want:g}%, applied {loss_got:g}%")
        if not measured:
            results.append(result(check_id, "shaping", title, SKIP, "No profile has been applied to this WAN yet."))
            continue
        status = FAIL if problems else PASS
        detail = "; ".join(problems) if problems else "Applied rate, delay and loss match the configured profile."
        results.append(result(check_id, "shaping", title, status, detail, measured, expected))
    return results


def check_paths(facts):
    results = []
    for link in facts.get("links", []):
        label = link.get("label") or link["id"]
        check_id, title = f"paths.{link['id']}", f"{label} path latency"
        probe = link.get("probe") or {}
        if not probe or probe.get("skipped"):
            detail = probe.get("skipped") or "No ICMP probe is configured for this WAN."
            results.append(result(check_id, "paths", title, SKIP, detail))
            continue
        if not probe.get("success"):
            results.append(result(check_id, "paths", title, FAIL,
                                  f"Probe to {probe.get('target')} failed: {probe.get('detail') or 'no reply'}"))
            continue
        latency, delay = probe["latency_ms"], probe.get("configured_delay_ms") or 0.0
        excess = latency - delay
        status = PASS if excess <= EXCESS_WARN_MS else WARN if excess <= EXCESS_FAIL_MS else FAIL
        detail = (f"{latency:.1f} ms round trip to {probe.get('target')}, {delay:g} ms configured: "
                  f"{excess:+.1f} ms beyond the configured delay.")
        results.append(result(check_id, "paths", title, status, detail, round(latency, 2), delay))
    return results


CHECKS = (check_host, check_sampling, check_clock, check_telemetry, check_database, check_simulator,
          check_software, check_links, check_shaping, check_paths)


def run_self_test(facts, now=None):
    """Judge the gathered facts. The overall status is the worst judged check; skipped checks are not judged."""
    results = []
    for check in CHECKS:
        results.extend(check(facts))
    counts = {status: sum(1 for item in results if item["status"] == status) for status in (PASS, WARN, FAIL, SKIP)}
    judged = [item["status"] for item in results if item["status"] != SKIP]
    overall = max(judged, key=RANK.get) if judged else SKIP
    return {"generated_at": now, "status": overall, "counts": counts, "checks": results}


def redact(value):
    """Configuration for a support bundle: anything that looks like a credential is replaced, never copied."""
    if isinstance(value, dict):
        return {key: "[redacted]" if SECRET_KEY.search(str(key)) and item else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def stamp(epoch):
    if epoch is None:
        return "unknown"
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def diagnostics_text(report, facts, environment):
    """A plain-text report for a ticket: every verdict, the reading behind it, and the raw evidence."""
    counts = report["counts"]
    lines = ["NetEm platform self-test", f"Generated: {stamp(report['generated_at'])}",
             f"Overall: {report['status'].upper()} · {counts[PASS]} pass, {counts[WARN]} warn, "
             f"{counts[FAIL]} fail, {counts[SKIP]} skipped", ""]
    for group, title in GROUP_TITLES.items():
        items = [item for item in report["checks"] if item["group"] == group]
        if not items:
            continue
        lines.append(title.upper())
        for item in items:
            lines.append(f"  [{item['status'].upper()}] {item['title']}: {item['detail']}")
            if item["measured"] is not None or item["expected"] is not None:
                lines.append(f"      measured: {json.dumps(item['measured'], default=str)}"
                             f"  expected: {json.dumps(item['expected'], default=str)}")
        lines.append("")
    lines.append("EVIDENCE")
    for entry in facts.get("evidence", []):
        lines.append(f"  {entry['name']}:")
        lines += [f"    {line}" for line in (entry.get("text") or "(no output)").splitlines()]
    lines.append("")
    lines.append("ENVIRONMENT")
    lines += [f"  {key}: {value}" for key, value in environment.items()]
    return "\n".join(lines) + "\n"


def diagnostics_json(report, facts, environment, configuration):
    """The same evidence as JSON, for tools: the verdicts, the facts they were judged on, and the environment."""
    bundle = {"report": report, "facts": facts, "environment": environment, "configuration": redact(configuration)}
    return json.dumps(bundle, indent=2, default=str, sort_keys=True) + "\n"
