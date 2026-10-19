"""FortiGate discovery, configuration contracts and safe provisioning boundaries."""
import copy
import json
import sqlite3
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app as netem
import access_control
from appliance_inventory import APIError, Store, validate
import fortinet_lab as lab


ROW = dict(id='a'*32, revision=1, vendor='fortinet', endpoint='https://192.0.2.10',
           vdom='root', self_signed=False, fingerprint='', name='Lab FortiGate')


def fixture():
    result = {table: [] for table in lab.TABLES}
    result['system/interface'] = [dict(name='port1', type='physical', vdom='root', role='undefined', mode='static',
        ip='192.0.2.10 255.255.255.0', allowaccess='https ssh ping', status='up')]
    for n, role, ip in [(2, 'lan', '10.250.10.1 255.255.255.0'), (3, 'wan', '10.10.1.2 255.255.255.252'),
                        (4, 'wan', '10.10.2.2 255.255.255.252'), (5, 'wan', '10.10.3.2 255.255.255.252')]:
        result['system/interface'].append(dict(name=f'port{n}', type='physical', vdom='root', role=role,
            mode='static', ip=ip, allowaccess='ping', status='up'))
    result['system/sdwan'] = dict(status='disable', zone=[{'name': 'virtual-wan-link'}], members=[], service=[], **{'health-check': []})
    result['system/settings'] = {'central-nat': 'disable'}
    result['identity'] = dict(hostname='lab', model='FortiGate-VM64', serial='FGT123', version='v7.6.7')
    return result


def spec(count=2):
    return {'management_interface': 'port1', 'lan': {'interface': 'port2', 'address': '10.250.10.1/24'},
        'wans': [{'interface': f'port{i+3}', 'address': f'10.10.{i+1}.2/30', 'gateway': f'10.10.{i+1}.1'} for i in range(count)],
        'destination': '198.18.0.0/15', 'health_server': '198.18.0.1', 'nat': True}


class FakeClient:
    def __init__(self, snapshot=None):
        self.data = copy.deepcopy(snapshot or fixture())
        self.writes = []
        self.fail_at = None
        self.ignore = None

    def get(self, path):
        return {'status': 'success', 'version': self.data['identity']['version'], 'results': self.data['identity']}

    def cmdb(self, table, method='GET', key=None, payload=None):
        if method == 'GET':
            return copy.deepcopy(self.data[table])
        self.writes.append((table, method, key, copy.deepcopy(payload)))
        if self.fail_at == len(self.writes):
            raise APIError('unreachable', 'Management API could not be reached.')
        if self.ignore == table:
            return {'status': 'success'}
        if table == 'system/sdwan':
            self.data[table].update(copy.deepcopy(payload))
        else:
            field = {'system/interface': 'name', 'firewall/address': 'name', 'router/static': 'seq-num', 'firewall/policy': 'policyid'}[table]
            existing = next((x for x in self.data[table] if str(x.get(field)) == str(payload[field])), None)
            if existing is None:
                self.data[table].append(copy.deepcopy(payload))
            else:
                existing.update(copy.deepcopy(payload))
        return {'status': 'success'}


class PlanTests(unittest.TestCase):
    def test_one_two_three_wan_cmdb_payloads(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                plan = lab.build_plan(spec(count), fixture(), ROW)
                sdwan = plan['_sdwan_payload']
                self.assertEqual(len(sdwan['members']), count)
                self.assertEqual(sdwan['status'], 'enable')
                self.assertTrue(all(1 <= m['seq-num'] <= 512 for m in sdwan['members']))
                self.assertTrue(1 <= sdwan['service'][0]['id'] <= 4000)
                self.assertEqual([m['interface'] for m in sdwan['members']], [f'port{i+3}' for i in range(count)])
                self.assertEqual(sdwan['service'][0]['priority-members'], [{'seq-num': m['seq-num']} for m in sdwan['members']])
                self.assertEqual(sdwan['service'][0]['mode'], 'sla')
                self.assertEqual(sdwan['health-check'][0]['server'], '198.18.0.1')
                self.assertEqual(sdwan['health-check'][0]['sla'][0]['packetloss-threshold'], 2)
                self.assertEqual(plan['objects']['router/static'][0]['dst'], '198.18.0.0 255.254.0.0')
                self.assertEqual(plan['objects']['firewall/policy'][0]['nat'], 'enable')
                self.assertFalse(any(a['key'] == 'port1' for a in plan['actions']))

    def test_discovery_suggests_management_addresses_and_gateways(self):
        snapshot = fixture()
        snapshot['router/static'] = [{'seq-num': 1, 'device': 'port3', 'gateway': '10.10.1.1', 'dst': '0.0.0.0 0.0.0.0'}]
        public = lab.public_discovery(snapshot, ROW)
        self.assertEqual(public['management_interface'], 'port1')
        port = next(p for p in public['interfaces'] if p['name'] == 'port3')
        self.assertEqual((port['cidr'], port['gateway']), ('10.10.1.2/30', '10.10.1.1'))
        self.assertNotIn('private-key', json.dumps(public))
        self.assertEqual(lab.discover(FakeClient(snapshot))['identity']['version'], 'v7.6.7')

    def test_discovery_does_not_expose_unrecognized_device_fields(self):
        snapshot = fixture()
        snapshot['system/interface'][0]['password'] = 'device-secret'
        snapshot['firewall/policy'] = [{'password': 'policy-secret'}]
        self.assertNotIn('secret', json.dumps(lab.public_discovery(snapshot, ROW)))

    def test_rejects_unsupported_firmware(self):
        for version in ('v6.4.15', 'v8.0.0', '', 'something'):
            client = FakeClient(); client.data['identity']['version'] = version
            with self.assertRaises(ValueError):
                lab.discover(client)

    def test_preserves_unrelated_sdwan_objects_and_allocates_free_ids(self):
        snapshot = fixture()
        snapshot['system/sdwan'].update(status='enable', members=[{'seq-num': 1, 'interface': 'unused', 'zone': 'production', 'unknown-field': 'preserve'}],
            zone=[{'name': 'production', 'unknown-field': 'preserve'}], service=[{'id': 1, 'name': 'existing'}])
        snapshot['router/static'] = [{'seq-num': 9000, 'device': 'unused'}]
        snapshot['firewall/policy'] = [{'policyid': 9000, 'srcintf': [{'name': 'unused'}]}]
        original = copy.deepcopy(snapshot)
        plan = lab.build_plan(spec(), snapshot, ROW)
        self.assertEqual(snapshot, original)
        self.assertEqual(plan['_sdwan_payload']['members'][0], snapshot['system/sdwan']['members'][0])
        self.assertEqual(plan['_sdwan_payload']['members'][1]['seq-num'], 2)
        self.assertEqual(plan['objects']['router/static'][0]['seq-num'], 9001)
        self.assertNotIn('unknown-field', json.dumps(lab.Manager.public_plan(plan)))

    def test_repeated_setup_is_idempotent_and_supports_adding_and_removing_wans(self):
        client = FakeClient()
        first = lab.build_plan(spec(1), client.data, ROW)
        for action in first['actions']:
            payload = first['_sdwan_payload'] if action['table'] == 'system/sdwan' else action['after']
            client.cmdb(action['table'], action['method'], action['key'], payload)
        owned = {'objects': first['objects']}
        repeated = lab.build_plan(spec(1), client.data, ROW, owned)
        self.assertEqual(repeated['actions'], [])
        added = lab.build_plan(spec(3), client.data, ROW, owned)
        self.assertEqual(len(added['_sdwan_payload']['members']), 3)
        for action in added['actions']:
            payload = added['_sdwan_payload'] if action['table'] == 'system/sdwan' else action['after']
            client.cmdb(action['table'], action['method'], action['key'], payload)
        reduced = lab.build_plan(spec(2), client.data, ROW, {'objects': added['objects']})
        self.assertEqual(len(reduced['_sdwan_payload']['members']), 2)
        self.assertTrue(any('retain' in w for w in reduced['warnings']))

    def test_manual_changes_to_owned_objects_are_not_overwritten(self):
        client = FakeClient()
        plan = lab.build_plan(spec(), client.data, ROW)
        for action in plan['actions']:
            client.cmdb(action['table'], action['method'], action['key'], plan['_sdwan_payload'] if action['table'] == 'system/sdwan' else action['after'])
        client.data['firewall/address'][0]['subnet'] = '10.99.0.0 255.255.0.0'
        with self.assertRaisesRegex(ValueError, 'changed outside'):
            lab.build_plan(spec(), client.data, ROW, {'objects': plan['objects']})

    def test_invalid_topology_refused_without_writes(self):
        variants = []
        def change(key, value):
            data = spec(); data[key] = value; variants.append(data)
        change('management_interface', 'missing')
        change('management_interface', 'port2')
        change('wans', [])
        change('wans', spec(3)['wans'] + [spec(1)['wans'][0]])
        change('wans', [spec()['wans'][0], spec()['wans'][0]])
        change('wans', [dict(spec()['wans'][0], gateway='10.10.1.2')])
        change('wans', [dict(spec()['wans'][0], gateway='10.11.1.1')])
        change('wans', [dict(spec()['wans'][0], address='10.10.1.0/30')])
        change('lan', dict(interface='port2', address='10.10.1.1/24'))
        change('lan', dict(interface='port1', address='192.0.2.10/24'))
        change('lan', dict(interface='port2', address='192.0.2.11/24'))
        change('destination', '0.0.0.0/0')
        change('destination', '10.250.10.0/24')
        change('destination', '198.18.0.1/15')
        change('health_server', '8.8.8.8')
        change('health_server', '198.18.0.0')
        change('nat', 'enable')
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError):
                lab.build_plan(data, fixture(), ROW)

    def test_existing_dependencies_central_nat_and_name_collisions_are_blocked(self):
        mutations = [
            ('system/zone', [{'name': 'lan', 'interface': [{'interface-name': 'port2'}]}]),
            ('system/switch-interface', [{'name': 'switch', 'member': [{'interface-name': 'port3'}]}]),
            ('system/virtual-switch', [{'name': 'switch', 'port': [{'name': 'port4'}]}]),
            ('system.dhcp/server', [{'id': 1, 'interface': 'port2'}]),
            ('router/policy', [{'seq-num': 1, 'input-device': 'port2'}]),
            ('router/static', [{'seq-num': 1, 'device': 'port3'}]),
            ('firewall/policy', [{'policyid': 1, 'dstintf': [{'name': 'port4'}]}]),
            ('system/settings', {'central-nat': 'enable'}),
            ('firewall/address', [{'name': 'netem-aaaaaaaa-lan'}]),
        ]
        for table, content in mutations:
            snapshot = fixture(); snapshot[table] = content
            with self.subTest(table=table), self.assertRaises(ValueError):
                lab.build_plan(spec(), snapshot, ROW)

    def test_unmanaged_sdwan_members_are_not_adopted(self):
        snapshot = fixture(); snapshot['system/sdwan']['members'] = [{'seq-num': 1, 'interface': 'port3'}]
        with self.assertRaisesRegex(ValueError, 'managed outside'):
            lab.build_plan(spec(), snapshot, ROW)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        definition = validate(dict(ROW, kind='vm', target='198.18.0.1'), netem.SDWAN_VENDORS)
        self.id = self.store.save(definition, token='private-token')
        self.row = self.store.get(self.id)
        self.client = FakeClient()
        self.netem = SimpleNamespace(APPLIANCE_PLATFORM=SimpleNamespace(store=self.store),
            STORAGE_ADMIN=SimpleNamespace(idle=lambda: True, running=lambda: False),
            traffic_generator_config=lambda: {}, log_event=Mock(), app=netem.app)
        self.manager = lab.Manager(self.netem, lambda row, token: self.client)
        self.manager.run_discovery(self.row, self.client)

    def preview(self, count=2):
        self.manager.preview(self.id, spec(count))
        return copy.deepcopy(self.manager.plan)

    def test_apply_and_readback_preserve_management_and_unrelated_config(self):
        plan = self.preview(3)
        management = copy.deepcopy(self.client.data['system/interface'][0])
        self.manager.run_apply(self.row, self.client, plan)
        self.assertEqual(self.manager.status()['state'], 'completed')
        self.assertEqual(self.client.data['system/interface'][0], management)
        self.assertEqual(len(self.client.data['system/sdwan']['members']), 3)
        self.assertTrue(self.store.state('fortinet_lab_owned:' + self.id)['objects'])
        self.assertNotIn('private-token', json.dumps(self.manager.status()))
        self.assertNotIn('private-token', json.dumps(self.store.state('fortinet_lab_recovery:' + self.id)))
        self.netem.log_event.assert_called_once()

    def test_change_between_preview_and_apply_causes_zero_writes(self):
        plan = self.preview()
        self.client.data['firewall/policy'].append({'policyid': 123, 'name': 'added-after-review'})
        self.manager.run_apply(self.row, self.client, plan)
        self.assertEqual(self.manager.status()['state'], 'failed')
        self.assertIn('changed since discovery', self.manager.status()['message'])
        self.assertEqual(self.client.writes, [])

    def test_timeout_never_retries_and_journal_identifies_uncertain_step(self):
        plan = self.preview()
        self.client.fail_at = 2
        self.manager.run_apply(self.row, self.client, plan)
        result = self.manager.status()
        self.assertEqual(result['state'], 'needs_review')
        self.assertEqual(len(self.client.writes), 2)
        self.assertEqual([x['state'] for x in result['steps']], ['acknowledged', 'sending'])
        self.assertIn('may have applied', result['message'])
        self.assertTrue(self.store.state('fortinet_lab_recovery:' + self.id)['actions'])

    def test_readback_mismatch_is_not_success(self):
        plan = self.preview()
        self.client.ignore = 'router/static'
        self.manager.run_apply(self.row, self.client, plan)
        self.assertEqual(self.manager.status()['state'], 'needs_review')
        self.assertFalse(self.store.state('fortinet_lab_owned:' + self.id))

    def test_saved_job_reports_interruption_after_restart(self):
        self.store.state('fortinet_lab_job', {'state': 'applying', 'steps': [{'table': 'router/static', 'state': 'sending'}]})
        restarted = lab.Manager(self.netem)
        self.assertEqual(restarted.status()['state'], 'interrupted')
        self.assertIsNone(restarted.status()['plan'])
        self.assertEqual(restarted.status()['steps'][0]['state'], 'sending')

    def test_preview_is_bound_to_revision_credentials_and_expiry(self):
        self.manager.snapshot['at'] -= 901
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.preview()
        self.manager.run_discovery(self.row, self.client)
        self.preview()
        self.store.save(validate(dict(ROW, kind='vm', target='198.18.0.1'), netem.SDWAN_VENDORS), self.id, token='replacement')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.manager.start_apply(self.id, self.manager.plan['id'])
        self.assertEqual(self.client.writes, [])

    def test_busy_lab_and_duplicate_approval_are_refused(self):
        plan = self.preview()
        self.netem.STORAGE_ADMIN.idle = lambda: False
        with self.assertRaisesRegex(ValueError, 'Finish'):
            self.manager.start_apply(self.id, plan['id'])
        self.netem.STORAGE_ADMIN.idle = lambda: True
        with patch('fortinet_lab.threading.Thread') as worker:
            self.manager.start_apply(self.id, plan['id'])
            self.assertIsNone(self.manager.plan)
            worker.return_value.start.assert_called_once()
            with self.assertRaises(ValueError):
                self.manager.start_apply(self.id, plan['id'])

    def test_discovery_failure_is_sanitized_and_does_not_change_device(self):
        self.client.get = Mock(side_effect=APIError('auth_error', 'API access denied.'))
        self.manager.run_discovery(self.row, self.client)
        self.assertEqual(self.manager.status()['state'], 'failed')
        self.assertEqual(self.client.writes, [])

    def test_unwritable_journal_refuses_admission_without_launching_worker(self):
        with patch.object(self.store, 'state', side_effect=sqlite3.OperationalError('disk is full')), patch('fortinet_lab.threading.Thread') as worker:
            with self.assertRaisesRegex(ValueError, 'Check NetEm storage'):
                self.manager.start('applying', lambda: None, self.id)
            worker.assert_not_called()
        self.assertFalse(self.manager.busy())
        self.assertEqual(self.client.writes, [])

    def test_journal_failure_stops_before_next_device_write(self):
        plan = self.preview()
        original = self.store.state
        count = 0
        def save(key, value=None):
            nonlocal count
            if key == 'fortinet_lab_job' and value is not None:
                count += 1
                if count >= 3:
                    raise sqlite3.OperationalError('disk is full')
            return original(key, value)
        with patch.object(self.store, 'state', side_effect=save), self.assertLogs(netem.app.logger, 'ERROR'):
            self.manager.run_apply(self.row, self.client, plan)
        self.assertEqual(len(self.client.writes), 1)
        self.assertEqual(self.manager.status()['state'], 'needs_review')
        self.assertIn('storage', self.manager.status()['message'])
        self.assertFalse(self.manager.busy())


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.flags = patch.dict(netem.app.config, NETEM_AUTH_REQUIRED=False, SESSION_COOKIE_SECURE=False)
        self.flags.start(); self.addCleanup(self.flags.stop)
        patcher = patch.object(netem.FORTINET_LAB, 'store', Store(self.tmp.name)); patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(netem.ACCESS_STORE, 'path', Path(self.tmp.name)/'access.db'); patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(netem.FORTINET_LAB, 'job', {'state': 'idle', 'message': 'Ready'}); patcher.start(); self.addCleanup(patcher.stop)
        self.client = netem.app.test_client()
        self.id = netem.ACCESS_STORE.create('admin', 'administrator-password', 'admin')
        self.login(self.id)

    def login(self, user_id):
        user = netem.ACCESS_STORE.get(user_id)
        with self.client.session_transaction() as state:
            state['access_identity'] = {'id': user_id, 'version': user['version'], 'at': time.time()}
            state['integration_csrf'] = 'test-token'

    def test_admin_page_status_and_route_permissions(self):
        self.assertEqual(self.client.get('/integrations/fortinet').status_code, 200)
        self.assertEqual(self.client.get('/api/v1/integrations/fortinet').status_code, 200)
        for role in ('viewer', 'presenter', 'operator'):
            self.login(netem.ACCESS_STORE.create('user-' + role, 'sample-user-password', role))
            for path in ('/integrations/fortinet', '/api/v1/integrations/fortinet', '/integrations/fortinet/x/recovery.json'):
                self.assertEqual(self.client.get(path).status_code, 403)
            for suffix in ('connection', 'discover', 'preview', 'apply'):
                self.assertEqual(self.client.post('/integrations/fortinet/' + suffix, data={'integration_csrf': 'test-token'}).status_code, 403)

    def test_explicit_csrf_and_apply_confirmation(self):
        self.assertEqual(self.client.post('/integrations/fortinet/connection').status_code, 400)
        self.assertEqual(self.client.post('/integrations/fortinet/discover').status_code, 400)
        self.assertEqual(self.client.post('/integrations/fortinet/apply', data={'integration_csrf': 'test-token'}).status_code, 400)
        self.assertEqual(self.client.post('/integrations/fortinet/preview', data={'integration_csrf': 'test-token', 'configuration': '[]'}).status_code, 409)

    def test_pending_operation_blocks_lab_starts_and_inventory_edits(self):
        with patch.object(netem.FORTINET_LAB, 'job', {'state': 'applying'}):
            for path in ('/sessions/start', '/appliances/save', '/settings/storage/compact'):
                self.assertEqual(self.client.post(path, data={'integration_csrf': 'test-token'}).status_code, 409)
        self.assertEqual(access_control.required_role('fortinet_status'), 'admin')

    def test_guided_connection_create_keep_token_and_change_origin(self):
        payload = dict(name='Guided FortiGate', endpoint='https://192.0.2.10', vdom='root', kind='vm',
                       api_token='private-guided-token', integration_csrf='test-token')
        with patch.object(netem.FORTINET_LAB, 'idle'), patch.object(netem, 'log_event'):
            response = self.client.post('/integrations/fortinet/connection', data=payload)
            self.assertEqual(response.status_code, 200)
            appliance_id = response.json['appliance_id']
            self.assertEqual(netem.FORTINET_LAB.store.token(appliance_id), 'private-guided-token')
            self.assertNotIn('private-guided-token', response.text)
            payload.update(id=appliance_id, api_token='')
            self.assertEqual(self.client.post('/integrations/fortinet/connection', data=payload).status_code, 200)
            self.assertEqual(netem.FORTINET_LAB.store.token(appliance_id), 'private-guided-token')
            payload['endpoint'] = 'https://192.0.2.11'
            response = self.client.post('/integrations/fortinet/connection', data=payload)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(netem.FORTINET_LAB.store.get(appliance_id)['endpoint'], 'https://192.0.2.10')
            payload['api_token'] = 'replacement-private-token'
            self.assertEqual(self.client.post('/integrations/fortinet/connection', data=payload).status_code, 200)
            self.assertEqual(netem.FORTINET_LAB.store.token(appliance_id), 'replacement-private-token')
            html = self.client.get('/integrations/fortinet').text
            self.assertIn('data-has-token="1"', html)
            self.assertNotIn('replacement-private-token', html)

    def test_guided_connection_requires_token_and_verified_self_signed_trust(self):
        payload = dict(name='Guided FortiGate', endpoint='https://192.0.2.10', vdom='root', integration_csrf='test-token')
        with patch.object(netem.FORTINET_LAB, 'idle'):
            self.assertEqual(self.client.post('/integrations/fortinet/connection', data=payload).status_code, 409)
            payload.update(api_token='private-token', self_signed='1')
            self.assertEqual(self.client.post('/integrations/fortinet/connection', data=payload).status_code, 409)
        self.assertEqual(netem.FORTINET_LAB.store.rows(), [])

    def test_authenticated_discover_preview_apply_flow_uses_real_handlers(self):
        manager = netem.FORTINET_LAB
        device_id = manager.store.save(validate(dict(ROW, kind='vm', target='198.18.0.1'), netem.SDWAN_VENDORS), token='private-token')
        remote = FakeClient()
        def inline(*args, **kwargs):
            return SimpleNamespace(start=kwargs['target'])
        with patch.object(manager, 'client_factory', return_value=remote), patch.object(manager, 'snapshot', None), \
                patch.object(manager, 'plan', None), patch.object(manager, 'idle'), \
                patch('fortinet_lab.threading.Thread', side_effect=inline), patch.object(netem, 'log_event'):
            response = self.client.post('/integrations/fortinet/discover', data={'id': device_id, 'integration_csrf': 'test-token'})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json['state'], 'discovered')
            self.assertEqual(remote.writes, [])
            response = self.client.post('/integrations/fortinet/preview', data={'id': device_id, 'integration_csrf': 'test-token', 'configuration': json.dumps(spec(3))})
            self.assertEqual(response.status_code, 200)
            plan_id = response.json['plan']['id']
            self.assertEqual(remote.writes, [])
            response = self.client.post('/integrations/fortinet/apply', data={'id': device_id, 'integration_csrf': 'test-token', 'plan_id': plan_id, 'confirmation': 'apply'})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json['state'], 'completed')
            self.assertEqual(len(remote.data['system/sdwan']['members']), 3)
            response = self.client.get(f'/integrations/fortinet/{device_id}/recovery.json')
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('private-token', response.text)


class TransportTests(unittest.TestCase):
    def test_cmdb_uses_existing_pinned_transport_and_encodes_keys(self):
        client = lab.Client(ROW, 'token')
        with patch.object(client, '_request', return_value={'status': 'success', 'results': []}) as request:
            client.cmdb('system/interface', 'PUT', 'port 2', {'ip': '10.1.1.1 255.255.255.0'})
            self.assertEqual(request.call_args.args[:2], ('PUT', '/api/v2/cmdb/system/interface/port%202'))
            for args in [('system/api-user', 'POST'), ('system/interface', 'POST'), ('firewall/policy', 'DELETE')]:
                with self.assertRaises(ValueError):
                    client.cmdb(*args)

    def test_truncation_and_bad_cmdb_shapes_are_refused(self):
        client = lab.Client(ROW, 'token')
        for value in ({'results': [], 'limit_reached': True}, {'results': {'unexpected': True}}, {'results': None}):
            with patch.object(client, '_request', return_value=value), self.assertRaises(ValueError):
                client.cmdb('system/interface')

    def test_end_cursor_is_not_mistaken_for_a_truncated_response(self):
        client = lab.Client(ROW, 'token')
        with patch.object(client, '_request', return_value={'results': [{'name': 'port1'}], 'limit_reached': False, 'next_idx': 1}):
            self.assertEqual(client.cmdb('system/interface'), [{'name': 'port1'}])

    def test_missing_hardware_switch_support_is_optional_but_permission_failure_is_not(self):
        client = FakeClient()
        read = client.cmdb
        def unsupported(table):
            if table == 'system/virtual-switch':
                raise APIError('unsupported', 'Endpoint is not available.')
            return read(table)
        with patch.object(client, 'cmdb', side_effect=unsupported):
            self.assertEqual(lab.discover(client)['system/virtual-switch'], [])
        with patch.object(client, 'cmdb', side_effect=APIError('auth_error', 'Access denied.')):
            with self.assertRaises(APIError):
                lab.discover(client)
