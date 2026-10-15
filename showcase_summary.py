"""Presentation-only session facts and test briefings; never invent a target pass."""
import math


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def session_facts(summaries, clean):
    summaries = sorted(summaries, key=lambda item: item.get('ended_at') or 0)
    outcomes = {'pass': 0, 'fail': 0, 'unknown': 0}
    execution = {'completed': 0, 'attention': 0, 'stopped': 0}
    recovery = {'pass': 0, 'fail': 0, 'unknown': 0, 'not_assessed': 0}
    results = []
    scores, times = [], []
    for summary in summaries:
        outcome = summary.get('experience_result')
        outcomes[outcome if outcome in outcomes else 'unknown'] += 1
        result = summary.get('execution_result') or summary.get('result')
        result = result if result in ('passed', 'failed', 'stopped') else 'unknown'
        if result in ('passed', 'failed', 'stopped'):
            execution[{'passed': 'completed', 'failed': 'attention', 'stopped': 'stopped'}[result]] += 1
        recovery_phases = [phase for phase in summary.get('phases') or [] if phase.get('name') == 'Recovery']
        reached_recovery = [phase for phase in recovery_phases if phase.get('reached') is not False]
        recovery_result = reached_recovery[-1].get('experience_verdict', 'unknown') if reached_recovery else 'unknown' if recovery_phases else 'not_assessed'
        recovery_result = recovery_result if recovery_result in recovery else 'unknown'
        recovery[recovery_result] += 1
        unmet = [clean(f"{phase.get('name') or 'Phase'}: {line}") for phase in summary.get('phases') or []
                 for line in phase.get('target_failures') or []]
        unmet.extend(clean(item.get('label')) for item in (summary.get('assertions') or {}).get('items') or []
                     if item.get('passed') is False)
        results.append({'name': clean(summary.get('name')), 'execution': result,
                        'requirements': outcome if outcome in outcomes else 'unknown', 'recovery': recovery_result,
                        'unmet_requirements': unmet,
                        'conclusion': [clean(line) for line in summary.get('narrative') or summary.get('conclusion') or []]})
        for phase in summary.get('phases') or []:
            if phase.get('reached') is False:
                continue
            if number(phase.get('experience_score')):
                scores.append((phase['experience_score'], bool(phase.get('note')) or phase.get('score_status') == 'provisional'))
            if number(phase.get('interactive_p95_ms')):
                times.append(phase['interactive_p95_ms'])
    lowest = min(scores, key=lambda item: item[0]) if scores else (None, False)
    latest = summaries[-1] if summaries else None
    return {'tests_completed': sum(item.get('result') in ('passed', 'failed') for item in summaries),
            'tests_stopped': sum(item.get('result') == 'stopped' for item in summaries),
            'execution': execution, 'recovery': recovery, 'demonstration_results': results,
            'outcomes': outcomes, 'lowest_experience': lowest[0], 'lowest_is_provisional': lowest[1],
            'slowest_phase_completion_p95_ms': max(times) if times else None,
            'latest': {'name': clean(latest.get('name')), 'ended_at': latest.get('ended_at'),
                       'execution': results[-1]['execution'], 'recovery': results[-1]['recovery'],
                       'outcome': latest.get('experience_result') if latest.get('experience_result') in outcomes else 'unknown',
                       'conclusion': [clean(line) for line in (latest.get('narrative') or latest.get('conclusion') or [])[:2]]}
                      if latest else None}


def briefing(scenario, targets, window_seconds=15):
    targets = targets or {}
    def target(key, sign, suffix):
        value = targets.get(key)
        return f' (target {sign} {value:g}{suffix})' if number(value) else ''
    measurements = [
        'Experience score' + target('experience_min', '≥', '/100'),
        'Request success' + target('success_min_pct', '≥', '%'),
        'Interactive application completion P95' + target('interactive_p95_max_ms', '≤', ' ms'),
        'WAN download/upload and where the appliance sends traffic',
        'Target-agent round-trip delay (RTD/RTT), when supported',
    ]
    if number(targets.get('steering_max_s')):
        measurements.append(f"Traffic steering time (target ≤ {targets['steering_max_s']:g} s)")
    steps = scenario.get('steps') or []
    impairment = any(
        (step.get('action') == 'fault' and step.get('value') != 'normal') or
        (step.get('action') == 'quality' and number(step.get('value')) and step['value'] < 100) or
        step.get('action') in ('profile', 'packet') or
        (step.get('action') == 'mtu' and number(step.get('value')) and step['value'] > 0)
        for step in steps)
    recovery = any(step.get('action') == 'fault' and step.get('value') == 'normal' or
                   step.get('phase') == 'Recovery' for step in steps)
    watch = ['Watch application scores, failures and completion time together; a healthy WAN alone does not prove good user experience.']
    if impairment:
        watch.append('During impairment, look for application impact and traffic moving to a healthy WAN. Successful steering may keep users unaffected.')
    else:
        watch.append('Use this test to establish performance and compare it with its configured checks.')
    if recovery:
        watch.append('In recovery, look for applications returning toward baseline; recovery is confirmed by measurements.')
    watch.append(f'Live results use the selected {window_seconds:g}-second window within each phase. Limited evidence is labelled; the final report retains the full phase.')
    return {'measurements': measurements, 'watch': watch}
