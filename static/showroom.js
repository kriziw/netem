(() => {
  'use strict';
  const number = value => typeof value === 'number' && Number.isFinite(value);
  const format = value => number(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 2}) : '—';
  const total = (links, key) => links.length && links.every(link => number(link[key]))
    ? links.reduce((sum, link) => sum + link[key], 0) : null;
  const pct = (value, digits = 1) => number(value) ? `${value.toFixed(digits)}%` : '—';
  const words = value => String(value || '').replaceAll('_', ' ');
  // Smooth curves through lightly averaged samples; gaps (unknown samples) stay gaps.
  function chart(points, key, now, max) {
    const runs = [];
    let run = null;
    for (const point of points) {
      if (!number(point[key])) { run = null; continue; }
      if (!run) runs.push(run = []);
      run.push({x: Math.max(0, Math.min(300, (point.timestamp - now + 120) / 120 * 300)), value: point[key]});
    }
    let drawing = '';
    for (const items of runs) {
      const smoothed = items.map((item, index) => {
        const window = items.slice(Math.max(0, index - 1), index + 2);
        const value = window.reduce((sum, entry) => sum + entry.value, 0) / window.length;
        return {x: item.x, y: 50 - Math.min(1, value / max) * 46};
      });
      drawing += `M${smoothed[0].x.toFixed(1)},${smoothed[0].y.toFixed(1)} `;
      for (let index = 0; index < smoothed.length - 1; index += 1) {
        const before = smoothed[Math.max(0, index - 1)], from = smoothed[index];
        const to = smoothed[index + 1], after = smoothed[Math.min(smoothed.length - 1, index + 2)];
        const c1x = from.x + (to.x - before.x) / 6, c1y = from.y + (to.y - before.y) / 6;
        const c2x = to.x - (after.x - from.x) / 6, c2y = to.y - (after.y - from.y) / 6;
        drawing += `C${c1x.toFixed(1)},${c1y.toFixed(1)} ${c2x.toFixed(1)},${c2y.toFixed(1)} ${to.x.toFixed(1)},${to.y.toFixed(1)} `;
      }
    }
    return drawing;
  }

  const clock = seconds => {
    const value = Math.max(0, Math.round(Number(seconds) || 0));
    return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`;
  };

  // The running test's planned phases with their state and fill, from pause-free elapsed time.
  function phaseTimeline(scenario) {
    const phases = (scenario && scenario.phases) || [];
    const total = phases.reduce((sum, phase) => sum + (Number(phase.planned_s) || 0), 0);
    let start = 0;
    return phases.map((phase, index) => {
      const planned = Number(phase.planned_s) || 0;
      const elapsed = Number(scenario.elapsed_s) || 0;
      const into = number(scenario.phase_elapsed_s) ? scenario.phase_elapsed_s : elapsed - start;
      const item = {name: phase.name, planned_s: planned, share: total ? planned / total * 100 : 100 / phases.length,
        state: index < scenario.phase_index ? 'done' : index === scenario.phase_index ? 'current' : 'upcoming',
        fill: index < scenario.phase_index ? 100 : index === scenario.phase_index && planned
          ? Math.max(0, Math.min(100, into / planned * 100)) : 0};
      start += planned;
      return item;
    });
  }

  // When the next phase starts, counted from when the current phase really began.
  function phaseCountdown(scenario) {
    if (!scenario || !scenario.active || !number(scenario.phase_remaining_s)) return '';
    const left = clock(scenario.phase_remaining_s), next = scenario.next_phase;
    if (scenario.paused) return next ? `${next} follows ${left} after resuming` : `Ends ${left} after resuming`;
    if (scenario.phase_remaining_s < 1) return next ? `Next: ${next} shortly` : 'Finishing';
    return next ? `Next: ${next} in ${left}` : `Ends in ${left}`;
  }

  // Between snapshots the clocks keep running locally, so countdowns tick every second.
  function advance(scenario, seconds) {
    if (!scenario || !scenario.active || scenario.paused || !(seconds > 0)) return scenario;
    const moved = Object.assign({}, scenario);
    if (number(moved.elapsed_s)) moved.elapsed_s += seconds;
    if (number(moved.phase_elapsed_s)) moved.phase_elapsed_s += seconds;
    if (number(moved.phase_remaining_s)) moved.phase_remaining_s = Math.max(0, moved.phase_remaining_s - seconds);
    return moved;
  }

  // One row per planned phase of the finished test: what users got and where traffic went.
  function summaryRows(summary) {
    const mbps = value => number(value) ? (value >= 10 ? Math.round(value) : value.toFixed(1)) : '—';
    return ((summary && summary.phases) || []).map(phase => ({
      name: phase.name, reached: phase.reached !== false,
      duration: number(phase.duration_s) ? clock(phase.duration_s) : '',
      experience: number(phase.experience_score) ? `${Math.round(phase.experience_score)}` : '—',
      success: number(phase.success_pct) ? `${phase.success_pct.toFixed(1)}%` : '—',
      low: number(phase.worst_success_pct) && number(phase.success_pct) && phase.worst_success_pct < phase.success_pct - 0.05
        ? `low ${phase.worst_success_pct.toFixed(1)}%` : '',
      interactive: number(phase.interactive_p95_ms) ? `${Math.round(phase.interactive_p95_ms)} ms` : '—',
      traffic: (phase.wans || []).map(wan => ({label: wan.label, health: wan.health || 'unknown',
        text: `${wan.label} ${mbps(wan.down_mbps)}↓ ${mbps(wan.up_mbps)}↑`})),
      steering: Object.entries(phase.steering || {}).sort(([a], [b]) => classOrder(a) - classOrder(b)).map(([label, verdict]) => ({
        label: CLASS_SHORT[label] || label, text: VERDICTS_SHORT[verdict] || words(verdict), tone: VERDICT_TONE[verdict] || 'unknown'})),
    }));
  }

  const RESULTS = {passed: ['Passed', 'pass'], failed: ['Failed', 'fail'], stopped: ['Stopped', 'warn']};

  function reportView(summary) {
    const checks = summary.assertions || {};
    const [result, tone] = RESULTS[summary.result] || [words(summary.result) || 'Finished', 'unknown'];
    const target = summary.steering_target_s;
    return {
      name: summary.name || 'Test', result, tone,
      meta: [summary.link && `on ${summary.link}`, summary.site, number(summary.duration_s) && `${clock(summary.duration_s)} long`,
        number(checks.total) && checks.total ? `${checks.passed} of ${checks.total} checks passed` : 'no checks',
        number(summary.ended_at) && `finished ${new Date(summary.ended_at * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`]
        .filter(Boolean).join(' · '),
      // The first line repeats the result shown in the heading.
      conclusion: (summary.conclusion || []).slice(1),
      remediation: (summary.remediation || []).map(item => ({
        text: `${item.traffic_class} moved off ${item.wan} in ${item.seconds} s` +
          (item.within_target === true ? ` · within the ${target} s target` : item.within_target === false ? ` · slower than the ${target} s target` : ''),
        tone: item.within_target === false ? 'fail' : 'pass'})),
      remediationNote: !(summary.phases || []).some(phase => number(phase.experience_score))
        ? 'Not assessed: no simulated user traffic was measured.'
        : (summary.phases || []).some(phase => Object.values(phase.steering || {}).some(verdict => ['stuck', 'stuck_impact'].includes(verdict)))
          ? 'The appliance kept traffic on an impaired WAN and did not move it during the test.'
          : 'No traffic had to be moved: users stayed on healthy WANs.',
    };
  }

  const HEALTH = {healthy: 'Healthy', congested: 'Congested', degraded: 'Degraded', failed: 'Failed'};
  const VERDICTS_SHORT = {steered: 'steered', unaffected: 'unaffected', balanced: 'healthy', stuck: 'on impaired WAN',
    stuck_impact: 'users affected', no_healthy: 'no healthy WAN', idle: 'no traffic', partial: 'partly traced', unattributed: 'WAN unknown'};
  const VERDICT_TONE = {steered: 'pass', unaffected: 'pass', balanced: 'pass', stuck: 'warn', partial: 'warn',
    unattributed: 'warn', stuck_impact: 'fail', no_healthy: 'fail', idle: 'unknown'};
  const CLASS_SHORT = {'Voice & video': 'Voice', 'Web, collaboration & DNS': 'Web', 'File transfers': 'Files'};
  const classOrder = label => { const index = Object.keys(CLASS_SHORT).indexOf(label); return index < 0 ? 9 : index; };
  const REPORT_SECONDS = 1800;
  const VERDICTS = {steered: 'Steered away', unaffected: 'Unaffected', balanced: 'All WANs healthy', stuck: 'On impaired WAN',
    stuck_impact: 'Users affected', no_healthy: 'No healthy WAN', idle: 'No traffic', partial: 'Partly attributed',
    unattributed: 'WAN unknown'};

  // The three questions the screen answers: who the site is, what runs on the network, what users get.
  function siteView(site, session) {
    if (!site) {
      return {title: 'No client site selected', meta: 'Choose a site in the operator interface to show its industry, size and targets.',
        people: '', targets: [], lines: '', heading: 'Lab targets'};
    }
    const devices = Object.entries(site.devices || {}).map(([name, count]) =>
      `${count.toLocaleString()} ${{ot_device: 'OT devices', camera: 'cameras', guest: 'guests'}[name] || words(name)}`);
    const t = site.targets || {};
    const line = role => {
      const item = (site.wan_lines || {})[role];
      return item ? `${role === 'primary' ? 'Primary' : 'Backup'} ${String(item.preset).toUpperCase()} ${item.download_mbit}/${item.upload_mbit}` : null;
    };
    return {
      title: `${site.sub_industry} · ${site.function}`,
      meta: `${site.industry} · ${site.size} · ${site.criticality}`,
      people: [`${format(site.employees)} employees`, `${format(site.simulated_users)} simulated users`, ...devices].join(' · '),
      targets: [`Experience ≥ ${t.experience_min}`, `Success ≥ ${t.success_min_pct}%`, `Interactive ≤ ${t.interactive_p95_max_ms} ms`,
        ...(number(t.steering_max_s) ? [`Failover ≤ ${t.steering_max_s} s`] : []), ...(t.media_mode ? [`${t.media_mode} voice/video`] : [])],
      lines: ['Typical lines', line('primary'), line('backup')].filter(Boolean).join(' · '),
      heading: 'Experience targets',
    };
  }

  function resultView(experience) {
    const t = (experience && experience.targets) || {};
    const prefix = experience && experience.targets_source === 'lab' ? 'Lab target' : 'Target';
    const available = Boolean(experience && experience.available);
    const verdicts = (experience && experience.verdicts) || {};
    return {
      experience: {value: available && number(experience.experience_score) ? `${Math.round(experience.experience_score)} / 100` : '—',
        target: `${prefix} ≥ ${format(t.experience_min)}`, verdict: available ? verdicts.experience || 'unknown' : 'unknown'},
      success: {value: available ? pct(experience.success_pct, 2) : '—',
        target: `${prefix} ≥ ${format(t.success_min_pct)}%`, verdict: available ? verdicts.success || 'unknown' : 'unknown'},
      interactive: {value: available && number(experience.interactive_p95_ms) ? `${Math.round(experience.interactive_p95_ms)} ms` : '—',
        target: `${prefix} ≤ ${format(t.interactive_p95_max_ms)} ms`, verdict: available ? verdicts.interactive || 'unknown' : 'unknown'},
    };
  }

  function steeringView(item, targetSeconds) {
    const segments = (item.shares || []).filter(share => number(share.pct) && share.pct > 0)
      .map(share => ({width: share.pct, health: share.health || 'unknown', label: `${share.label} ${Math.round(share.pct)}%`}));
    if (number(item.unattributed_pct) && item.unattributed_pct > 0) {
      segments.push({width: item.unattributed_pct, health: 'unknown', label: `Unknown ${Math.round(item.unattributed_pct)}%`});
    }
    const reactions = (item.reactions || []).map(reaction => reaction.steered_after_seconds != null
      ? `moved off ${reaction.label} in ${reaction.steered_after_seconds} s` +
        (reaction.within_target === false ? ` · target ≤ ${targetSeconds} s missed` : reaction.within_target ? ' · within target' : '')
      : `${reaction.label} ${words(reaction.health)} for ${reaction.impaired_for_seconds} s`);
    return {label: item.label, verdict: VERDICTS[item.verdict] || words(item.verdict),
      tone: {good: 'pass', warn: 'warn', bad: 'fail'}[item.severity] || 'unknown',
      segments, note: [item.text, ...reactions].filter(Boolean).join(' · ')};
  }

  // A finished test's report replaces the live findings for half an hour, until the next test starts.
  const showReport = data => Boolean(data && data.last_test && !(data.scenario || {}).active &&
    number(data.last_test.ended_at) && data.timestamp - data.last_test.ended_at < REPORT_SECONDS);

  window.ShowroomUI = {format, total, chart, siteView, resultView, steeringView, phaseTimeline, summaryRows, reportView, showReport, clock, phaseCountdown, advance};

  if (typeof document === 'undefined' || !document.getElementById) return;
  const histories = new Map();
  let latest = null, online = false, page = 0, receivedAt = 0;
  const text = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = value; };
  const element = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.textContent = content;
    return node;
  };
  const pageSize = () => window.innerWidth <= 850 ? 1 : window.innerHeight >= 1400 ? 3 : 2;

  function renderSite() {
    const view = siteView(latest.site, latest.session);
    text('site-title', view.title);
    text('site-meta', view.meta);
    text('site-people', view.people);
    text('targets-heading', view.heading);
    text('site-lines', view.lines);
    document.getElementById('site-targets').replaceChildren(...view.targets.map(item => element('span', 'chip', item)));
    const session = latest.session || {};
    text('session-name', session.active ? session.name || 'Lab session' : 'No session running');
    text('session-site', session.active ? (session.site ? `Validating ${session.site}` : 'Session without a recorded site') : '');
  }

  function renderRunning() {
    const scenario = advance(latest.scenario || {}, online ? Math.min(5, (Date.now() - receivedAt) / 1000) : 0);
    const plan = latest.plan || {}, traffic = latest.traffic || {};
    text('demo-name', `${online ? '' : 'Last known: '}${scenario.active ? scenario.scenario_name || 'Running test'
      : plan.active ? plan.label || 'Site test plan' : 'Live network observation'}`);
    const phases = scenario.active ? phaseTimeline(scenario) : [];
    text('demo-stage', scenario.active
      ? [scenario.link && `On ${scenario.link}`, phases.length ? `Phase ${(scenario.phase_index || 0) + 1} of ${phases.length}: ${scenario.phase}` : null,
        scenario.step_label].filter(Boolean).join(' · ')
      : showReport(latest) ? `Last test: ${latest.last_test.name} ${latest.last_test.result || 'finished'} · report on the right`
      : plan.result ? `Last site test plan: ${plan.result}` : 'Waiting for the next test');
    const track = document.getElementById('demo-phases');
    track.hidden = !phases.length;
    track.replaceChildren(...phases.map(phase => {
      const part = element('span', `phase ${phase.state}`);
      part.style.flexBasis = `${phase.share.toFixed(2)}%`;
      part.title = `${phase.name} · ${clock(phase.planned_s)}`;
      const fill = element('i');
      fill.style.width = `${phase.fill.toFixed(1)}%`;
      part.append(fill, element('b', '', phase.name));
      return part;
    }));
    document.querySelector('.demo').classList.toggle('paused', Boolean(scenario.active && scenario.paused));
    text('demo-time', scenario.active && phases.length ? [`${clock(scenario.elapsed_s)} of about ${clock(scenario.planned_s)}`,
      scenario.paused ? `Paused by the operator, holding ${scenario.phase}` : null, phaseCountdown(scenario)].filter(Boolean).join(' · ') : '');
    document.getElementById('plan-tests').replaceChildren(...(plan.tests || []).map(test =>
      element('span', `chip ${{passed: 'pass', failed: 'fail', running: 'run', stopped: 'warn', skipped: 'warn'}[test.status] || ''}`,
        `${{passed: '✓', failed: '✗', running: '▶', stopped: '■', skipped: '–'}[test.status] || '·'} ${test.name}`)));
    const running = ['starting', 'running'].includes(traffic.status);
    text('workload-users', running && number(traffic.users) ? traffic.users.toLocaleString() : traffic.connected ? 'Idle' : '—');
    text('workload-summary', !traffic.configured ? 'Traffic Simulator not connected'
      : !traffic.connected ? 'Traffic Simulator unavailable'
      : [traffic.label, traffic.activity && `${traffic.activity} activity`, traffic.media_mode && `${traffic.media_mode} voice/video`]
        .filter(Boolean).join(' · ') || (running ? 'Simulated corporate users' : 'No workload running'));
    document.getElementById('app-mix').replaceChildren(...(running ? traffic.applications || [] : []).slice(0, 5).map(app => {
      const row = element('div', 'mix-row');
      const bar = element('div', 'bar');
      const fill = element('i');
      fill.style.width = `${Math.max(0, Math.min(100, app.share_pct))}%`;
      bar.append(fill);
      row.append(element('span', '', app.name), bar, element('span', '', `${Math.round(app.share_pct)}%`));
      return row;
    }));
  }

  function renderOutcome() {
    const view = resultView(latest.experience);
    for (const key of ['experience', 'success', 'interactive']) {
      const tile = document.getElementById(`result-${key}`);
      tile.className = `result ${online ? view[key].verdict : 'unknown'}`;
      tile.querySelector('[data-value]').textContent = online ? view[key].value : '—';
      tile.querySelector('[data-target]').textContent = view[key].target;
    }
    const traffic = latest.traffic || {};
    text('outcome-window', number(traffic.requests) ? `Last ${traffic.window_seconds || 60} s · ${traffic.requests.toLocaleString()} transactions` : '');
    const findings = document.getElementById('findings');
    const items = (latest.findings || []).slice(0, 3);
    findings.replaceChildren(...(items.length ? items.map(item => {
      const card = element('article', `finding ${item.severity || 'info'}`);
      card.append(element('h4', '', item.title));
      for (const wan of item.wans || []) {
        card.append(element('p', 'where', `${wan.label}${wan.affected ? ` · ${wan.affected} affected` : ''}${wan.causes.length ? ` ← ${wan.causes.join('; ')}` : ''}`));
      }
      if (item.unattributed) card.append(element('p', 'where', `${item.unattributed} without a reply to show the WAN`));
      if (item.hint || item.detail) card.append(element('p', 'detail', item.hint || item.detail));
      return card;
    }) : [element('p', 'muted', latest.experience && latest.experience.available
      ? 'No problems detected: users are getting the expected experience.' : 'No simulated user traffic yet.')]));
    const target = ((latest.experience || {}).targets || {}).steering_max_s;
    const steering = document.getElementById('steering');
    const classes = latest.steering || [];
    steering.replaceChildren(...(classes.length ? classes.map(item => {
      const view = steeringView(item, target);
      const row = element('div', 'steer');
      const head = element('div', 'steer-head');
      head.append(element('strong', '', view.label), element('span', `pill ${view.tone}`, view.verdict));
      const bar = element('div', 'steer-bar');
      for (const segment of view.segments) {
        const part = element('span', segment.health, segment.width >= 18 ? segment.label : '');
        part.style.width = `${segment.width}%`;
        part.title = segment.label;
        bar.append(part);
      }
      row.append(head, bar, element('p', 'detail', view.note));
      return row;
    }) : [element('p', 'muted', 'Steering is shown once simulated traffic can be traced to each WAN.')]));
  }

  function renderReport() {
    const visible = showReport(latest);
    document.getElementById('report').hidden = !visible;
    document.getElementById('live-outcome').hidden = visible;
    if (!visible) return;
    const view = reportView(latest.last_test);
    text('report-name', view.name);
    text('report-meta', view.meta);
    const result = document.getElementById('report-result');
    result.className = `pill ${view.tone}`;
    result.textContent = view.result;
    document.getElementById('report-conclusion').replaceChildren(...view.conclusion.map(line => element('li', '', line)));
    document.getElementById('report-phases').replaceChildren(...summaryRows(latest.last_test).map(row => {
      const tr = element('tr', row.reached ? '' : 'skipped');
      const name = element('td');
      name.append(element('strong', '', row.name), element('small', '', row.reached ? row.duration : 'not reached'));
      const success = element('td', '', row.success);
      if (row.low) success.append(element('small', '', row.low));
      const traffic = element('td');
      traffic.append(...row.traffic.map(wan => element('span', `wan ${wan.health}`, wan.text)));
      const steering = element('td');
      steering.append(...row.steering.map(item => element('span', `pill ${item.tone}`, `${item.label}: ${item.text}`)));
      tr.append(name, element('td', '', row.experience), success, element('td', '', row.interactive), traffic, steering);
      return tr;
    }));
    const remediation = document.getElementById('report-remediation');
    remediation.replaceChildren(...(view.remediation.length ? view.remediation.map(item => element('p', `fix ${item.tone}`, item.text))
      : [element('p', 'muted', view.remediationNote)]));
  }

  function renderPaths() {
    const links = latest.links;
    text('path-count', links.length);
    text('healthy-count', online ? `${links.filter(link => link.sla_pass && link.fault === 'normal').length} / ${links.length}` : '—');
    text('total-down', online ? `${format(total(links, 'down_mbps'))} Mbit/s` : '—');
    text('total-up', online ? `${format(total(links, 'up_mbps'))} Mbit/s` : '—');
    const pages = Math.max(1, Math.ceil(links.length / pageSize()));
    page %= pages;
    text('page-number', pages > 1 ? `Page ${page + 1} of ${pages} · rotates every 12 seconds` : '');
    const container = document.getElementById('paths');
    container.replaceChildren();
    if (!links.length) {
      container.append(element('p', 'empty', 'No WAN paths configured. Configure the lab from the operator interface.'));
    }
    for (const link of links.slice(page * pageSize(), (page + 1) * pageSize())) {
      const node = document.getElementById('path-template').content.firstElementChild.cloneNode(true);
      const field = (name, value) => { node.querySelector(`[data-field="${name}"]`).textContent = value; };
      const health = link.fault !== 'normal' ? 'failed' : link.health || (link.sla_pass ? 'healthy' : 'degraded');
      node.classList.add({healthy: 'good', congested: 'warn', degraded: 'bad', failed: 'bad'}[health] || 'good');
      field('name', link.name);
      field('profile', link.profile);
      field('health', `${online ? '' : 'Last known · '}${link.fault !== 'normal' ? words(link.fault) : HEALTH[health] || (link.sla_pass ? 'Model SLA pass' : 'Model SLA fail')}`);
      field('reason', link.health_reason || (link.sla_pass ? '' : 'Model SLA fails on the requested impairment'));
      field('down', format(online ? link.down_mbps : null));
      field('up', format(online ? link.up_mbps : null));
      for (const direction of ['down', 'up']) {
        const util = online ? link[`${direction}_util_pct`] : null;
        node.querySelector(`[data-bar="${direction}"]`).style.width = `${number(util) ? Math.max(0, Math.min(100, util)) : 0}%`;
        node.querySelector(`[data-bar="${direction}"]`).className = number(util) ? (util >= 90 ? 'full' : util >= 70 ? 'busy' : '') : '';
        field(`${direction}-util`, number(util) ? `${Math.round(util)}% of limit` : 'limit use unknown');
      }
      field('limits', `Configured limits · ↓ ${format(link.download_limit_mbit)} / ↑ ${format(link.upload_limit_mbit)} Mbit/s`);
      field('delay', `${format(link.delay_ms)} ms`);
      field('jitter', `${format(link.jitter_ms)} ms`);
      field('loss', `${format(link.loss_pct)}%`);
      field('quality', `${format(link.quality)}%`);
      field('users', number(link.users_success_pct) ? `Users on this WAN: ${pct(link.users_success_pct)} success` +
        (link.worst_app ? ` · worst ${link.worst_app} ${pct(link.worst_app_success_pct, 0)}` : '') : 'No simulated users traced to this WAN');
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

  function render() {
    if (!latest) return;
    renderSite();
    renderRunning();
    renderOutcome();
    renderReport();
    renderPaths();
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
      receivedAt = Date.now();
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
      // Retain labelled last-known state while blanking all live rates.
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
  setInterval(() => { if (latest) { page += 1; render(); } }, 12000);
  setInterval(() => { if (latest && online && (latest.scenario || {}).active) renderRunning(); }, 1000);
  window.addEventListener('resize', render);
  poll();
})();
