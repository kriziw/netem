"""Compatibility and customer presentation for continuous application checks."""
CAPABILITY = 'continuous-application-checks-v1'
SMALL_SITE_USERS = 25


def start_payload(payload, catalog):
    result = dict(payload)
    supported = CAPABILITY in (catalog or {}).get('engine_capabilities', [])
    if not supported:
        result.pop('application_checks', None)
        return result
    try:
        small_site = 1 <= float(result.get('users', 50)) <= SMALL_SITE_USERS
    except (TypeError, ValueError):
        small_site = False
    rate_mode = result.get('generation_mode') == 'rate' or any(result.get(key) for key in
                ('paced_mbps', 'rate_down_mbps', 'rate_up_mbps'))
    if small_site and not rate_mode:
        result.setdefault('application_checks', True)
    return result


def presentation(run, dem, now):
    enabled = run.get('application_checks') is True
    apps = dem.get('applications') or {}
    expected = [app for app, share in (run.get('manifest') or {}).get('application_shares', {}).items() if share > 0]
    measured = sum(bool((apps.get(app) or {}).get('requests')) for app in expected)
    started = run.get('started_at')
    age = max(0, now - started) if isinstance(started, (int, float)) else 30
    failed = dem.get('business_acceptance') == 'fail'
    preparing = bool(enabled and age < 20 and dem.get('scoring_complete') is not True and not failed)
    sources = dem.get('measurement_sources') or {}
    return {'enabled': enabled, 'preparing': preparing, 'measured_apps': measured, 'expected_apps': len(expected),
            'checks': sources.get('application_checks', 0), 'user_transactions': sources.get('user_transactions'),
            'check_bytes_down': sources.get('check_bytes_down', 0), 'check_bytes_up': sources.get('check_bytes_up', 0),
            'worker_errors': len(run.get('application_check_errors') or {}),
            'basis': 'User activity and continuous application checks' if enabled else 'User activity'}
