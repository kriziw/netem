"""Exercise the platform-to-simulator live context API and exact report queries.
No external endpoints, interfaces or traffic generators are used.
"""
from gevent import monkey
monkey.patch_all()

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, os.environ['SIMULATOR_SOURCE'])


def check():
    with tempfile.TemporaryDirectory() as directory:
        os.environ.update(TRAFFICGEN_CONFIG_DIR=directory+'/config', TRAFFICGEN_RUNTIME_DIR=directory+'/runtime',
                          TRAFFICGEN_DATABASE=directory+'/runtime/traffic.db', TRAFFICGEN_DEM_WORKER='0')
        from trafficgen.app import app as agent
        from trafficgen import database, live_window
        from trafficgen.workload import compile_manifest
        import app as platform
        controller = agent.config['TRAFFICGEN_CONTROLLER']
        now = time.time()
        run = controller._validate_start({'users': 1, 'live_window_seconds': 60})
        run.update(started_at=now-120, status='running')
        run['manifest'] = compile_manifest(run, 1)
        database.create_run(controller.settings.database_path, run)
        database.save_contract(controller.settings.database_path, run, run['started_at'])
        run['live_context'] = live_window.initialize(controller.settings.database_path, run)
        controller.run, controller.runner = run, SimpleNamespace(user_count=1)
        for age in (12, 10, 4, 3):
            database.record_transaction(controller.settings.database_path, dict(run_id=run['run_id'], timestamp=now-age,
                application='web_saas', endpoint_id='web', success=True, response_time_ms=50))
        database.record_operation_events(controller.settings.database_path, [dict(run_id=run['run_id'],
            operation_id=f'probe-{age}', kind='target_timing', timestamp=now-age, started_at=now-age-.01,
            success=True, rtd_ms=value) for age, value in ((12, 1), (3, 20))])
        client = agent.test_client()
        headers = {'Authorization': 'Bearer ' + agent.config['TRAFFICGEN_SETTINGS'].api_key_path.read_text().strip()}
        def api(path, method='GET', payload=None, **kwargs):
            reply = client.post(path, headers=headers, json=payload) if method == 'POST' else client.get(path, headers=headers)
            assert reply.status_code == 200, (path, reply.status_code, reply.json)
            return reply.json
        state = dict(active=True, workload_supervised=True, workload_run_id=run['run_id'], scenario_id='acceptance',
                     started_at=now-30, phase_index=0, phase='Baseline', live_window_seconds=15)
        original_change = live_window.change_context
        boundary = [now-30]
        def change(*args, **kwargs):
            return original_change(*args, **kwargs, now=boundary[0])
        try:
            with patch.dict(platform.SCENARIO_STATE, state, clear=True), \
                 patch.object(platform, 'traffic_generator_request', side_effect=api), \
                 patch.object(live_window, 'change_context', side_effect=change):
                baseline = platform.publish_test_live_context()['dem']
                assert baseline['requests'] == 4 and baseline['configured_window_seconds'] == 15, baseline
                boundary[0] = now-5
                platform.SCENARIO_STATE.update(phase_index=1, phase='Degradation')
                degraded = platform.publish_test_live_context()['dem']
                assert degraded['requests'] == 2 and degraded['live_phase_id'] != baseline['live_phase_id'], degraded
                assert degraded['applications']['web_saas']['experience_score'] == 100
                assert degraded['applications']['web_saas']['sample_confidence'] == 'limited_sample'
                assert not degraded['scoring_complete'] and degraded['business_acceptance'] != 'pass'
                assert degraded['timing']['target_rtd']['p95_ms'] == 20
                assert degraded['timing']['target_rtd']['samples'] == 1
                explicit = api('/api/v1/dem/summary?window=60')
                assert explicit['requests'] == 2 and explicit['since'] == degraded['since'], explicit
                full = platform.phase_window_dem(30, since=now-30, until=now, run_id=run['run_id'])
                assert full['requests'] == 4 and full['since'] == now-30 and full['until'] == now, full
                assert full['timing']['target_rtd']['samples'] == 2
                test_id = 'c' * 32
                platform.test_records.write(directory, test_id, 'configuration.json', {'scenario': {'name': 'Acceptance'}})
                export = platform.test_records.collect_agent(directory, test_id, [run['run_id']], now-30, now, api)
                assert export['complete'], export
                bundle = platform.test_records.archive(directory, test_id)
                with zipfile.ZipFile(bundle) as zipped:
                    transactions = json.loads(zipped.read('agent-0-transactions-000.json'))
                    assert len(transactions['items']) == 4
                    assert 'agent-0-operations-000.json' in zipped.namelist()
                reset = api('/api/v1/controller/heartbeat', method='POST',
                            payload={'run_id': run['run_id'], 'live_context': {'reset': True}})['dem']
                assert reset['configured_window_seconds'] == 60 and reset['live_phase_id'] is None, reset
        finally:
            controller.run['status'] = 'stopped'
            controller.runner = None
            controller.shutdown()
        result = dict(default_live_seconds=15, baseline_requests=baseline['requests'], current_phase_requests=degraded['requests'],
                      full_phase_report_requests=full['requests'], restored_borrowed_window_seconds=reset['configured_window_seconds'])
        (ROOT/'live-window-acceptance-results.json').write_text(json.dumps(result, indent=2))
        print('PASS live-window cross-component API, phase isolation, full report and borrowed-window restoration')


if __name__ == '__main__':
    check()
