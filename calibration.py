"""Bandwidth calibration: step paced load up a ladder and find the highest rate the infrastructure carries.

Each step offers a target rate through the traffic path under test. A step fails when the WAN delivers too little of
what was offered, or when the NetEm host is at its limit or its sampler runs late. Drops at the shaper are reported as
warnings, not failures: TCP bulk transfers drop packets at a small buffer by design, and the WAN can still deliver its
rate. The steps stop at the first failure, or once the configured WAN rate is reached, since higher steps cannot be
told apart then. This module only judges: app.py drives the load and measures the WAN.
"""

LADDER_MBPS = (50, 100, 200, 400, 600, 800, 1000)
SETTLE_S = 10.0          # let TCP reach its steady rate before measuring
MEASURE_S = 30.0         # the window each step is judged on; long enough that a few transfers per user
                         # cannot tilt it, since the simulator staggers its users
EFFICIENCY_PASS = 0.90   # achieved / expected, where expected is the target capped at the WAN rate
DROP_NOTE_PCT = 0.5      # shaper drops above this share of offered packets are reported as a warning
REACHED_SHAPING = 0.95   # a step at or above this share of the WAN rate means the shaping is the limit


def judge_step(target_mbps, shaped_mbps, achieved_mbps, drop_pct, host_status, late_seconds, drops_ps=None):
    """The verdict for one step. Missing readings are not treated as passing: they fail the step they block."""
    expected = min(target_mbps, shaped_mbps) if shaped_mbps else target_mbps
    reasons, warnings = [], []
    if achieved_mbps is None:
        reasons.append("no WAN measurement in the window")
        achieved = 0.0
    else:
        achieved = achieved_mbps
    efficiency = achieved / expected if expected > 0 else 0.0
    if host_status in (None, "unknown", "unavailable"):
        reasons.append("NetEm host measurement unavailable")
    if host_status == "at_limit":
        reasons.append("NetEm host at its limit")
    if late_seconds:
        reasons.append(f"sampler late for {late_seconds:.0f} s")
    if achieved_mbps is not None and efficiency < EFFICIENCY_PASS:
        reasons.append(f"achieved {achieved:.0f} of {expected:.0f} Mbit/s")
    if drop_pct is not None and drop_pct > DROP_NOTE_PCT:
        warnings.append(f"{drop_pct:.1f}% of packets dropped at the shaper")
    return {
        "target_mbps": target_mbps,
        "expected_mbps": round(expected, 1),
        "achieved_mbps": None if achieved_mbps is None else round(achieved_mbps, 1),
        "efficiency": round(efficiency, 3),
        "drop_pct": None if drop_pct is None else round(drop_pct, 3),
        "drops_ps": None if drops_ps is None else round(drops_ps, 1),
        "host_status": host_status,
        "status": "fail" if reasons else "pass",
        "reasons": reasons,
        "warnings": warnings,
    }


def limited_by(step):
    """What stopped the ladder at this step, in the terms an operator acts on."""
    text = " ".join(step["reasons"])
    if "host" in text or "sampler" in text:
        return "NetEm host (CPU, scheduling or NIC)"
    cause = "path or traffic generator: the WAN carried less than the step offered"
    if step.get("drop_pct") is not None and step["drop_pct"] > DROP_NOTE_PCT:
        cause += f"; the shaper also dropped {step['drop_pct']:.1f}% of packets"
    return cause


def summarise(steps, shaped_mbps):
    """The calibrated capacity: the highest achieved rate of a passing step, and what limited it."""
    passed = [step for step in steps if step["status"] == "pass"]
    capacity = max((step["achieved_mbps"] for step in passed), default=None)
    failed = next((step for step in steps if step["status"] == "fail"), None)
    if failed is not None:
        cause = limited_by(failed)
    elif shaped_mbps and capacity is not None and capacity >= shaped_mbps * REACHED_SHAPING:
        cause = f"the configured WAN rate ({shaped_mbps:g} Mbit/s); raise the WAN shaping to test higher"
    elif shaped_mbps and capacity is not None:
        cause = (f"no step failed, but the top passing step carried {capacity / shaped_mbps * 100:.0f}% of the configured "
                 f"{shaped_mbps:g} Mbit/s, short of the {REACHED_SHAPING * 100:.0f}% that shows the shaping as the limit")
    elif steps:
        cause = f"no step failed: the ladder stopped at {steps[-1]['target_mbps']} Mbit/s"
    else:
        cause = "no step ran"
    return {"capacity_mbps": capacity, "wan_rate_mbps": shaped_mbps, "limited_by": cause,
            "steps": steps, "passed": len(passed), "failed": 1 if failed else 0}
