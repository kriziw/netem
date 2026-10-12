window.NetEmInventory = (() => {
  const text = (selector, value) => document.querySelectorAll(selector).forEach(node => { node.textContent = value; });
  const date = value => value ? new Date(value * 1000).toLocaleString() : 'Not checked';
  function render(snapshot) {
    for (const row of snapshot.appliances || []) {
      const element = document.querySelector('[data-inventory-row="' + CSS.escape(row.id) + '"]');
      if (!element) continue;
      const observation = row.observation || {};
      element.querySelector('[data-inventory-availability]').textContent = row.stale ? 'Stale / unknown' : (observation.availability || 'Not checked');
      element.querySelector('[data-inventory-health]').textContent = row.stale ? 'Health unknown' : 'Health: ' + (observation.health?.state || 'unknown');
      element.querySelector('[data-inventory-checked]').textContent = 'Checked: ' + date(row.checked_at);
      element.querySelector('[data-inventory-selection]').textContent = row.selected
        ? (snapshot.selection_verified ? 'In use' : 'Last selected / unverified') : 'Available';
      const licenses = element.querySelector('[data-inventory-licenses]');
      licenses.replaceChildren();
      if (row.stale || !observation.licenses?.length) {
        licenses.textContent = row.stale ? 'Unknown / stale' : (observation.api === 'unsupported' ? 'Unsupported adapter' : 'Unknown / not checked');
      } else for (const license of observation.licenses) {
        const line = document.createElement('div');
        const badge = document.createElement('span');
        badge.className = 'status ' + (['expired', 'invalid'].includes(license.state) ? 'bad' : license.state === 'valid' ? 'good' : 'warn');
        badge.textContent = license.key + ' · ' + license.state;
        line.append(badge);
        if (license.days_left != null) line.append(document.createTextNode(' ' + license.days_left + ' days'));
        licenses.append(line);
      }
    }
    const platform = snapshot.platform || {}, path = platform.path || {}, network = platform.network || {};
    text('[data-workload-path]', (snapshot.selection?.name || 'Appliance / existing route') + ' · ' + (snapshot.selection?.mode || 'appliance').replaceAll('_', ' '));
    const observations = platform.path_health?.paths || [];
    document.querySelectorAll('[data-path-health]').forEach(node => {
      const observation = observations.find(item => item.id === node.dataset.pathHealth);
      const fresh = observation?.checked_at && Date.now()/1000 - observation.checked_at >= 0 && Date.now()/1000 - observation.checked_at <= 90;
      node.textContent = !fresh ? 'Unknown / not observed' : observation.reachable === true ? 'Reachable · ' + observation.latency_ms + ' ms (HTTP health)' : observation.reachable === false ? 'No target reply on this path' : 'Path inactive / unknown';
    });
    const stale = snapshot.platform_stale;
    text('[data-platform-time]', 'Last check: ' + date(platform.checked_at));
    text('[data-platform-freshness]', stale ? 'Stale / unknown' : 'Current observation');
    text('[data-platform-simulator]', stale ? 'Unknown / stale' : platform.connected ? 'Connected' : 'Unavailable');
    text('[data-platform-version]', platform.simulator?.version || 'Unknown');
    text('[data-platform-workload]', stale ? 'Unknown / stale' : network.workload_active === true ? 'Running' : network.workload_active === false ? 'Idle' : 'Unknown');
    const clock = platform.simulator?.clock || {};
    text('[data-platform-clock]', stale ? 'Unknown / stale' : clock.synchronized === true ? 'Synchronized' : clock.synchronized === false ? 'Not synchronized' : clock.container === true ? 'Shared host clock; sync unverified' : 'Unknown');
    text('[data-platform-target]', stale ? 'Unknown / stale' : path.target?.ok === true ? 'Reachable' : 'Not verified');
    text('[data-platform-target-message]', path.target?.message || 'No verified target observation.');
    text('[data-platform-path]', path.message || 'Not checked');
    text('[data-platform-gateway]', path.path?.gateway || 'Unknown');
    text('[data-platform-interface]', path.path?.interface || 'Unknown');
    text('[data-platform-target-address]', path.path?.target || 'Unknown');
    text('[data-platform-job]', network.job?.state || 'None');
    const management = platform.target_management || {};
    text('[data-platform-management]', stale ? 'Unknown / stale' : management.connected ? 'Connected' : 'Not verified');
    text('[data-platform-management-message]', management.message || (management.configured ? 'Version ' + (management.version || 'unknown') : 'Not configured. Connect the target management agent in the simulator.'));
    const errors = document.querySelector('[data-platform-errors]');
    if (errors) {
      errors.replaceChildren();
      for (const error of platform.errors || []) {
        const line = document.createElement('p'); line.className = 'docs-warning'; line.textContent = error; errors.append(line);
      }
    }
  }
  function mount() {
    let stopped = false, fetching = false;
    const controller = new AbortController();
    async function refresh() {
      if (stopped || fetching) return;
      fetching = true;
      try {
        const response = await fetch('/api/v1/appliances', {cache:'no-store', signal:controller.signal});
        if (!response.ok) throw new Error('Unavailable');
        const snapshot = await response.json();
        if (!stopped) render(snapshot);
      } catch (_) {
        if (!stopped) {
          text('[data-platform-freshness]', 'Connection lost · last known observations');
          text('[data-platform-simulator], [data-platform-target], [data-platform-management]', 'Unknown / connection lost');
          text('[data-inventory-availability]', 'Unknown / connection lost');
          text('[data-inventory-health]', 'Health unknown');
          text('[data-inventory-licenses]', 'Unknown / connection lost');
          text('[data-path-health]', 'Unknown / connection lost');
        }
      } finally { fetching = false; }
    }
    document.querySelectorAll('[data-inventory-time]').forEach(node => { node.textContent = date(Number(node.dataset.inventoryTime)); });
    document.querySelectorAll('[data-inventory-select]').forEach(form => form.addEventListener('submit', () => {
      const button = form.querySelector('button'); button.disabled = true; button.textContent = 'Verifying traffic path…';
    }));
    refresh();
    const timer = setInterval(refresh, 15000);
    return {stop() { stopped = true; clearInterval(timer); controller.abort(); }};
  }
  return {mount, render};
})();
