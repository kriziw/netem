"""FortiGate lab provisioning: discover, review, recheck, apply and verify.

Remote configuration is never written by a GET. Plans live in memory for 15 minutes;
only a bounded, credential-free discovery view and operation journal are persisted.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import re
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote, urlsplit

from flask import abort, g, jsonify, render_template, request, session

from appliance_inventory import APIError, FortiGateClient, validate

TABLES = ('system/interface', 'system/sdwan', 'router/static', 'firewall/policy',
          'firewall/address', 'system/zone', 'system/switch-interface',
          'system/virtual-switch', 'system.dhcp/server', 'router/policy', 'system/settings')
WRITE_TABLES = {'system/interface', 'system/sdwan', 'router/static', 'firewall/policy', 'firewall/address'}
INTERFACE_FIELDS = ('name', 'vdom', 'type', 'role', 'mode', 'ip', 'status', 'alias', 'interface',
                    'member', 'allowaccess', 'dedicated-to', 'vrf', 'secondary-IP', 'secondaryip')
SDWAN_KEYS = {'zone': 'name', 'members': 'seq-num', 'health-check': 'name', 'service': 'id'}
TTL = 900


def clean(value):
    """Discard FortiOS response-only origin keys before returning configuration."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k != 'q_origin_key'}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(clean(value), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def subset(actual, wanted):
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(k in actual and subset(actual[k], v) for k, v in wanted.items())
    if isinstance(wanted, list):
        return isinstance(actual, list) and len(actual) == len(wanted) and all(subset(a, w) for a, w in zip(actual, wanted))
    # FortiOS sometimes renders numeric CMDB keys as strings.
    return actual == wanted or (isinstance(wanted, int) and str(actual) == str(wanted))


def single(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        return value[0]
    raise ValueError('FortiGate returned an unexpected singleton configuration.')


class Client(FortiGateClient):
    def cmdb(self, table, method='GET', key=None, payload=None):
        if table not in TABLES or method not in ('GET', 'POST', 'PUT'):
            raise ValueError('Unsupported FortiGate configuration endpoint.')
        if method != 'GET' and (table not in WRITE_TABLES or (table == 'system/interface' and method != 'PUT')):
            raise ValueError('Unsupported FortiGate configuration operation.')
        path = '/api/v2/cmdb/' + table + ('/' + quote(str(key), safe='') if key is not None else '')
        response = self._request(method, path, payload)
        if method != 'GET':
            return response
        result = clean(response.get('results'))
        if table in ('system/sdwan', 'system/settings'):
            return single(result)
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise ValueError('FortiGate returned an unexpected configuration table.')
        # Never silently provision from a partial/paginated response.
        if response.get('limit_reached'):
            raise ValueError('FortiGate discovery was truncated. Reduce the VDOM configuration before provisioning.')
        return result


def discover(client):
    identity = client.get('system/status')
    version = str(identity.get('version') or identity.get('results', {}).get('version') or '')
    if not re.match(r'^v?7\.[246](?:\.|$)', version):
        raise ValueError('Lab provisioning requires FortiOS 7.2, 7.4 or 7.6. Detected firmware: ' + version[:40])
    snapshot = {}
    for table in TABLES:
        try:
            snapshot[table] = client.cmdb(table)
        except APIError as exc:
            # Hardware-switch configuration is absent on some VM/model builds.
            if table != 'system/virtual-switch' or exc.category != 'unsupported':
                raise
            snapshot[table] = []
    snapshot['identity'] = {k: identity.get('results', {}).get(k) for k in ('hostname', 'model', 'serial')}
    snapshot['identity']['version'] = version
    return snapshot


def iface_address(row):
    try:
        return ipaddress.IPv4Interface(str(row.get('ip', '')).replace(' ', '/'))
    except ValueError:
        return None


def public_discovery(snapshot, row):
    interfaces = []
    management = []
    try:
        host = ipaddress.ip_address(urlsplit(row['endpoint']).hostname)
    except ValueError:
        host = None
    members = snapshot['system/sdwan'].get('members', [])
    routes = snapshot['router/static']
    for source in snapshot['system/interface']:
        if source.get('vdom', 'root') != row['vdom']:
            continue
        item = {k: copy.deepcopy(source[k]) for k in INTERFACE_FIELDS if k in source}
        ip = iface_address(source)
        item['cidr'] = str(ip) if ip and not ip.ip.is_unspecified else ''
        if ip and host == ip.ip:
            management.append(source['name'])
        gateways = [x.get('gateway') for x in members + routes if x.get('interface', x.get('device')) == source['name']]
        item['gateway'] = next((gw for gw in gateways if gw and gw != '0.0.0.0'), '')
        item['sdwan_member'] = any(m.get('interface') == source['name'] for m in members)
        interfaces.append(item)
    return {'identity': snapshot['identity'], 'interfaces': interfaces,
            'management_interface': management[0] if len(management) == 1 else '',
            'routes': [{k: r[k] for k in ('seq-num', 'device', 'gateway', 'dst', 'sdwan-zone') if k in r} for r in routes],
            'sdwan': {'status': snapshot['system/sdwan'].get('status'),
                      'zones': [z.get('name') for z in snapshot['system/sdwan'].get('zone', [])],
                      'members': [{k: m[k] for k in ('seq-num', 'interface', 'gateway', 'zone') if k in m} for m in members]},
            'policy_count': len(snapshot['firewall/policy']),
            'policies': [{k: copy.deepcopy(p[k]) for k in ('policyid', 'name', 'srcintf', 'dstintf', 'srcaddr', 'dstaddr', 'action', 'status', 'nat') if k in p}
                         for p in snapshot['firewall/policy']],
            'central_nat': snapshot['system/settings'].get('central-nat', 'disable')}


def ipv4(value):
    ip = ipaddress.IPv4Address(value)
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback or ip.is_link_local or ip == ipaddress.IPv4Address('255.255.255.255'):
        raise ValueError('Use a unicast IPv4 address.')
    return ip


def validate_spec(data, snapshot, row):
    interfaces = {i['name']: i for i in snapshot['system/interface'] if i.get('vdom', 'root') == row['vdom']}
    mgmt = str(data.get('management_interface') or '')
    if mgmt not in interfaces:
        raise ValueError('Confirm the dedicated management interface from discovery.')
    wans = data.get('wans')
    if not isinstance(wans, list) or not 1 <= len(wans) <= 3:
        raise ValueError('Choose one, two or three WAN links.')
    ports = [data.get('lan')] + wans
    normalized, names, networks = [], [], []
    for port in ports:
        if not isinstance(port, dict) or port.get('interface') not in interfaces:
            raise ValueError('Choose interfaces discovered on this VDOM.')
        source = interfaces[port['interface']]
        name = source['name']
        if source.get('type') != 'physical' or source.get('dedicated-to', 'none') not in ('none', ''):
            raise ValueError(f'{name}: only independent physical/VM ports can be provisioned. Detach switch/aggregate members first.')
        if source.get('vrf', 0) not in (0, '0') or source.get('secondary-IP', 'disable') == 'enable':
            raise ValueError(f'{name}: VRFs and secondary addresses require manual setup.')
        address = ipaddress.IPv4Interface(str(port.get('address', '')))
        ipv4(str(address.ip))
        if address.network.prefixlen > 30 or address.ip in (address.network.network_address, address.network.broadcast_address):
            raise ValueError(f'{name}: use a host address with a /1 to /30 subnet.')
        if address.network.prefixlen == 0:
            raise ValueError('Interface addresses cannot use a default-route mask.')
        names.append(name)
        networks.append(address.network)
        normalized.append({'interface': name, 'address': str(address)})
        if len(normalized) > 1:
            gateway = ipv4(str(port.get('gateway', '')))
            if gateway not in address.network or gateway in (address.ip, address.network.network_address, address.network.broadcast_address):
                raise ValueError(f'{name}: gateway must be another host in the WAN subnet.')
            normalized[-1]['gateway'] = str(gateway)
    if len(set(names)) != len(names) or mgmt in names:
        raise ValueError('LAN, WAN and management ports must all be different.')
    if any(a.overlaps(b) for i, a in enumerate(networks) for b in networks[i + 1:]):
        raise ValueError('LAN and WAN subnets must not overlap.')
    try:
        api_ip = ipaddress.ip_address(urlsplit(row['endpoint']).hostname)
    except ValueError:
        api_ip = None
    for name, source in interfaces.items():
        current = iface_address(source)
        if name in names and current and api_ip in (current.ip,):
            raise ValueError('The API management port cannot be changed.')
        if name not in names and current and not current.ip.is_unspecified:
            if any(current.network.overlaps(n) for n in networks):
                raise ValueError(f'Lab subnet overlaps existing interface {name}.')
    if api_ip and any(api_ip in n for n in networks):
        raise ValueError('Lab subnets must not contain the management API address.')
    # WANs already referenced by policies/zones/routes cannot simply become SD-WAN members.
    # Other references are checked too; FortiOS remains the final dependency authority.
    for table in ('system/zone', 'system/switch-interface', 'system/virtual-switch', 'system.dhcp/server', 'router/policy'):
        for entry in snapshot[table]:
            encoded = json.dumps(entry)
            if any(('"' + name + '"') in encoded for name in names):
                raise ValueError(f'An existing {table} entry references a selected port. Resolve it before provisioning.')
    destination = ipaddress.IPv4Network(str(data.get('destination', '')), strict=True)
    if destination.prefixlen == 0 or destination.is_multicast or destination.is_loopback or destination.is_link_local or destination.is_unspecified:
        raise ValueError('Choose a specific routed lab destination subnet, not the default route.')
    if any(destination.overlaps(n) for n in networks):
        raise ValueError('The routed destination must be beyond the WAN gateways, outside connected subnets.')
    management_ip = iface_address(interfaces[mgmt])
    if (management_ip and not management_ip.ip.is_unspecified and destination.overlaps(management_ip.network)) or (api_ip and api_ip in destination):
        raise ValueError('The lab destination must not overlap management addressing.')
    health = ipv4(str(data.get('health_server', '')))
    if health not in destination:
        raise ValueError('The ICMP health-check server must be in the routed destination subnet and reachable through every WAN.')
    if destination.prefixlen < 31 and health in (destination.network_address, destination.broadcast_address):
        raise ValueError('Health-check server must be a destination host address.')
    if not isinstance(data.get('nat'), bool):
        raise ValueError('Choose whether outbound source NAT is required.')
    if snapshot['system/settings'].get('central-nat', 'disable') != 'disable':
        raise ValueError('Central NAT is enabled. This wizard requires policy NAT; configure this VDOM manually.')
    return {'management_interface': mgmt, 'lan': normalized[0], 'wans': normalized[1:],
            'destination': str(destination), 'health_server': str(health), 'nat': data['nat']}


def netmask(cidr):
    address = ipaddress.ip_interface(cidr)
    return f'{address.ip} {address.netmask}'


def free_id(rows, key, start=9000, maximum=65534):
    used = {str(r.get(key)) for r in rows}
    available = next((n for n in range(start, maximum + 1) if str(n) not in used), None)
    if available is None:
        raise ValueError('No free FortiOS configuration IDs remain in the supported range.')
    return available


def build_plan(data, snapshot, row, owned=None):
    spec = validate_spec(data, snapshot, row)
    owned = owned or {}
    prefix = 'netem-' + row['id'][:8]
    sdwan = snapshot['system/sdwan']
    # Check ownership against fields last verified by NetEm, never infer it from a name.
    for table, expected in owned.get('objects', {}).items():
        current = snapshot[table]
        if table == 'system/sdwan':
            for field, key in SDWAN_KEYS.items():
                for old in expected.get(field, []):
                    actual = next((x for x in current.get(field, []) if str(x.get(key)) == str(old[key])), None)
                    if not subset(actual, old):
                        raise ValueError('NetEm-managed SD-WAN configuration changed outside NetEm. Reconcile it before applying.')
        else:
            key = {'system/interface': 'name', 'firewall/address': 'name', 'router/static': 'seq-num', 'firewall/policy': 'policyid'}[table]
            for old in expected:
                actual = next((x for x in current if str(x.get(key)) == str(old[key])), None)
                if not subset(actual, old):
                    raise ValueError('NetEm-managed configuration changed outside NetEm. Reconcile it before applying.')
    wan_names = {p['interface'] for p in spec['wans']}
    own_members = owned.get('objects', {}).get('system/sdwan', {}).get('members', [])
    own_member_ids = {str(m['seq-num']) for m in own_members}
    if any(m.get('interface') == spec['lan']['interface'] for m in sdwan.get('members', [])):
        raise ValueError('The selected LAN is already an SD-WAN member. Choose a separate LAN port.')
    if any(m.get('interface') in wan_names and str(m.get('seq-num')) not in own_member_ids for m in sdwan.get('members', [])):
        raise ValueError('A selected WAN is already an SD-WAN member managed outside NetEm. Choose unused ports.')
    for table, key in (('router/static', 'seq-num'), ('firewall/policy', 'policyid')):
        own_ids = {str(x[key]) for x in owned.get('objects', {}).get(table, [])}
        for item in snapshot[table]:
            if str(item.get(key)) not in own_ids and any(('"' + name + '"') in json.dumps(item) for name in wan_names):
                raise ValueError('A selected WAN is referenced by an existing route or firewall policy. Resolve that reference first.')
    objects, actions = {}, []
    interfaces = {i['name']: i for i in snapshot['system/interface']}
    interface_updates = []
    for index, port in enumerate([spec['lan']] + spec['wans']):
        desired = {'name': port['interface'], 'mode': 'static', 'ip': netmask(port['address']),
                   'role': 'lan' if index == 0 else 'wan', 'status': 'up'}
        interface_updates.append(desired)
        if not subset(interfaces[port['interface']], desired):
            before = {k: interfaces[port['interface']].get(k) for k in desired}
            actions.append({'table': 'system/interface', 'key': desired['name'], 'method': 'PUT', 'before': before, 'after': desired})
    objects['system/interface'] = interface_updates
    lan_network = ipaddress.ip_interface(spec['lan']['address']).network
    dest = ipaddress.ip_network(spec['destination'])
    addresses = [{'name': prefix + '-lan', 'type': 'ipmask', 'subnet': f'{lan_network.network_address} {lan_network.netmask}'},
                 {'name': prefix + '-dst', 'type': 'ipmask', 'subnet': f'{dest.network_address} {dest.netmask}'}]
    objects['firewall/address'] = addresses
    def table_action(table, desired, key):
        current = next((x for x in snapshot[table] if str(x.get(key)) == str(desired[key])), None)
        known = any(str(x.get(key)) == str(desired[key]) for x in owned.get('objects', {}).get(table, []))
        if current and not known:
            raise ValueError(f'A reserved NetEm object already exists in {table}. Resolve the name/ID collision first.')
        if not subset(current, desired):
            actions.append({'table': table, 'key': desired[key] if current else None, 'method': 'PUT' if current else 'POST',
                            'before': {k: current.get(k) for k in desired} if current else None, 'after': desired})
    for address in addresses:
        table_action('firewall/address', address, 'name')
    planned_sdwan = {'zone': [{'name': prefix, 'service-sla-tie-break': 'cfg-order'}], 'members': []}
    used = copy.deepcopy(sdwan.get('members', []))
    for port in spec['wans']:
        old = next((m for m in own_members if m['interface'] == port['interface']), None)
        seq = old['seq-num'] if old else free_id(used, 'seq-num', 1, 512)
        member = {'seq-num': seq, 'interface': port['interface'], 'zone': prefix, 'gateway': port['gateway'], 'status': 'enable', 'priority': 1}
        planned_sdwan['members'].append(member)
        used.append(member)
    member_ids = [{'seq-num': m['seq-num']} for m in planned_sdwan['members']]
    planned_sdwan['health-check'] = [{'name': prefix + '-sla', 'server': spec['health_server'], 'protocol': 'ping',
        'members': member_ids, 'update-static-route': 'enable', 'sla': [{'id': 1, 'link-cost-factor': 'latency jitter packet-loss', 'latency-threshold': 150,
        'jitter-threshold': 30, 'packetloss-threshold': 2}]}]
    old_services = owned.get('objects', {}).get('system/sdwan', {}).get('service', [])
    service_id = old_services[0]['id'] if old_services else free_id(sdwan.get('service', []), 'id', 1, 4000)
    planned_sdwan['service'] = [{'id': service_id, 'name': prefix, 'mode': 'sla', 'status': 'enable',
        'src': [{'name': addresses[0]['name']}], 'dst': [{'name': addresses[1]['name']}],
        'sla': [{'health-check': prefix + '-sla', 'id': 1}], 'priority-members': member_ids, 'tie-break': 'cfg-order'}]
    objects['system/sdwan'] = planned_sdwan
    merged = {'status': 'enable'}
    before_sdwan = {'status': sdwan.get('status', 'disable')}
    for field, key in SDWAN_KEYS.items():
        old = owned.get('objects', {}).get('system/sdwan', {}).get(field, [])
        old_ids = {str(x[key]) for x in old}
        foreign = [x for x in sdwan.get(field, []) if str(x.get(key)) not in old_ids]
        if any(str(x.get(key)) == str(y[key]) for x in foreign for y in planned_sdwan[field]):
            raise ValueError('A reserved NetEm SD-WAN name or ID is already in use.')
        # Full child arrays are required by the FortiOS singleton CMDB endpoint.
        # Preserve every unrelated object, including fields unknown to this adapter.
        replacements = {str(x[key]): x for x in planned_sdwan[field]}
        merged[field] = []
        for current in sdwan.get(field, []):
            identity = str(current.get(key))
            if identity not in old_ids:
                merged[field].append(current)
            elif identity in replacements:
                merged[field].append(dict(current, **replacements.pop(identity)))
        merged[field].extend(replacements.values())
        before_sdwan[field] = old
    def same_sdwan():
        if sdwan.get('status') != 'enable':
            return False
        for field, key in SDWAN_KEYS.items():
            current = sdwan.get(field, [])
            if len(current) != len(merged[field]):
                return False
            if field == 'service' and [str(x[key]) for x in current] != [str(x[key]) for x in merged[field]]:
                return False
            by_id = {str(x[key]): x for x in current}
            if any(not subset(by_id.get(str(x[key])), x) for x in merged[field]):
                return False
        return True
    if not same_sdwan():
        # Public preview contains only owned changes; merged foreign objects remain in memory.
        actions.append({'table': 'system/sdwan', 'key': None, 'method': 'PUT',
                        'before': before_sdwan, 'after': dict(status='enable', **planned_sdwan)})
    for table, key, desired in (
        ('router/static', 'seq-num', {'dst': f'{dest.network_address} {dest.netmask}', 'sdwan-zone': [{'name': prefix}],
                                    'distance': 10, 'status': 'enable', 'comment': prefix}),
        ('firewall/policy', 'policyid', {'name': prefix, 'srcintf': [{'name': spec['lan']['interface']}],
             'dstintf': [{'name': prefix}], 'srcaddr': [{'name': addresses[0]['name']}], 'dstaddr': [{'name': addresses[1]['name']}],
             'action': 'accept', 'schedule': 'always', 'service': [{'name': 'ALL'}], 'nat': 'enable' if spec['nat'] else 'disable',
             'status': 'enable', 'logtraffic': 'all'})):
        prior = owned.get('objects', {}).get(table, [])
        desired[key] = prior[0][key] if prior else free_id(snapshot[table], key)
        objects[table] = [desired]
        table_action(table, desired, key)
    warnings = ['ICMP must be allowed on the target through every WAN. Upstream return routes are required when NAT is disabled.',
                'Existing policy and SD-WAN rule order is preserved. Earlier rules can override this lab rule; review them on the appliance.',
                'Take a FortiGate configuration backup and keep console access before applying. An API timeout can leave partial changes.']
    if snapshot['firewall/policy'] or sdwan.get('service'):
        warnings.append('This appliance has existing rules. The lab policy and SD-WAN rule are appended; conflicting earlier rules require manual reconciliation.')
    old_names = {p['name'] for p in owned.get('objects', {}).get('system/interface', [])}
    if old_names - {p['interface'] for p in [spec['lan']] + spec['wans']}:
        warnings.append('Ports removed from this lab retain their current IP configuration; only NetEm SD-WAN membership is removed.')
    return {'id': uuid.uuid4().hex, 'created_at': time.time(), 'expires_at': time.time() + TTL,
            'appliance_id': row['id'], 'revision': row['revision'], 'snapshot_hash': digest(snapshot),
            'spec': spec, 'actions': actions, 'warnings': warnings, 'objects': objects,
            '_sdwan_payload': merged}


class Manager:
    def __init__(self, netem, client_factory=Client):
        self.netem = netem
        self.store = netem.APPLIANCE_PLATFORM.store
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.snapshot = None
        self.plan = None
        self.job = self.store.state('fortinet_lab_job') or {'state': 'idle', 'message': 'Discover an appliance to begin.'}
        if self.job.get('state') in ('discovering', 'applying'):
            self.job.update(state='interrupted', message='NetEm restarted during the operation. Inspect the FortiGate before creating a fresh preview.')
            self.store.state('fortinet_lab_job', self.job)

    def busy(self):
        with self.lock:
            return self.job['state'] in ('discovering', 'applying')

    def row_client(self, appliance_id):
        row = self.store.get(appliance_id)
        if not row or row['vendor'] != 'fortinet' or not row['endpoint']:
            raise ValueError('Save a Fortinet appliance with an HTTPS API address first.')
        token = self.store.token(appliance_id)
        if not token:
            raise ValueError('Save the appliance API token before discovery.')
        return row, self.client_factory(row, token)

    def status(self):
        with self.lock:
            result = copy.deepcopy(self.job)
            result['discovery'] = copy.deepcopy(self.snapshot['public']) if self.snapshot else None
            result['plan'] = self.public_plan(self.plan) if self.plan else None
            if result.get('state') == 'completed' and result.get('appliance_id'):
                result['applied_spec'] = self.store.state('fortinet_lab_owned:' + result['appliance_id']).get('spec')
            return result

    @staticmethod
    def public_plan(plan):
        return {k: copy.deepcopy(v) for k, v in plan.items() if k not in ('_sdwan_payload', 'objects', 'snapshot_hash')}

    def set_job(self, **values):
        with self.lock:
            self.job.update(values)
            self.store.state('fortinet_lab_job', self.job)

    def finish_job(self, **values):
        try:
            self.set_job(**values)
        except (OSError, sqlite3.Error):
            # The in-memory result must still unblock the UI if the runtime disk filled.
            # No more device writes are attempted after losing the durable journal.
            with self.lock:
                self.job.update(values)
                self.job['message'] += ' The operation journal could not be saved; check NetEm storage.'
            self.netem.app.logger.error('FortiGate operation result could not be persisted')

    def start(self, kind, action, appliance_id):
        with self.lock:
            if self.busy():
                raise ValueError('A FortiGate operation is already running.')
            self.job = {'state': kind, 'appliance_id': appliance_id, 'started_at': time.time(),
                        'message': 'Discovering FortiGate configuration…' if kind == 'discovering' else 'Rechecking the approved plan…',
                        'steps': []}
            try:
                self.store.state('fortinet_lab_job', self.job)
            except (OSError, sqlite3.Error) as exc:
                self.job.update(state='failed', message='Cannot save the operation journal. Check NetEm storage before provisioning.')
                raise ValueError(self.job['message']) from exc
            try:
                threading.Thread(target=action, name='netem-fortinet-lab', daemon=True).start()
            except Exception:
                self.finish_job(state='failed', message='The background operation could not start.')
                raise

    def start_discovery(self, appliance_id):
        row, client = self.row_client(appliance_id)
        with self.lock:
            if self.busy():
                raise ValueError('A FortiGate operation is already running.')
            self.snapshot = self.plan = None
            self.start('discovering', lambda: self.run_discovery(row, client), appliance_id)

    def run_discovery(self, row, client):
        try:
            snapshot = discover(client)
            public = public_discovery(snapshot, row)
            public['saved_spec'] = self.store.state('fortinet_lab_owned:' + row['id']).get('spec')
            public['suggested_target'] = row.get('target', '')
            with self.lock:
                self.snapshot = {'row': row, 'raw': snapshot, 'at': time.time(),
                                 'credential_hash': digest(self.store.token(row['id'])), 'public': public}
            self.set_job(state='discovered', message='Discovery complete. Review the suggested ports and complete missing addresses.')
        except (APIError, ValueError) as exc:
            self.finish_job(state='failed', message=str(exc))
        except Exception:
            self.netem.app.logger.exception('FortiGate discovery failed')
            self.finish_job(state='failed', message='FortiGate discovery failed. Check NetEm service logs.')

    def preview(self, appliance_id, data):
        with self.lock:
            if self.busy() or not self.snapshot or self.snapshot['row']['id'] != appliance_id:
                raise ValueError('Discover this appliance before creating a preview.')
            if time.time() - self.snapshot['at'] > TTL:
                raise ValueError('Discovery expired. Discover the appliance again.')
            row, _client = self.row_client(appliance_id)
            if row['revision'] != self.snapshot['row']['revision'] or digest(self.store.token(appliance_id)) != self.snapshot['credential_hash']:
                raise ValueError('Appliance connection changed. Discover it again.')
            self.plan = build_plan(data, self.snapshot['raw'], row, self.store.state('fortinet_lab_owned:' + appliance_id))
            return self.public_plan(self.plan)

    def idle(self):
        n = self.netem
        if not n.STORAGE_ADMIN.idle() or n.STORAGE_ADMIN.running():
            raise ValueError('Finish the active session, test, capture or database maintenance before provisioning.')
        if self.store.state('pending') or self.store.state('platform').get('network', {}).get('busy'):
            raise ValueError('Finish the pending simulator routing operation before provisioning.')
        if getattr(n, 'CALIBRATION_STATE', {}).get('running'):
            raise ValueError('Finish bandwidth calibration before provisioning.')
        if n.traffic_generator_config().get('host'):
            status = n.traffic_generator_request('/api/v1/status', timeout=4)
            if status.get('status') not in ('idle', 'stopped', 'failed', 'interrupted'):
                raise ValueError('Stop the simulator workload before provisioning.')

    def start_apply(self, appliance_id, plan_id):
        with self.lock:
            if self.busy() or not self.plan or self.plan['id'] != plan_id or self.plan['appliance_id'] != appliance_id:
                raise ValueError('Create and approve a current configuration preview first.')
            if self.plan['expires_at'] < time.time():
                raise ValueError('The preview expired. Discover and preview again.')
            row, client = self.row_client(appliance_id)
            if row['revision'] != self.plan['revision'] or digest(self.store.token(appliance_id)) != self.snapshot['credential_hash']:
                raise ValueError('Appliance connection changed. Discover it again.')
            self.idle()
            plan = copy.deepcopy(self.plan)
            self.plan = None  # Single-use approval, including failed or ambiguous writes.
            self.start('applying', lambda: self.run_apply(row, client, plan), appliance_id)

    def run_apply(self, row, client, plan):
        wrote = False
        try:
            self.idle()
            fresh = discover(client)
            if digest(fresh) != plan['snapshot_hash']:
                raise ValueError('FortiGate configuration changed since discovery. No changes applied; discover and preview again.')
            # Save the reviewed recovery record before the first write. No device secrets.
            recovery = self.public_plan(plan)
            self.store.state('fortinet_lab_recovery:' + row['id'], recovery)
            steps = []
            for action in plan['actions']:
                step = {'table': action['table'], 'key': action['key'], 'state': 'sending'}
                steps.append(step)
                self.set_job(steps=steps, message='Applying ' + action['table'] + '…')
                wrote = True
                payload = plan['_sdwan_payload'] if action['table'] == 'system/sdwan' else action['after']
                client.cmdb(action['table'], action['method'], action['key'], payload)
                step['state'] = 'acknowledged'
                self.set_job(steps=steps)
            verified = discover(client)
            for table, expected in plan['objects'].items():
                if table == 'system/sdwan':
                    if verified[table].get('status') != 'enable':
                        raise ValueError('SD-WAN enable state was not verified.')
                    for field, key in SDWAN_KEYS.items():
                        # Verify preservation as well as our own fields. Rule order matters.
                        all_wanted = plan['_sdwan_payload'][field]
                        if len(verified[table].get(field, [])) != len(all_wanted):
                            raise ValueError('SD-WAN read-back changed the number of configured objects.')
                        if field == 'service' and [x[key] for x in verified[table].get(field, [])] != [x[key] for x in all_wanted]:
                            raise ValueError('SD-WAN rule order did not match the approved configuration.')
                        for wanted in all_wanted:
                            actual = next((x for x in verified[table].get(field, []) if str(x.get(key)) == str(wanted[key])), None)
                            if not subset(actual, wanted):
                                raise ValueError('SD-WAN read-back did not match the approved configuration.')
                else:
                    key = {'system/interface': 'name', 'firewall/address': 'name', 'router/static': 'seq-num', 'firewall/policy': 'policyid'}[table]
                    for wanted in expected:
                        actual = next((x for x in verified[table] if str(x.get(key)) == str(wanted[key])), None)
                        if not subset(actual, wanted):
                            raise ValueError('FortiGate read-back did not match the approved configuration.')
            self.store.state('fortinet_lab_owned:' + row['id'], {'objects': plan['objects'], 'spec': plan['spec']})
            self.netem.log_event('fortinet-lab', 'FortiGate lab configuration applied and verified', appliance_id=row['id'], wan_count=len(plan['spec']['wans']))
            with self.lock:
                self.snapshot = None
            self.finish_job(state='completed', finished_at=time.time(), message='Configuration applied and verified. Use Appliances → Verify & select to test the traffic path; forwarding has not yet been measured.')
        except (APIError, ValueError, RuntimeError) as exc:
            self.finish_job(state='needs_review' if wrote else 'failed', finished_at=time.time(),
                         message=str(exc) + (' Changes may have applied. Inspect the device and recovery record before creating another preview; do not retry blindly.' if wrote else ''))
        except Exception:
            self.netem.app.logger.exception('FortiGate provisioning failed')
            self.finish_job(state='needs_review' if wrote else 'failed', finished_at=time.time(),
                         message='Provisioning was interrupted. Inspect live device configuration and the recovery record before continuing.')


def init_app(app, netem):
    manager = Manager(netem)
    app.extensions['fortinet_lab'] = manager
    mutations = {'fortinet_connection', 'fortinet_discover', 'fortinet_preview', 'fortinet_apply'}
    guarded = mutations | {'session_start', 'lab_scenario_start', 'site_run', 'traffic_generator_start', 'lab_capture_start',
                          'storage_compact', 'inventory_save', 'inventory_archive', 'inventory_select', 'inventory_import',
                          'traffic_path_save', 'traffic_path_select', 'traffic_path_remove', 'platform_self_test', 'platform_calibrate',
                          'traffic_settings_command', 'traffic_generator_save', 'traffic_generator_repair', 'appliance_save',
                          'setup', 'configure', 'quick_wan_action', 'site_apply_wan', 'wan_egress', 'update_components', 'updates'}

    @app.before_request
    def fortinet_admission():
        if request.method != 'POST' or request.endpoint not in guarded:
            return
        if request.endpoint in mutations:
            supplied, expected = request.form.get('integration_csrf', ''), session.get('integration_csrf', '')
            if not supplied or not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
                abort(400, 'Invalid form token. Reload the page and retry.')
        netem.APPLIANCE_PLATFORM.operations.acquire()
        g.fortinet_admission = True
        if manager.busy():
            return jsonify(error='A FortiGate operation is running. Wait for its result before changing the lab.'), 409

    app.before_request_funcs[None].insert(1, app.before_request_funcs[None].pop())

    @app.teardown_request
    def release_fortinet_admission(_error):
        if getattr(g, 'fortinet_admission', False):
            g.fortinet_admission = False
            netem.APPLIANCE_PLATFORM.operations.release()

    @app.get('/integrations/fortinet')
    def fortinet_settings():
        rows = [r for r in manager.store.rows() if r['vendor'] == 'fortinet']
        rows = [dict(r, has_token=bool(manager.store.token(r['id']))) for r in rows]
        return render_template('fortinet.html', page='integrations', appliances=rows, lab=manager.status())

    @app.post('/integrations/fortinet/connection')
    def fortinet_connection():
        try:
            manager.idle()
            appliance_id = request.form.get('id') or None
            old = manager.store.get(appliance_id) if appliance_id else None
            if appliance_id and (not old or old['vendor'] != 'fortinet'):
                raise ValueError('Choose a Fortinet appliance or create a new connection.')
            token = request.form.get('api_token', '').strip()
            values = dict(old or {}, **{k: request.form.get(k, '') for k in
                          ('name', 'endpoint', 'vdom', 'fingerprint', 'self_signed')}, vendor='fortinet')
            values['kind'] = request.form.get('kind') or (old or {}).get('kind', 'vm')
            if not old:
                values.update(required_licenses='vm')
            else:
                values['required_licenses'] = ' '.join(old.get('required_licenses', []))
            definition = validate(values, netem.SDWAN_VENDORS)
            if old and old.get('simulator_id'):
                definition['simulator_id'] = old['simulator_id']
            if not token and (not old or old['endpoint'] != definition['endpoint'] or not manager.store.token(appliance_id)):
                raise ValueError('Paste the FortiGate REST API token. A new API address requires a new token.')
            saved = manager.store.save(definition, appliance_id, token=token or None)
            with manager.lock:
                manager.snapshot = manager.plan = None
                manager.set_job(state='idle', appliance_id=saved, steps=[], started_at=None, finished_at=None,
                                message='Connection saved. Discover the appliance to verify certificate trust, token and permissions.')
            netem.log_event('fortinet-connection', 'FortiGate API connection saved', appliance_id=saved)
            return jsonify(saved=True, appliance_id=saved, name=definition['name'], endpoint=definition['endpoint'],
                           vdom=definition['vdom'], fingerprint=definition['fingerprint'], self_signed=definition['self_signed'])
        except (ValueError, RuntimeError) as exc:
            return jsonify(error=str(exc)), 409

    @app.get('/api/v1/integrations/fortinet')
    def fortinet_status():
        return jsonify(manager.status())

    @app.get('/integrations/fortinet/<appliance_id>/recovery.json')
    def fortinet_recovery(appliance_id):
        value = manager.store.state('fortinet_lab_recovery:' + appliance_id)
        if not value:
            abort(404)
        response = jsonify(value)
        response.headers['Content-Disposition'] = 'attachment; filename="fortigate-lab-recovery.json"'
        return response

    def response(action):
        try:
            action()
            return jsonify(manager.status()), 202
        except (APIError, ValueError, RuntimeError) as exc:
            return jsonify(error=str(exc)), 409

    @app.post('/integrations/fortinet/discover')
    def fortinet_discover():
        return response(lambda: manager.start_discovery(request.form.get('id')))

    @app.post('/integrations/fortinet/preview')
    def fortinet_preview():
        try:
            payload = json.loads(request.form.get('configuration', ''))
            if not isinstance(payload, dict):
                raise ValueError('Invalid configuration.')
            manager.preview(request.form.get('id'), payload)
            return jsonify(manager.status())
        except (ValueError, RuntimeError) as exc:
            return jsonify(error=str(exc)), 409

    @app.post('/integrations/fortinet/apply')
    def fortinet_apply():
        if request.form.get('confirmation') != 'apply':
            return jsonify(error='Confirm the reviewed changes and console/backup access before applying.'), 400
        return response(lambda: manager.start_apply(request.form.get('id'), request.form.get('plan_id')))

    return manager
