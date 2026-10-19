/* Guided FortiGate provisioning. POSTs are single-shot; uncertainty is resolved by status reads. */
(function (global) {
  'use strict';
  function mount() {
    const root = document.getElementById('fortinet-lab');
    if (!root) return {stop() {}};
    const find = id => root.querySelector('#fortinet-' + id);
    const text = (id, value) => { find(id).textContent = value; };
    let state = {}, initialized = '', timer = null, stopped = false, posting = false, firstRead = true;
    let connectionStage = 1, connectionInitialized = false, lastSpec = null;
    const ignoredPlans = new Set();
    const requests = new Set();
    const option = (value, label) => { const node = document.createElement('option'); node.value = value; node.textContent = label; return node; };
    function fill(select, interfaces, value) {
      select.replaceChildren(option('', 'Choose interface'));
      interfaces.forEach(row => select.append(option(row.name, row.name + ' · ' + (row.alias || row.role || row.type || 'port'))));
      select.value = value || '';
    }
    function showConnectionStep(step) {
      connectionStage = step;
      find('prepare').hidden = step !== 1;
      find('connection-panel').hidden = step !== 2;
      find('discovery-panel').hidden = step !== 3;
      if (step !== 3) { find('topology').hidden = true; find('review').hidden = true; }
    }
    function loadConnection(id) {
      find('connection-choice').value = id || '';
      const data = find('connection-choice').selectedOptions[0]?.dataset || {};
      for (const field of ['name', 'endpoint', 'vdom', 'fingerprint', 'kind']) {
        find('connection-' + field).value = data[field] || (field === 'vdom' ? 'root' : field === 'kind' ? 'vm' : '');
      }
      find('connection-self-signed').checked = data.selfSigned === '1';
      find('connection-fingerprint').required = data.selfSigned === '1';
      find('connection-token').value = '';
      find('connection-token').required = data.hasToken !== '1';
      text('token-help', data.hasToken === '1' ? 'A token is stored privately. Leave blank to keep it, or paste a replacement. Changing the API address requires a new token.' : 'Paste the token generated in Step 1.');
    }
    const ports = () => state.discovery?.interfaces || [];
    const address = name => ports().find(p => p.name === name)?.cidr || '';
    function wanRows(saved) {
      const count = Number(find('wan-count').value), container = find('wans');
      const previous = saved || [...container.querySelectorAll('[data-wan]')].map(row => ({interface: row.querySelector('select').value, address: row.querySelector('[data-address]').value, gateway: row.querySelector('[data-gateway]').value}));
      const suggestions = ports().filter(p => p.role === 'wan' && p.name !== find('management').value);
      container.replaceChildren();
      for (let i = 0; i < count; i++) {
        const group = document.createElement('fieldset'); group.dataset.wan = String(i);
        const legend = document.createElement('legend'); legend.textContent = 'WAN ' + (i + 1); group.append(legend);
        const grid = document.createElement('div'); grid.className = 'form-grid'; group.append(grid);
        const values = previous[i] || {interface: suggestions[i]?.name || '', address: suggestions[i]?.cidr || '', gateway: suggestions[i]?.gateway || ''};
        const select = document.createElement('select'); select.required = true; select.id = 'fortinet-wan-' + i;
        fill(select, ports(), values.interface);
        const fields = [['Interface', select], ['Address / prefix', document.createElement('input')], ['Gateway', document.createElement('input')]];
        fields.forEach(([label, input], index) => {
          const wrap = document.createElement('div'), title = document.createElement('label');
          input.id = input.id || 'fortinet-wan-' + i + '-' + index; title.htmlFor = input.id; title.textContent = label;
          input.required = true; wrap.append(title, input); grid.append(wrap);
          if (index === 1) { input.dataset.address = ''; input.value = values.address; }
          if (index === 2) { input.dataset.gateway = ''; input.value = values.gateway; }
        });
        select.addEventListener('change', () => {
          group.querySelector('[data-address]').value = address(select.value);
          group.querySelector('[data-gateway]').value = ports().find(p => p.name === select.value)?.gateway || '';
          invalidate();
        });
        container.append(group);
      }
    }
    function invalidate() { if (state.plan) ignoredPlans.add(state.plan.id); state.plan = null; find('review').hidden = true; find('confirm').checked = false; }
    function initialize(discovery) {
      const list = discovery.interfaces;
      const saved = discovery.saved_spec;
      fill(find('management'), list, saved?.management_interface || discovery.management_interface);
      const lan = list.find(p => p.role === 'lan' && p.name !== discovery.management_interface);
      fill(find('lan'), list, saved?.lan.interface || lan?.name);
      find('lan-address').value = saved?.lan.address || lan?.cidr || '';
      const count = list.filter(p => p.role === 'wan' && p.name !== discovery.management_interface).length;
      find('wan-count').value = String(saved?.wans.length || Math.min(3, Math.max(1, count || 2)));
      find('destination').value = saved?.destination || (discovery.suggested_target?.startsWith('198.1') ? '198.18.0.0/15' : '');
      find('health').value = saved?.health_server || discovery.suggested_target || '';
      find('nat').value = saved?.nat === false ? 'disable' : 'enable';
      wanRows(saved?.wans || []);
    }
    function render(next) {
      if (firstRead && next.appliance_id) find('appliance').value = next.appliance_id;
      firstRead = false;
      if (next.plan && ignoredPlans.has(next.plan.id)) next = {...next, plan: null};
      state = next;
      if (!connectionInitialized) {
        connectionInitialized = true;
        loadConnection(find('appliance').value);
        const chosen = find('appliance').selectedOptions[0];
        showConnectionStep(next.discovery || (chosen?.dataset.hasToken === '1' && chosen.dataset.endpoint) ? 3 : 1);
      }
      if (next.plan || next.applied_spec) lastSpec = next.plan?.spec || next.applied_spec;
      text('status', next.message || 'Ready.');
      const busy = posting || ['discovering', 'applying'].includes(next.state);
      root.querySelectorAll('button').forEach(button => { button.disabled = busy; });
      find('discover').querySelector('button[type=submit]').disabled = busy || !find('appliance').options.length;
      find('appliance').disabled = busy;
      if (next.appliance_id) {
        find('recovery').href = '/integrations/fortinet/' + encodeURIComponent(next.appliance_id) + '/recovery.json';
        find('recovery').hidden = !['applying', 'completed', 'needs_review', 'interrupted'].includes(next.state);
      }
      const steps = find('steps'); steps.replaceChildren();
      (next.steps || []).forEach(step => { const line = document.createElement('p'); line.textContent = step.table + ' · ' + (step.key ?? 'configuration') + ' · ' + step.state; steps.append(line); });
      const selected = find('appliance').value === next.appliance_id;
      find('topology').hidden = !next.discovery || !selected || connectionStage !== 3;
      if (next.discovery && selected) {
        const d = next.discovery, signature = next.appliance_id + ':' + next.started_at;
        if (signature !== initialized) { initialized = signature; initialize(d); }
        text('identity', [d.identity.hostname, d.identity.model, d.identity.version].filter(Boolean).join(' · '));
        text('discovery-summary', d.policy_count + ' firewall policies · SD-WAN ' + (d.sdwan.status || 'unknown') + ' · zones: ' + (d.sdwan.zones.join(', ') || 'none') + ' · central NAT ' + d.central_nat);
        text('existing', JSON.stringify({routes: d.routes, sdwan: d.sdwan, policies: d.policies}, null, 2));
        const table = find('interfaces'); table.replaceChildren();
        d.interfaces.forEach(port => { const row = document.createElement('tr'); [port.name, [port.type, port.role].filter(Boolean).join(' / '), port.cidr || 'Not configured', port.gateway || 'Not discovered', port.sdwan_member ? 'Yes' : 'No'].forEach(value => { const td = document.createElement('td'); td.textContent = value; row.append(td); }); table.append(row); });
      }
      find('review').hidden = !next.plan || !selected || connectionStage !== 3;
      if (next.plan && selected) {
        const p = next.plan, warnings = find('warnings'), plan = find('plan'); warnings.replaceChildren(); plan.replaceChildren();
        p.warnings.forEach(message => { const li = document.createElement('li'); li.textContent = message; warnings.append(li); });
        text('plan-summary', p.spec.wans.length + ' WAN link(s) · ' + p.actions.length + ' changes · expires ' + new Date(p.expires_at * 1000).toLocaleTimeString());
        p.actions.forEach(action => { const details = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre'); summary.textContent = action.method + ' ' + action.table + (action.key !== null ? ' / ' + action.key : ''); pre.textContent = JSON.stringify({before: action.before, after: action.after}, null, 2); details.append(summary, pre); plan.append(details); });
      }
      find('finish').hidden = next.state !== 'completed' || !selected || connectionStage !== 3;
      if (next.state === 'completed') {
        find('finish-appliance').href = '/appliances?edit=' + encodeURIComponent(next.appliance_id) + '#appliance-editor';
        text('finish-summary', lastSpec?.lan ? 'Base configuration verified. FortiGate LAN gateway: ' + lastSpec.lan.address.split('/')[0] + '. Complete forwarding verification below.' : 'Base configuration verified. Complete forwarding verification below using the LAN gateway from your approved configuration.');
      }
    }
    async function requestData(url, options = {}) {
      const controller = new AbortController(); requests.add(controller);
      const deadline = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(url, {...options, signal: controller.signal, headers: {'Accept': 'application/json'}});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || result.message || 'Request failed (HTTP ' + response.status + ').');
        return result;
      } finally { clearTimeout(deadline); requests.delete(controller); }
    }
    async function post(url, fields) {
      if (posting) return;
      posting = true;
      const data = new URLSearchParams({...fields, integration_csrf: root.querySelector('[name=integration_csrf]').value});
      render(state);
      try { const next = await requestData(url, {method: 'POST', body: data}); posting = false; render(next); }
      catch (error) { posting = false; render(state); text('status', error.message + ' Check operation status before retrying; an interrupted request may have been accepted.'); }
      schedule();
    }
    find('token-ready').addEventListener('click', () => showConnectionStep(2));
    find('token-back').addEventListener('click', () => showConnectionStep(1));
    find('edit-connection').addEventListener('click', () => { loadConnection(find('appliance').value); showConnectionStep(2); });
    find('connection-choice').addEventListener('change', () => loadConnection(find('connection-choice').value));
    find('connection-self-signed').addEventListener('change', () => { find('connection-fingerprint').required = find('connection-self-signed').checked; });
    find('connection').addEventListener('submit', async event => {
      event.preventDefault();
      if (posting) return;
      posting = true; render(state);
      const body = new URLSearchParams(new FormData(find('connection')));
      text('connection-status', 'Saving the API connection…');
      try {
        const saved = await requestData(root.dataset.connectionUrl, {method:'POST', body});
        let item = [...find('appliance').options].find(o => o.value === saved.appliance_id);
        if (!item) { item = option(saved.appliance_id, ''); find('appliance').append(item); }
        item.textContent = saved.name + ' · ' + saved.endpoint + ' · VDOM: ' + saved.vdom;
        item.dataset.hasToken = '1'; item.dataset.endpoint = saved.endpoint;
        find('appliance').value = saved.appliance_id;
        let connection = [...find('connection-choice').options].find(o => o.value === saved.appliance_id);
        if (!connection) { connection = option(saved.appliance_id, saved.name); find('connection-choice').append(connection); }
        Object.assign(connection.dataset, {name:saved.name, endpoint:saved.endpoint, vdom:saved.vdom,
          kind:find('connection-kind').value, fingerprint:saved.fingerprint, selfSigned:saved.self_signed ? '1' : '0', hasToken:'1'});
        find('connection-token').value = '';
        find('connection-token').required = false;
        initialized = ''; invalidate();
        state = {state:'idle', appliance_id:saved.appliance_id, message:'Connection saved. Click Discover appliance to verify the certificate, token and permissions.', steps:[]};
        posting = false; showConnectionStep(3); render(state);
      } catch (error) {
        posting = false; render(state); text('connection-status', error.message + ' The connection was not confirmed; check before submitting again.');
      }
      schedule();
    });
    find('discover').addEventListener('submit', event => { event.preventDefault(); initialized = ''; post(root.dataset.discoverUrl, {id: find('appliance').value}); });
    find('appliance').addEventListener('change', () => { invalidate(); find('topology').hidden = true; initialized = ''; });
    find('lan').addEventListener('change', () => { find('lan-address').value = address(find('lan').value); });
    find('wan-count').addEventListener('change', () => { wanRows(); invalidate(); });
    find('configure').addEventListener('input', invalidate);
    find('configure').addEventListener('change', invalidate);
    find('configure').addEventListener('submit', event => {
      event.preventDefault();
      const configuration = {management_interface: find('management').value, lan: {interface: find('lan').value, address: find('lan-address').value},
        wans: [...find('wans').querySelectorAll('[data-wan]')].map(group => ({interface: group.querySelector('select').value, address: group.querySelector('[data-address]').value, gateway: group.querySelector('[data-gateway]').value})),
        destination: find('destination').value, health_server: find('health').value, nat: find('nat').value === 'enable'};
      find('confirm').checked = false;
      post(root.dataset.previewUrl, {id: find('appliance').value, configuration: JSON.stringify(configuration)});
    });
    find('apply').addEventListener('submit', event => {
      event.preventDefault();
      if (!state.plan || !find('confirm').checked) return;
      const plan = state.plan; invalidate();
      post(root.dataset.applyUrl, {id: plan.appliance_id, plan_id: plan.id, confirmation: 'apply'});
    });
    async function refresh() {
      if (stopped || posting) return schedule();
      try { render(await requestData(root.dataset.statusUrl)); }
      catch (error) { if (!stopped) text('status', 'Status unavailable: ' + error.message); }
      schedule();
    }
    function schedule() { if (!stopped) { clearTimeout(timer); timer = setTimeout(refresh, 3000); } }
    refresh();
    return {stop() { stopped = true; clearTimeout(timer); requests.forEach(controller => controller.abort()); }};
  }
  global.NetEmFortinet = {mount};
})(typeof window !== 'undefined' ? window : globalThis);
