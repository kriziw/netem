"""Experience sub-scores for each traffic group, shown under the overall score.

Measured averages use configured workload and criticality weights. Confirmed
critical target misses keep the display grade below the passing band.
Each group also gets one short sentence: how it is
performing and, when it is not, what limits it, from the evidence the simulator scored.
"""
from experience_presentation import display_score

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


def group_summary(key, label, members, label_for, evidence=None):
    requests = sum(int(item.get("requests") or 0) for item in members.values())
    summary = {"key": key, "label": label, "requests": requests}
    if not requests:
        return {**summary, "score": None, "tone": "unknown", "text": "No traffic in this group in the window."}
    scored = {name: item for name, item in members.items() if item.get("experience_score") is not None}
    unscored = len(members) - len(scored)
    incomplete = any(item.get("scoring_complete") is False for item in members.values())
    if not scored:
        return {**summary, "score": None, "tone": "unknown",
                "scoring_complete": False,
                "text": ("Measurements are unavailable: " + ", ".join(sorted({
                    str(issue).replace('_', ' ') for item in members.values()
                    for issue in (item.get('evidence_issues') or [])})) + "."
                    if any(item.get('evidence_issues') for item in members.values()) else
                    "Required application measurements are missing; see application details."
                    if any(item.get('missing_measurements') for item in members.values()) else
                    f"Too few transactions to score yet: {requests} in the window.")}
    worst = min(scored, key=lambda name: scored[name]["experience_score"])
    weights = {name: item.get("weight") if item.get("weight") is not None else 1 for name, item in scored.items()}
    total_weight = sum(weights.values())
    score = round(sum(weights[name] * item["experience_score"] for name, item in scored.items()) / total_weight) if total_weight else None
    alerts = [{"application": name, "severity": "violation", "violations": item["target_violations"]}
              for name, item in members.items() if item.get("target_violations")
              and (item.get("critical") or (item.get("experience_targets") or {}).get("critical"))]
    grade = display_score({**(evidence or {}), "applications": members, "critical_alerts": alerts,
                           "business_acceptance": None}, score, label_for=label_for)
    if grade["score_adjusted"]:
        return {**summary, **grade, "tone": tone_for(grade["score"]), "scoring_complete": not incomplete and not unscored,
                "text": "Critical targets missed: " + "; ".join(grade["target_failures"]) + "."}
    if incomplete or unscored:
        limited = any(item.get('sample_confidence') == 'limited_sample' for item in scored.values())
        missing = [label_for(name) for name, item in members.items() if not item.get('requests')]
        text = (f"Measured from {requests} transactions; limited sample, acceptance not yet confirmed."
                if limited and not unscored else "Incomplete application evidence; this group cannot yet be judged.")
        if missing:
            text += ' Awaiting: ' + ', '.join(missing) + '.'
        if score is not None and score < GOOD_SCORE:
            text += f" Observed impact: {label_for(worst)} ({scored[worst]['experience_score']:.0f}/100): {limit_reason(scored[worst])}."
        return {**summary, "score": score, "tone": "unknown", "scoring_complete": False,
                "sample_confidence": 'limited_sample' if limited else 'partial_coverage',
                "text": text}
    tone = tone_for(score)
    if tone == "good":
        text = "Performing well."
    else:
        text = f"Limited by {label_for(worst)} ({scored[worst]['experience_score']:.0f}/100): {limit_reason(scored[worst])}."
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
    result = []
    for key, label in GROUPS:
        members = {name: item for name, item in applications.items() if (item.get('class') or 'interactive') == key}
        group = group_summary(key, label, members, label_for, dem)
        group['applications'] = [{'label': label_for(name), 'score': item.get('experience_score'),
            'requests': item.get('requests') or 0, 'sample_confidence': item.get('sample_confidence'),
            'scoring_complete': item.get('scoring_complete'),
            'missing_measurements': item.get('missing_measurements') or [],
            'evidence_issues': item.get('evidence_issues') or [], 'success_pct': item.get('availability_pct'),
            'p95_ms': item.get('p95_ms'), 'class': item.get('class') or 'interactive',
            'age_s': max(0, (dem.get('until') or 0) - item['last_observation_at'])
                     if dem.get('until') is not None and item.get('last_observation_at') is not None else None}
            for name, item in members.items()]
        result.append(group)
    return result


def attach_experience_groups(dem, label_for=None):
    """Adds the group sub-scores to a simulator DEM summary, in place, so the first render and the live refresh both carry them."""
    if isinstance(dem, dict):
        dem["experience_groups"] = experience_groups(dem, label_for)
    return dem

