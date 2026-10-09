(() => {
  'use strict';
  const number = value => typeof value === 'number' && Number.isFinite(value);
  const format = value => number(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 2}) : '—';
  const total = (links, key) => links.length && links.every(link => number(link[key]))
    ? links.reduce((sum, link) => sum + link[key], 0) : null;
  function chart(points, key, now, max) {
    let drawing = '', connected = false;
    for (const point of points) {
      if (!number(point[key])) { connected = false; continue; }
      const x = Math.max(0, Math.min(300, (point.timestamp - now + 120) / 120 * 300));
      const y = 50 - Math.min(1, point[key] / max) * 46;
      drawing += `${connected ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)} `;
      connected = true;
    }
    return drawing;
  }
  window.ShowroomUI = {format, total, chart};
  const histories = new Map();
  let latest = null, online = false, page = 0;
  const text = (id, value) => { document.getElementById(id).textContent = value; };
  const pageSize = () => {
    const columns = window.innerWidth >= 2200 ? 3 : window.innerWidth <= 850 ? 1 : 2;
    return columns * (window.innerHeight >= 1400 ? 2 : 1);
  };

  function render() {
    if (!latest) return;
    const links = latest.links;
    text('path-count', links.length);
    text('healthy-count', online ? `${links.filter(link => link.sla_pass && link.fault === 'normal').length} / ${links.length}` : '—');
    text('total-down', online ? `${format(total(links, 'down_mbps'))} Mbit/s` : '—');
    text('total-up', online ? `${format(total(links, 'up_mbps'))} Mbit/s` : '—');
    const scenario = latest.scenario;
    text('demo-name', `${online ? '' : 'Last known: '}${scenario.active ? scenario.scenario_name || 'Running demonstration' : 'Live network observation'}`);
    text('demo-stage', scenario.active ? `Stage ${scenario.step || 0} of ${scenario.step_count || 0} · ${scenario.step_label || 'Preparing'}` : 'Waiting for the next demonstration');
    text('session-name', latest.session.active ? `Session · ${latest.session.name || 'Active'}` : '');
    const pages = Math.max(1, Math.ceil(links.length / pageSize()));
    page %= pages;
    text('page-number', pages > 1 ? `Page ${page + 1} of ${pages} · rotates every 12 seconds` : '');
    const container = document.getElementById('paths');
    container.replaceChildren();
    if (!links.length) {
      const empty = document.createElement('p');
      empty.className = 'empty';
      empty.textContent = 'No WAN paths configured. Configure the lab from the operator interface.';
      container.append(empty);
    }
    for (const link of links.slice(page * pageSize(), (page + 1) * pageSize())) {
      const node = document.getElementById('path-template').content.firstElementChild.cloneNode(true);
      const field = (name, value) => { node.querySelector(`[data-field="${name}"]`).textContent = value; };
      const impaired = link.fault !== 'normal' || !link.sla_pass;
      node.classList.add(impaired ? 'bad' : link.quality < 75 ? 'warn' : 'good');
      field('name', link.name);
      field('profile', link.profile);
      field('health', `${online ? '' : 'Last known · '}${link.fault !== 'normal' ? link.fault.replaceAll('_', ' ') : link.sla_pass ? 'Model SLA pass' : 'Model SLA fail'}`);
      field('down', format(online ? link.down_mbps : null));
      field('up', format(online ? link.up_mbps : null));
      field('limits', `Configured limits · ↓ ${format(link.download_limit_mbit)} / ↑ ${format(link.upload_limit_mbit)} Mbit/s`);
      field('delay', `${format(link.delay_ms)} ms`);
      field('jitter', `${format(link.jitter_ms)} ms`);
      field('loss', `${format(link.loss_pct)}%`);
      field('quality', `${format(link.quality)}%`);
      field('sample', !online ? 'Connection lost · live traffic unavailable' : !link.traffic_available
        ? 'Traffic measurement unavailable · waiting for a fresh sample'
        : link.down_mbps === 0 && link.up_mbps === 0 ? 'Measured idle · no traffic crossing this path' : 'Live measured traffic');
      const points = histories.get(link.id) || [];
      const max = Math.max(1, ...points.flatMap(point => [point.down_mbps, point.up_mbps]).filter(number));
      node.querySelector('.download').setAttribute('d', chart(points, 'down_mbps', latest.timestamp, max));
      node.querySelector('.upload').setAttribute('d', chart(points, 'up_mbps', latest.timestamp, max));
      container.append(node);
    }
  }

  function connection(connected) {
    online = connected;
    document.body.classList.toggle('disconnected', !connected);
    const badge = document.getElementById('connection');
    badge.className = connected ? 'live' : 'offline';
    badge.textContent = connected ? 'Live' : 'Connection lost · reconnecting';
    render();
  }

  async function poll() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch('/api/snapshot', {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error('Snapshot unavailable');
      const data = await response.json();
      if (!number(data.timestamp) || !Array.isArray(data.links) || !data.scenario || !data.session) throw new Error('Invalid snapshot');
      latest = data;
      const ids = new Set(data.links.map(link => link.id));
      for (const id of histories.keys()) if (!ids.has(id)) histories.delete(id);
      for (const link of data.links) {
        const points = (histories.get(link.id) || []).filter(point => point.timestamp >= data.timestamp - 120 && point.timestamp <= data.timestamp).slice(-60);
        points.push({timestamp: data.timestamp, down_mbps: link.down_mbps, up_mbps: link.up_mbps});
        histories.set(link.id, points);
      }
      text('updated', `Updated ${new Date(data.timestamp * 1000).toLocaleTimeString()}`);
      connection(true);
    } catch (error) {
      // Retain labelled last-known impairments while blanking all live rates.
      const now = Date.now() / 1000;
      for (const [id, points] of histories) {
        const recent = points.filter(point => point.timestamp >= now - 120).slice(-60);
        recent.push({timestamp: now, down_mbps: null, up_mbps: null});
        histories.set(id, recent);
      }
      connection(false);
    } finally {
      clearTimeout(timeout);
      setTimeout(poll, 2000);
    }
  }
  if (typeof document !== 'undefined') {
    setInterval(() => { if (latest) { page += 1; render(); } }, 12000);
    window.addEventListener('resize', render);
    poll();
  }
})();
