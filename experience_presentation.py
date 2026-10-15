"""Target-aligned display grades; the simulator's measured utility stays intact."""
import math


INVALID_EVIDENCE = {"row_limit", "clock_discontinuity", "configuration_changed_in_window",
                    "dropped_transactions", "persistence_unhealthy"}


def confirmed_alerts(dem):
    if dem.get("truncated") or INVALID_EVIDENCE.intersection(dem.get("evidence_issues") or []):
        return []
    return [item for item in dem.get("critical_alerts") or []
            if item.get("severity", "violation") in ("violation", "outage")]


def target_details(item, violations):
    quality, targets = item.get("quality") or {}, item.get("experience_targets") or {}
    details = []
    for key in violations:
        actual, target, unit = None, None, ""
        if key == "success":
            actual = item.get("availability_pct", item.get("success"))
            target, unit = targets.get("success_min_pct"), "% success"
        elif key == "deadline":
            if targets.get("hard_deadline"):
                details.append(f"{quality.get('deadline_misses', 'Observed')} late transactions (hard deadline {targets.get('deadline_ms', 0):g} ms)")
                continue
            actual = quality.get("packet_rtt_p95_upper_ms", item.get("p95_ms"))
            target, unit = targets.get("deadline_ms"), " ms P95"
        elif key == "loss":
            actual, target, unit = quality.get("udp_loss_pct"), targets.get("loss_max_pct"), "% packet loss"
        elif key == "jitter":
            actual = quality.get("udp_rtt_variation_p95_ms")
            target, unit = targets.get("jitter_max_ms"), " ms timing variation"
        elif key.startswith(("throughput_", "sustained_")):
            direction = key.rsplit("_", 1)[-1]
            sustained = key.startswith("sustained_")
            actual = quality.get(f"sustained_{direction}_mbps" if sustained else f"transfer_{direction}_p05_mbps")
            target = targets.get(f"min_sustained_{direction}_mbps" if sustained else f"min_{direction}_mbps")
            unit = f" Mbit/s {'download' if direction == 'down' else 'upload'}"
        label = key.replace("_", " ")
        details.append(f"{actual:g}{unit} (target {'≥' if key == 'success' or key.startswith(('throughput_', 'sustained_')) else '≤'} {target:g})"
                       if actual is not None and target is not None else f"{label} target missed")
    return "; ".join(details) or "configured critical target missed"


def display_score(dem, average, minimum=75, label_for=str, site_misses=()):
    """A confirmed critical miss cannot receive a passing display grade.

    75 starts the existing good band. Use the lower site threshold when present.
    This is an explicit acceptance grade, not a rescaling of measured latency/loss.
    Observation-only alerts and incomplete/corrupt evidence never invent a failure.
    """
    alerts = confirmed_alerts(dem)
    valid = not dem.get("truncated") and not INVALID_EVIDENCE.intersection(dem.get("evidence_issues") or [])
    failed = bool(alerts) or valid and dem.get("business_acceptance") == "fail"
    failures, scores = [], []
    applications = dem.get("applications") or {}
    for alert in alerts:
        name = alert.get("application")
        item = applications.get(name) or {}
        score = item.get("experience_score", item.get("experience"))
        if score is not None:
            scores.append(score)
        failures.append(f"{label_for(name)}: {target_details(item, alert.get('violations') or [])}")
    if valid and dem.get("scoring_complete") is True and not dem.get("evidence_issues") and dem.get("business_acceptance") != "unknown":
        failures.extend(site_misses)
        failed = failed or bool(site_misses)
    if failed and not failures:
        failures.append("A configured critical application target was missed")
    grade = average
    if average is not None and failed:
        ceiling = max(0, math.ceil(min(75, minimum if minimum is not None else 75)) - 1)
        grade = min(average, ceiling, *scores)
    return {"score": grade, "average_score": average, "target_failures": failures,
            "score_adjusted": grade != average}
