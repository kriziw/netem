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
    if (!scenario || !scenario.active || scenario.paused || scenario.preparing_workload || !(seconds > 0)) return scenario;
    const moved = Object.assign({}, scenario);
    if (number(moved.elapsed_s)) moved.elapsed_s += seconds;
    if (number(moved.phase_elapsed_s)) moved.phase_elapsed_s += seconds;
    if (number(moved.phase_remaining_s)) moved.phase_remaining_s = Math.max(0, moved.phase_remaining_s - seconds);
    if (number(moved.starts_in_s)) moved.starts_in_s = Math.max(0, moved.starts_in_s - seconds);
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
        number(summary.ended_at) && `finished ${window.NetEmTime.clock(summary.ended_at, {seconds: false})}`]
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
          : (summary.phases || []).some(phase => Object.keys(phase.steering || {}).length)
            ? 'No traffic had to be moved: users stayed on healthy WANs.'
            : 'No steering was recorded for this test, so it cannot say whether the appliance moved traffic.',
    };
  }

  const HEALTH = {healthy: 'Healthy', congested: 'Busy', queue_pressure: 'Queue pressure', degraded: 'Degraded', failed: 'Down'};
  const VERDICTS_SHORT = {steered: 'steered', unaffected: 'unaffected', balanced: 'healthy', stuck: 'on impaired WAN',
    stuck_impact: 'users affected', no_healthy: 'no healthy WAN', idle: 'no traffic', partial: 'partly traced', unattributed: 'WAN unknown'};
  const VERDICT_TONE = {steered: 'pass', unaffected: 'pass', balanced: 'pass', stuck: 'warn', partial: 'warn',
    unattributed: 'warn', stuck_impact: 'fail', no_healthy: 'fail', idle: 'unknown'};
  const CLASS_SHORT = {'Voice & video': 'Voice', 'Web, collaboration & DNS': 'Web', 'File transfers': 'Files'};
  const classOrder = label => { const index = Object.keys(CLASS_SHORT).indexOf(label); return index < 0 ? 9 : index; };
  const REPORT_SECONDS = 1800;
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
        target: `${prefix} ≥ ${format(t.experience_min)}` + (experience && experience.scoring_complete === false ? ` · provisional${number(experience.coverage_pct) ? ', ' + pct(experience.coverage_pct, 0) + ' coverage' : ''}` : ''), verdict: available ? verdicts.experience || 'unknown' : 'unknown'},
      success: {value: available ? pct(experience.success_pct, 2) : '—',
        target: `${prefix} ≥ ${format(t.success_min_pct)}%`, verdict: available ? verdicts.success || 'unknown' : 'unknown'},
      interactive: {value: available && number(experience.interactive_p95_ms) ? `${Math.round(experience.interactive_p95_ms)} ms` : '—',
        target: `${prefix} ≤ ${format(t.interactive_p95_max_ms)} ms`, verdict: available ? verdicts.interactive || 'unknown' : 'unknown'},
    };
  }

  // Rates settle over the last ~10 s so the numbers read calmly from across a room.
  function smoothedRate(points, key) {
    const values = (points || []).map(point => point[key]).filter(number).slice(-10);
    return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  }

  const rateText = value => !number(value) ? '—' : value >= 10 ? `${Math.round(value)}` : `${Math.round(value * 10) / 10}`;

  function carriesText(carries) {
    if (!Array.isArray(carries)) return '';
    if (!carries.length) return 'No simulated users on this WAN';
    const list = carries.length > 1 ? `${carries.slice(0, -1).join(', ')} and ${carries[carries.length - 1]}` : carries[0];
    return `Carries ${list}`;
  }

  function routeView(route) {
    if (route.state === 'idle') return {label: route.label, text: 'No traffic', tone: 'unknown'};
    if (!route.wan) return {label: route.label, text: 'Not identified', tone: 'unknown'};
    return {label: route.label, text: route.wan, tone: route.state === 'good' ? 'pass' : 'warn'};
  }

  function planProgress(plan) {
    const tests = (plan && plan.tests) || [];
    const index = tests.findIndex(test => test.status === 'running');
    if (!plan || !plan.active || index < 0) return '';
    return `Site test plan · test ${index + 1} of ${tests.length}`;
  }

  // A finished test's report replaces the live findings for half an hour, until the next test starts.
  const showReport = data => Boolean(data && data.last_test && !(data.scenario || {}).active &&
    number(data.last_test.ended_at) && data.timestamp - data.last_test.ended_at < REPORT_SECONDS);

  // After a run sequence, the plan's results replace the dashboard for half an hour, until the next test.
  const showPlanSummary = data => Boolean(data && data.plan_summary && !(data.scenario || {}).active &&
    !(data.plan || {}).active && number(data.plan_summary.ended_at) && data.timestamp - data.plan_summary.ended_at < REPORT_SECONDS);

  // What fills the screen: a waiting screen before the session, a test's announcement for the
  // few seconds before it starts, a run sequence's results, and otherwise the live dashboard.
  function stageMode(data) {
    const scenario = (data && data.scenario) || {};
    if (scenario.active && scenario.intro) return 'intro';
    if (showPlanSummary(data)) return 'summary';
    if (data && !(data.session || {}).active && !scenario.active && !(data.plan || {}).active && !showReport(data)) return 'waiting';
    return 'live';
  }

  function waitingView(site) {
    return site ? {site: true, title: `${site.sub_industry} · ${site.function}`, meta: `${site.industry} · ${site.size} · ${site.criticality}`}
      : {site: false, title: '', meta: ''};
  }

  // The coming test: what each phase does, what is checked, and when it starts.
  function introView(scenario, plan) {
    const seconds = Math.ceil(Number(scenario.starts_in_s) || 0);
    // A site plan's tests are named after the plan; the plan is named on the line below instead.
    const prefix = plan && plan.active && plan.label ? `${plan.label}: ` : null;
    const name = scenario.scenario_name || 'Next test';
    return {
      where: scenario.link ? `Coming up on ${scenario.link}` : 'Coming up',
      name: prefix && name.startsWith(prefix) ? name.slice(prefix.length) : name,
      description: scenario.description || '',
      countdown: seconds > 0 ? String(seconds) : 'Now',
      phases: (scenario.phases || []).map((phase, index) => ({
        number: index + 1, name: phase.name, duration: clock(phase.planned_s), what: phase.what || ''})),
      checks: scenario.checks || [],
      meta: [number(scenario.planned_s) && scenario.planned_s > 0 ? `About ${clock(scenario.planned_s)} long` : null,
        planProgress(plan)].filter(Boolean).join(' · '),
    };
  }

  const TEST_RESULTS = {passed: ['Passed', 'pass'], failed: ['Failed', 'fail'], stopped: ['Stopped', 'warn'], skipped: ['Not run', 'unknown']};

  // The whole run: the verdict, one row per test with what users got, and what the run showed.
  function planSummaryView(summary) {
    const [result, tone] = RESULTS[summary.result] || [words(summary.result) || 'Finished', 'unknown'];
    const checks = summary.checks || {};
    return {
      eyebrow: summary.label ? `Site test plan results · ${summary.label}` : 'Site test plan results',
      title: summary.title || 'Site test plan finished', result, tone,
      meta: [number(summary.duration_s) && `${clock(summary.duration_s)} in total`,
        number(checks.total) && checks.total ? `${checks.passed} of ${checks.total} checks met` : null,
        number(summary.ended_at) && `finished ${window.NetEmTime.clock(summary.ended_at, {seconds: false})}`]
        .filter(Boolean).join(' · '),
      tests: (summary.tests || []).map(test => {
        const [label, rowTone] = TEST_RESULTS[test.result] || [words(test.result) || '—', 'unknown'];
        const dropped = number(test.experience) && number(test.experience_low) && test.experience_low < test.experience - 0.5;
        const facts = !test.measured ? [test.result === 'skipped' ? 'Not run: the plan stopped before this test' : 'No simulated user traffic measured'] : [
          dropped ? `Experience ${Math.round(test.experience)} → ${Math.round(test.experience_low)} in ${test.low_phase}`
            : number(test.experience) ? `Experience ${Math.round(test.experience)}` : null,
          // Above 99% the second decimal matters: 99.98% is not 100%.
          number(test.success_low) ? `lowest success ${test.success_low.toFixed(test.success_low >= 99 ? 2 : 1)}%` : null,
          number(test.p95_max) ? `slowest response ${Math.round(test.p95_max)} ms` : null];
        return {name: test.name, result: label, tone: rowTone,
          where: [test.link, test.checks && test.checks.total ? `${test.checks.passed}/${test.checks.total} checks` : null].filter(Boolean).join(' · '),
          facts: facts.filter(Boolean).join(' · '),
          moved: (test.moved || []).map(item => `${item.traffic_class} moved off ${item.wan} in ${item.seconds} s`)};
      }),
      insights: (summary.insights || []).map(item => ({text: item.text, tone: ['pass', 'warn', 'fail'].includes(item.tone) ? item.tone : 'pass'})),
    };
  }

  // The operator's showcase video takes the waiting screen's place: a YouTube or Vimeo embed, or the uploaded file.
  // Only the hosts the server allows are embedded, and the same check is repeated here.
  const VIDEO_EMBED = /^https:\/\/(www\.youtube-nocookie\.com\/embed\/[A-Za-z0-9_-]{6,20}|player\.vimeo\.com\/video\/[0-9]{6,})$/;
  // Embeds loop and start muted, as the uploaded file does. YouTube repeats one video only when it is also the
  // playlist it plays; Vimeo has its own loop flag.
  function embedAddress(src) {
    if (src.includes('youtube-nocookie')) {
      const id = src.split('/').pop();
      return `${src}?autoplay=1&mute=1&loop=1&playlist=${id}`;
    }
    return `${src}?autoplay=1&muted=1&loop=1`;
  }
  function waitingMedia(video) {
    if (!video) return null;
    if (video.kind === 'embed' && VIDEO_EMBED.test(video.src || '')) return {tag: 'iframe', src: embedAddress(video.src)};
    if (video.kind === 'file' && video.src === '/assets/showcase-video') {
      return {tag: video.media === 'image' ? 'img' : 'video', src: video.src};
    }
    return null;
  }
  window.ShowroomUI = {format, total, chart, siteView, resultView, phaseTimeline, summaryRows, reportView, showReport, clock,
    phaseCountdown, advance, smoothedRate, rateText, carriesText, routeView, planProgress, stageMode, waitingView, introView,
    showPlanSummary, planSummaryView, waitingMedia, embedAddress, groupCards};

  if (typeof document === 'undefined' || !document.getElementById) return;
  const histories = new Map();
  let latest = null, online = false, page = 0, receivedAt = 0;
  const text = (id, value) => setText(document.getElementById(id), value);
  const element = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content != null) node.textContent = content;
    return node;
  };
  const pageSize = () => window.innerWidth <= 850 ? 1 : window.innerHeight >= 1400 ? 3 : 2;

  // The SD-WAN vendor under test, next to the MSP brand in the header.
  function renderPartner() {
    const appliance = latest.appliance || {};
    const partner = document.getElementById('partner');
    partner.hidden = !appliance.vendor_name;
    const logo = document.getElementById('partner-logo');
    logo.hidden = !appliance.vendor;
    if (appliance.vendor && logo.getAttribute('src') !== `/assets/vendors/${appliance.vendor}.png`) logo.setAttribute('src', `/assets/vendors/${appliance.vendor}.png`);
    logo.alt = appliance.vendor_name || '';
    text('partner-name', [appliance.vendor ? null : appliance.vendor_name, appliance.model || appliance.product].filter(Boolean).join(' · '));
  }

  function renderSite() {
    const view = siteView(latest.site, latest.session);
    text('site-title', view.title);
    text('site-meta', view.meta);
    text('site-people', view.people);
    const session = latest.session || {};
    text('session-name', session.active ? session.name || 'Lab session' : 'No session running');
    text('session-site', session.active ? (session.site ? `Validating ${session.site}` : 'Session without a recorded site') : '');
  }

  // ---- Smooth updates: panels keep their elements between samples, numbers ease to their new values, and the
  // sparklines scroll on every frame. Nothing is rebuilt at each sample, so transitions and animations run through.
  const frameLoop = typeof requestAnimationFrame === 'function';
  const setText = (node, value) => {
    if (!node) return;
    const next = value == null ? '' : String(value);
    if (node.textContent !== next) node.textContent = next;
  };
  const sigs = new Map();
  // True when a panel's content changed since it was last built: panels that rarely change are rebuilt only then.
  const changed = (key, value) => {
    const signature = JSON.stringify(value);
    if (sigs.get(key) === signature) return false;
    sigs.set(key, signature);
    return true;
  };
  // One element per item, found by key and reused; new items are created and removed ones leave; order follows items.
  function reconcile(container, items, keyOf, create, update) {
    for (const node of [...container.children]) if (node.dataset.key === undefined) node.remove();
    const existing = new Map();
    for (const node of container.children) existing.set(node.dataset.key, node);
    const nodes = items.map((item, index) => {
      const key = String(keyOf(item, index));
      let node = existing.get(key);
      if (node) existing.delete(key); else node = create(item, index);
      node.dataset.key = key;
      update(node, item, index);
      return node;
    });
    existing.forEach(node => node.remove());
    nodes.forEach((node, index) => {
      if (container.children[index] !== node) container.insertBefore(node, container.children[index] || null);
    });
  }
  // Numbers ease from the value on screen to the new one, so readings glide rather than jump at each sample.
  const DURATION = 900;
  const tweens = new WeakMap();
  const easing = new Set();
  let easingFrame = null;
  function stepEasing(now) {
    easingFrame = null;
    for (const node of [...easing]) {
      const state = tweens.get(node);
      const k = Math.min(1, (now - state.t0) / DURATION);
      state.shown = state.from + (state.to - state.from) * (1 - Math.pow(1 - k, 3));
      setText(node, state.format(state.shown));
      if (k >= 1) easing.delete(node);
    }
    if (easing.size) easingFrame = requestAnimationFrame(stepEasing);
  }
  function showNumber(node, value, format) {
    if (!node) return;
    const state = tweens.get(node);
    if (!number(value)) {
      easing.delete(node);
      tweens.delete(node);
      setText(node, format(null));
      return;
    }
    if (!frameLoop || !state) {
      easing.delete(node);
      tweens.set(node, {shown: value, from: value, to: value, t0: 0, format});
      setText(node, format(value));
      return;
    }
    if (state.to === value) {
      state.format = format;
      return;
    }
    Object.assign(state, {from: state.shown, to: value, t0: performance.now(), format});
    easing.add(node);
    if (easingFrame === null) easingFrame = requestAnimationFrame(stepEasing);
  }

  function renderRunning() {
    const scenario = advance(latest.scenario || {}, online ? Math.min(5, (Date.now() - receivedAt) / 1000) : 0);
    const plan = latest.plan || {}, traffic = latest.traffic || {};
    setText(document.getElementById('demo-name'), `${online ? '' : 'Last known: '}${scenario.active ? scenario.scenario_name || 'Running test'
      : plan.active ? plan.label || 'Site test plan' : 'Live network observation'}`);
    const phases = scenario.active ? phaseTimeline(scenario) : [];
    setText(document.getElementById('demo-stage'), scenario.active
      ? [scenario.link && `On ${scenario.link}`, phases.length ? `Phase ${(scenario.phase_index || 0) + 1} of ${phases.length}: ${scenario.phase}` : null,
        scenario.step_label].filter(Boolean).join(' · ')
      : showReport(latest) ? `Last test: ${latest.last_test.name} ${latest.last_test.result || 'finished'} · report on the right`
      : plan.result ? `Last site test plan: ${plan.result}` : 'Waiting for the next test');
    const track = document.getElementById('demo-phases');
    track.hidden = !phases.length;
    reconcile(track, phases, (phase, index) => `${index}:${phase.name}`, () => {
      const part = element('span', 'phase');
      part.append(element('i'), element('b'));
      return part;
    }, (part, phase) => {
      part.className = `phase ${phase.state}`;
      part.style.flexBasis = `${phase.share.toFixed(2)}%`;
      part.title = `${phase.name} · ${clock(phase.planned_s)}`;
      part.querySelector('i').style.width = `${phase.fill.toFixed(1)}%`;
      setText(part.querySelector('b'), phase.name);
    });
    document.querySelector('.demo').classList.toggle('paused', Boolean(scenario.active && scenario.paused));
    setText(document.getElementById('demo-time'), scenario.active && phases.length ? [`${clock(scenario.elapsed_s)} of about ${clock(scenario.planned_s)}`,
      scenario.paused ? `Paused by the operator, holding ${scenario.phase}` : null, phaseCountdown(scenario)].filter(Boolean).join(' · ') : '');
    setText(document.getElementById('plan-progress'), planProgress(plan));
    const running = ['starting', 'running'].includes(traffic.status);
    showNumber(document.getElementById('workload-users'), running && number(traffic.users) ? traffic.users : null,
      value => number(value) ? Math.round(value).toLocaleString() : traffic.connected ? 'Idle' : '—');
    setText(document.getElementById('workload-summary'), !traffic.configured ? 'Traffic Simulator not connected'
      : !traffic.connected ? 'Traffic Simulator unavailable'
      : [traffic.label, traffic.activity && `${traffic.activity} activity`, traffic.media_mode && `${traffic.media_mode} voice/video`]
        .filter(Boolean).join(' · ') || (running ? 'Simulated corporate users' : 'No workload running'));
  }

  // Each traffic group under the experience tiles: its own score, its tone and one sentence on how it is performing.
  function groupCards(groups, live) {
    return (Array.isArray(groups) ? groups : []).map(group => {
      const card = element('article', 'subscore ' + (live ? group.tone : 'unknown'));
      const score = live && group.score != null ? group.score + ' / 100' + (group.scoring_complete === false ? ' · provisional' : '') : '—';
      card.append(element('span', '', group.label), element('strong', '', score), element('p', '', group.text));
      return card;
    });
  }

  function renderOutcome() {
    const view = resultView(latest.experience);
    const experience = latest.experience || {};
    const live = online && Boolean(experience.available);
    const scoreText = value => number(value) ? `${Math.round(value)} / 100` : '—';
    const latencyText = value => number(value) ? `${Math.round(value)} ms` : '—';
    const tiles = {
      experience: [experience.experience_score, scoreText],
      success: [experience.success_pct, value => pct(value, 2)],
      interactive: [experience.interactive_p95_ms, latencyText],
    };
    for (const key of ['experience', 'success', 'interactive']) {
      const tile = document.getElementById(`result-${key}`);
      tile.className = `result ${online ? view[key].verdict : 'unknown'}`;
      const [raw, format] = tiles[key];
      showNumber(tile.querySelector('[data-value]'), live ? raw : null, format);
      setText(tile.querySelector('[data-target]'), view[key].target);
    }
    const groupHolder = document.getElementById('experience-groups');
    if (groupHolder) groupHolder.replaceChildren(...groupCards(experience.groups, live));
    const traffic = latest.traffic || {};
    setText(document.getElementById('outcome-window'), number(traffic.requests) ? `Last ${traffic.window_seconds || 60} s · ${traffic.requests.toLocaleString()} transactions` : '');
    const story = latest.story || {};
    setText(document.getElementById('story-status'), online ? story.status || 'Waiting for measurements…' : 'Connection lost · showing the last known state.');
    reconcile(document.getElementById('impacts'), story.impacts || [], item => `${item.impact}|${item.cause}`,
      () => { const card = element('article', 'impact'); card.append(element('strong'), element('span')); return card; },
      (card, item) => {
        card.className = `impact ${item.state} ${item.severity}`;
        setText(card.querySelector('strong'), item.impact);
        setText(card.querySelector('span'), `${item.state === 'resolved' ? 'Resolved · ' : item.state === 'unconfirmed' ? 'No longer confirmed · ' : ''}${item.cause}`);
      });
    const routesBox = document.getElementById('routes');
    reconcile(routesBox, story.routes || [], route => route.label,
      () => { const row = element('div', 'route'); row.append(element('span'), element('strong')); return row; },
      (row, route) => {
        const view = routeView(route);
        row.className = `route ${view.tone}`;
        setText(row.querySelector('span'), view.label);
        setText(row.querySelector('strong'), view.text);
      });
    if (!(story.routes || []).length) routesBox.append(element('p', 'muted', 'Shown once simulated users are running.'));
  }

  function renderReport() {
    const visible = showReport(latest);
    document.getElementById('report').hidden = !visible;
    document.getElementById('live-outcome').hidden = visible;
    if (!visible || !changed('report', latest.last_test)) return;
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

  // The WAN cards: one per link, updated in place. Rates ease to the smoothed value of each new sample.
  const scales = new Map();
  function updatePath(node, link) {
    const field = name => node.querySelector(`[data-field="${name}"]`);
    const health = link.display_health || (link.fault !== 'normal' ? 'failed' : link.health || 'healthy');
    node.className = `path ${({healthy: 'good', congested: 'warn', queue_pressure: 'warn', degraded: 'bad', failed: 'bad'}[health] || 'good')}`;
    setText(field('name'), link.name);
    setText(field('profile'), [link.profile, number(link.download_limit_mbit) && number(link.upload_limit_mbit)
      ? `${format(link.download_limit_mbit)}/${format(link.upload_limit_mbit)} Mbit/s` : null].filter(Boolean).join(' · '));
    setText(field('health'), `${online ? '' : 'Last known · '}${HEALTH[health] || words(health)}`);
    setText(field('impairment'), link.impairment || '');
    const points = histories.get(link.id) || [];
    showNumber(field('down'), online ? smoothedRate(points, 'down_mbps') : null, rateText);
    showNumber(field('up'), online ? smoothedRate(points, 'up_mbps') : null, rateText);
    setText(field('carries'), online ? carriesText(link.carries) : '');
  }
  function renderPaths() {
    const links = latest.links;
    const pages = Math.max(1, Math.ceil(links.length / pageSize()));
    page %= pages;
    setText(document.getElementById('page-number'), pages > 1 ? `Page ${page + 1} of ${pages} · rotates every 12 seconds` : '');
    const container = document.getElementById('paths');
    if (!links.length) {
      container.replaceChildren(element('p', 'empty', 'No WAN paths configured. Configure the lab from the operator interface.'));
      return;
    }
    const visible = links.slice(page * pageSize(), (page + 1) * pageSize());
    reconcile(container, visible, link => link.id,
      () => document.getElementById('path-template').content.firstElementChild.cloneNode(true), updatePath);
    drawSparklines();
  }

  // Each frame the sparklines move on: the time axis runs on from the last sample, so the lines scroll rather than step.
  // The vertical scale eases too, so a new peak does not jump the whole chart.
  function drawSparklines() {
    if (!latest || !latest.links) return;
    const now = latest.timestamp + (Date.now() - receivedAt) / 1000;
    for (const card of document.querySelectorAll('#paths .path')) {
      const id = card.dataset.key;
      const points = histories.get(id) || [];
      const target = Math.max(1, ...points.flatMap(point => [point.down_mbps, point.up_mbps]).filter(number));
      const previous = scales.get(id);
      const scale = number(previous) && frameLoop ? previous + (target - previous) * 0.06 : target;
      scales.set(id, scale);
      card.querySelector('.download').setAttribute('d', chart(points, 'down_mbps', now, scale));
      card.querySelector('.upload').setAttribute('d', chart(points, 'up_mbps', now, scale));
    }
  }

  function renderWaitingMedia(video) {
    const box = document.getElementById('waiting-media');
    if (!box) return;
    const wanted = JSON.stringify(video || null);
    if (box.dataset.source === wanted) return;
    box.dataset.source = wanted;
    const media = waitingMedia(video);
    if (!media) {
      const fallback = document.getElementById('waiting-default');
      box.replaceChildren(fallback ? fallback.content.cloneNode(true) : document.createTextNode(''));
      return;
    }
    const node = document.createElement(media.tag);
    node.src = media.src;
    if (media.tag === 'iframe') {
      node.title = 'Showcase video';
      node.setAttribute('allow', 'autoplay; encrypted-media; picture-in-picture; fullscreen');
      node.allowFullscreen = true;
      node.referrerPolicy = 'strict-origin-when-cross-origin';
    } else if (media.tag === 'video') {
      node.controls = true; node.autoplay = true; node.muted = true; node.loop = true; node.playsInline = true;
    } else {
      node.alt = 'Showcase animation';
    }
    box.replaceChildren(node);
  }

  function renderStage() {
    const mode = stageMode(latest);
    document.body.dataset.mode = mode;
    document.getElementById('stage').hidden = mode === 'live';
    document.getElementById('waiting').hidden = mode !== 'waiting';
    document.getElementById('intro').hidden = mode !== 'intro';
    document.getElementById('plan-summary').hidden = mode !== 'summary';
    if (mode === 'summary') {
      const view = planSummaryView(latest.plan_summary);
      document.getElementById('plan-summary').className = `plan-summary ${view.tone}`;
      setText(document.getElementById('ps-eyebrow'), view.eyebrow);
      setText(document.getElementById('ps-title'), view.title);
      setText(document.getElementById('ps-meta'), view.meta);
      const result = document.getElementById('ps-result');
      result.className = `pill ${view.tone}`;
      setText(result, view.result);
      if (changed('summary-tests', view.tests)) {
        document.getElementById('ps-tests').replaceChildren(...view.tests.map(test => {
          const item = element('li');
          const name = element('strong', '', test.name);
          if (test.where) name.append(element('small', '', test.where));
          item.append(element('span', `pill ${test.tone}`, test.result), name, element('p', '', test.facts));
          for (const line of test.moved) item.append(element('p', 'moved', line));
          return item;
        }));
      }
      if (changed('summary-insights', view.insights)) {
        document.getElementById('ps-insights').replaceChildren(...view.insights.map(item => element('li', item.tone, item.text)));
      }
    }
    if (mode === 'waiting') {
      renderWaitingMedia(latest.showcase_video);
      const view = waitingView(latest.site);
      const site = document.getElementById('waiting-site');
      if (site) site.hidden = !view.site;
      text('waiting-site-title', view.title);
      text('waiting-site-meta', view.meta);
    }
    if (mode === 'intro') {
      const scenario = advance(latest.scenario, online ? Math.min(5, (Date.now() - receivedAt) / 1000) : 0);
      const view = introView(scenario, latest.plan);
      text('intro-where', view.where);
      text('intro-name', view.name);
      text('intro-description', view.description);
      text('intro-seconds', view.countdown);
      text('intro-meta', view.meta);
      if (changed('intro-phases', view.phases)) {
        document.getElementById('intro-phases').replaceChildren(...view.phases.map(phase => {
          const item = element('li');
          const head = element('div', 'intro-phase-head');
          head.append(element('b', '', String(phase.number)), element('strong', '', phase.name), element('span', '', phase.duration));
          item.append(head);
          if (phase.what) item.append(element('p', '', phase.what));
          return item;
        }));
      }
      document.getElementById('intro-checks-block').hidden = !view.checks.length;
      if (changed('intro-checks', view.checks)) {
        document.getElementById('intro-checks').replaceChildren(...view.checks.map(check => element('li', '', check)));
      }
    }
  }

  function render() {
    if (!latest) return;
    renderStage();
    renderSite();
    renderPartner();
    renderRunning();
    renderOutcome();
    renderReport();
    renderPaths();
  }
  // Resizes and other bursts of changes are drawn once per frame.
  let renderFrame = null;
  const scheduleRender = () => {
    if (!frameLoop) return render();
    if (renderFrame === null) renderFrame = requestAnimationFrame(() => { renderFrame = null; render(); });
  };

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
      window.NetEmTime.setFormat(data.time_format);
      receivedAt = Date.now();
      const ids = new Set(data.links.map(link => link.id));
      for (const id of histories.keys()) if (!ids.has(id)) histories.delete(id);
      for (const link of data.links) {
        const points = (histories.get(link.id) || []).filter(point => point.timestamp >= data.timestamp - 120 && point.timestamp <= data.timestamp).slice(-130);
        points.push({timestamp: data.timestamp, down_mbps: link.down_mbps, up_mbps: link.up_mbps});
        histories.set(link.id, points);
      }
      text('updated', `Updated ${window.NetEmTime.clock(data.timestamp)}`);
      connection(true);
    } catch (error) {
      // Retain labelled last-known state while blanking all live rates.
      const now = Date.now() / 1000;
      for (const [id, points] of histories) {
        const recent = points.filter(point => point.timestamp >= now - 120).slice(-130);
        recent.push({timestamp: now, down_mbps: null, up_mbps: null});
        histories.set(id, recent);
      }
      connection(false);
    } finally {
      clearTimeout(timeout);
      // Poll faster while nothing runs, so an announced test appears within a second.
      setTimeout(poll, 1000);
    }
  }
  // The WAN pages turn with a short fade, so the change reads as a turn rather than a cut.
  setInterval(() => {
    if (!latest) return;
    const container = document.getElementById('paths');
    if (!frameLoop || !container) { page += 1; render(); return; }
    container.classList.add('turning');
    setTimeout(() => { page += 1; render(); container.classList.remove('turning'); }, 380);
  }, 12000);
  setInterval(() => { if (latest && online && (latest.scenario || {}).active) { renderStage(); renderRunning(); } }, 1000);
  window.addEventListener('resize', scheduleRender);
  // The sparklines scroll on every frame, whatever arrives from the server.
  if (frameLoop) {
    const frame = () => { drawSparklines(); requestAnimationFrame(frame); };
    requestAnimationFrame(frame);
  }
  poll();
})();
