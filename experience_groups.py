"""Experience sub-scores for each traffic group, shown under the overall score.

The overall score is the worst business-critical application. A group's sub-score is the worst application in that group,
so a poor group shows even when another group sets the overall score. Each group also gets one short sentence: how it is
performing and, when it is not, what limits it, from the evidence the simulator scored.
"""

# The traffic groups, in the order the dashboard shows them. The names match the impact wording.
GROUPS = (("realtime", "Voice and video"), ("interactive", "Web and business apps"), ("bulk", "File transfers"))

# The score bands the overall score uses.
GOOD_SCORE = 75
FAIR_SCORE = 55

# Transfer components: the measured rate, the target it is scored against, and the direction.
TRANSFER_COMPONENTS = {
    "sustained_down": ("sustained_down_mbps", "min_sustained_down_mbps", "download"),
    "sustained_up": ("sustained_up_mbps", "min_sustained_up_mbps", "upload"),
    "throughput_down": ("transfer_down_p05_mbps", "min_down_mbps", "download"),
    "throughput_up": ("transfer_up_p05_mbps", "min_up_mbps", "upload"),
}


def tone_for(score):
    if score is None:
        return "unknown"
    if score >= GOOD_SCORE:
        return "good"
    return "warn" if score >= FAIR_SCORE else "bad"


def limit_reason(item):
    """The weakest measured part of an application's score, in plain words."""
    components = item.get("score_components") or {}
    quality = item.get("quality") or {}
    targets = item.get("experience_targets") or {}
    if not components:
        return "its measurements are incomplete"
    worst = min(components, key=components.get)
    if worst == "deadline" and quality.get("on_time_pct") is not None:
        return f"{quality['on_time_pct']:.0f}% of transactions met the {targets.get('deadline_ms', 0):.0f} ms target"
    if worst == "response_time" and item.get("p95_ms") is not None:
        return f"P95 response time {item['p95_ms']:.0f} ms"
    if worst == "reliability" and item.get("availability_pct") is not None:
        return f"{100 - item['availability_pct']:.1f}% of transactions failed"
    if worst == "udp_loss" and quality.get("udp_loss_pct") is not None:
        return f"{quality['udp_loss_pct']:.1f}% of packets were lost"
    if worst == "udp_rtt_variation" and quality.get("udp_rtt_variation_p95_ms") is not None:
        return f"jitter p95 was {quality['udp_rtt_variation_p95_ms']:.0f} ms"
    if worst in TRANSFER_COMPONENTS:
        evidence_key, target_key, direction = TRANSFER_COMPONENTS[worst]
        if quality.get(evidence_key) is not None and targets.get(target_key):
            return (f"{direction} transfers reached {quality[evidence_key]:.1f} Mbit/s "
                    f"against a {targets[target_key]:g} Mbit/s target")
    return f"its {worst.replace('_', ' ')} score is lowest"


def group_summary(key, label, members, label_for):
    requests = sum(int(item.get("requests") or 0) for item in members.values())
    summary = {"key": key, "label": label, "requests": requests}
    if not requests:
        return {**summary, "score": None, "tone": "unknown", "text": "No traffic in this group in the window."}
    scored = {name: item for name, item in members.items() if item.get("experience_score") is not None}
    unscored = len(members) - len(scored)
    if not scored:
        return {**summary, "score": None, "tone": "unknown",
                "text": f"Too few transactions to score yet: {requests} in the window."}
    worst = min(scored, key=lambda name: scored[name]["experience_score"])
    score = round(scored[worst]["experience_score"])
    tone = tone_for(score)
    if tone == "good":
        text = "Performing well."
    else:
        text = f"Limited by {label_for(worst)} ({score}/100): {limit_reason(scored[worst])}."
        if len(scored) > 1:
            healthy = sum(1 for item in scored.values() if item["experience_score"] >= GOOD_SCORE)
            text += f" {healthy} of {len(scored)} scored applications are performing well."
    if unscored:
        text += f" {unscored} with too few transactions to score yet."
    return {**summary, "score": score, "tone": tone, "text": text}


def experience_groups(dem, label_for=None):
    """The sub-score and sentence for each traffic group, from a simulator DEM summary."""
    label_for = label_for or (lambda name: str(name).replace("_", " ").capitalize())
    applications = (dem or {}).get("applications") or {}
    return [group_summary(key, label, {name: item for name, item in applications.items()
                                       if (item.get("class") or "interactive") == key}, label_for)
            for key, label in GROUPS]


def attach_experience_groups(dem, label_for=None):
    """Adds the group sub-scores to a simulator DEM summary, in place, so the first render and the live refresh both carry them."""
    if isinstance(dem, dict):
        dem["experience_groups"] = experience_groups(dem, label_for)
    return dem
