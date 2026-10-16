import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
import app as n
import time, tempfile, json
from unittest.mock import patch
from contextlib import ExitStack
with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
    cfg={'mgmt_interface':'lo','wan_links':[{'id':'wan1','inner':'lo','outer':'lo','preset':'dia'}],
         'traffic_generator':{'host':'simulator.invalid','tls_sha256':'pinned'}}
    calls=[]
    def slow(path, **kwargs):
        calls.append(path);time.sleep(.25)
        if path.endswith('/status'):
            return {'status':'idle','run':None,'users':0,'dem':{k:None for k in ('experience_score','availability_pct','p50_ms','p95_ms','interactive_p95_ms','requests_per_second','failures_per_second')}}
        return {'ready':True}
    for name,value in [('load_config',lambda:cfg),('traffic_generator_api_key',lambda:'key'),
                       ('traffic_generator_request',slow)]:
        stack.enter_context(patch.object(n,name,value))
    stack.enter_context(patch.object(n,'CONFIG_PATH',Path(tmp)/'config.json'))
    stack.enter_context(patch.object(n,'TELEMETRY_DB_PATH',Path(tmp)/'telemetry.db'))
    stack.enter_context(patch.object(n.APPLIANCE_PLATFORM,'selected',return_value=None))
    stack.enter_context(patch.object(n,'ACTIVE_SESSION',dict(n.ACTIVE_SESSION,active=True,id='benchmark',started_at=time.time())))
    stack.enter_context(patch.object(n.ACCESS_STORE,'enabled',return_value=False))
    stack.enter_context(patch.dict(n.app.config, NETEM_AUTH_REQUIRED=False))
    results={}
    for path in ['/','/tests','/api/v1/diagnosis','showcase']:
        n.DIAGNOSIS_CACHE.update(payload=None,checked=None)
        start=time.perf_counter()
        client=n.showroom_app.test_client() if path=='showcase' else n.app.test_client()
        r=client.get('/api/snapshot' if path=='showcase' else path)
        results[path]={'ms':round((time.perf_counter()-start)*1000,1),'http':r.status_code}
    print(json.dumps(results))
    if hasattr(n,'PRESENTATION_READS'):
        for _ in range(8):
            with n.PRESENTATION_READS.lock:
                pending=[e['done'] for e in n.PRESENTATION_READS.entries.values() if e['loading']]
            for event in pending:event.wait(3)
    print(json.dumps({'requests':calls}))
