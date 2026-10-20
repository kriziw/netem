"""Presentation-only session facts and test briefings; never invent a target pass."""
import math


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def application_impact(dem):
    """Explain measured target misses without inventing human symptoms or causes."""
    from experience_presentation import INVALID_EVIDENCE
    if dem.get('truncated') or INVALID_EVIDENCE.intersection(dem.get('evidence_issues') or []):
        return []
    effects = {}
    for app in (dem.get('applications') or {}).values():
        if not app.get('requests') or app.get('scoring_complete') is not True or app.get('evidence_issues'):
            continue
        misses = set(app.get('target_violations') or [])
        kind = app.get('class') or 'interactive'
        message = None
        if kind == 'realtime':
            if misses.intersection(('loss', 'jitter', 'deadline')):
                message = 'Voice or video checks lost packets or missed timing targets; calls may break up.'
            elif 'success' in misses:
                message = 'Some voice or video checks failed; users may lose a call or meeting.'
        elif kind == 'bulk':
            if 'success' in misses:
                message = 'Some file transfers failed; users may need to restart a transfer.'
            elif 'deadline' in misses or any(key.startswith(('throughput_', 'sustained_')) for key in misses):
                message = 'File transfers were slower than required; uploads and downloads may take longer.'
        elif 'success' in misses:
            message = 'Some business requests failed; users may need to retry a task.'
        elif 'deadline' in misses:
            message = 'Business applications responded slowly; users may wait longer for a task to finish.'
        priority = 2 if 'success' in misses else 1
        if message and (kind not in effects or priority > effects[kind][0]):
            effects[kind] = (priority, message)
    return [effects[kind][1] for kind in ('realtime', 'interactive', 'bulk') if kind in effects]


USER_GROUPS = (
    ('realtime', 'Calls & meetings', 'Calls and meetings are performing within their measured targets.'),
    ('interactive', 'Business tasks', 'Business tasks are completing within their measured targets.'),
    ('bulk', 'Files & backups', 'Uploads and downloads are completing within their measured targets.'),
)


def user_effects(dem):
    """Three customer implications, derived from application evidence, never WAN settings."""
    from experience_presentation import INVALID_EVIDENCE
    invalid = dem.get('truncated') or INVALID_EVIDENCE.intersection(dem.get('evidence_issues') or [])
    uncertain = bool(dem.get('at_risk') or dem.get('evidence_issues'))
    result = []
    for key, label, healthy in USER_GROUPS:
        apps = {name: item for name, item in (dem.get('applications') or {}).items()
                if (item.get('class') or 'interactive') == key}
        messages = application_impact({**dem, 'applications': apps}) if not invalid and not dem.get('live_paused') else []
        if dem.get('live_paused'):
            state, text = 'unknown', 'Test paused; current user impact is not being measured.'
        elif invalid:
            state, text = 'unknown', 'Measurements need attention; current user impact cannot be confirmed.'
        elif messages:
            state, text = 'bad', messages[0]
        elif not uncertain and apps and all(item.get('requests') and item.get('scoring_complete') is True
                          and not item.get('evidence_issues') and not item.get('target_observations')
                          and not item.get('target_violations') for item in apps.values()):
            state, text = 'good', healthy
        else:
            state, text = 'unknown', 'Still measuring; the effect on users is not yet confirmed.'
        result.append(dict(key=key, label=label, state=state, text=text))
    return result


def observed_impact(summary):
    messages = list(dict.fromkeys(line for phase in summary.get('phases') or []
                                 if phase.get('reached') is not False for line in phase.get('user_impact') or []))
    return ' '.join(messages[:3])


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
        result = result if result in ('passed', 'failed', 'stopped', 'interrupted') else 'unknown'
        if result in ('passed', 'failed', 'stopped', 'interrupted'):
            execution[{'passed': 'completed', 'failed': 'attention', 'interrupted': 'attention', 'stopped': 'stopped'}[result]] += 1
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
                        'unmet_requirements': unmet, 'user_impact': clean(observed_impact(summary)),
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
                       'user_impact': results[-1]['user_impact'],
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

