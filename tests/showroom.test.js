const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync('static/showroom.js', 'utf8');
const context = {window: {}};
vm.createContext(context);
vm.runInContext(source, context);
const ui = context.window.ShowroomUI;
// Values built inside the vm context come from another realm; compare their plain form.
const plain = value => JSON.parse(JSON.stringify(value));

test('unknown rates stay unknown and totals never present partial data as a full measurement', () => {
  assert.equal(ui.format(null), '—');
  assert.equal(ui.format(0), '0');
  assert.equal(ui.total([], 'down_mbps'), null);
  assert.equal(ui.total([{down_mbps: 2}, {down_mbps: null}], 'down_mbps'), null);
  assert.equal(ui.total([{down_mbps: 0}, {down_mbps: 2}], 'down_mbps'), 2);
});

test('chart gaps do not imply zero traffic or join across missing measurements', () => {
  const drawing = ui.chart([{timestamp: 80, down_mbps: 1}, {timestamp: 82, down_mbps: null},
                           {timestamp: 84, down_mbps: 2}], 'down_mbps', 100, 2);
  assert.equal((drawing.match(/M/g) || []).length, 2);
  assert.equal((drawing.match(/L/g) || []).length, 0);
});

test('screen rotates paths, handles untrusted names, blanks disconnected rates, and recovers', async () => {
  const dom = new JSDOM(fs.readFileSync('templates/showroom.html', 'utf8'), {runScripts: 'outside-only'});
  const win = dom.window;
  let nextPoll, rotate, failed = false;
  // The next poll: 1 s while nothing runs, 2 s during a test (5 s is the request timeout).
  win.setTimeout = (fn, ms) => { if (ms !== 5000) nextPoll = fn; return 1; };
  win.clearTimeout = () => {};
  win.setInterval = (fn, ms) => { if (ms === 12000) rotate = fn; };
  const snapshot = {timestamp: 100, scenario: {active: false}, session: {active: false},
    links: Array.from({length: 5}, (_, i) => ({id: `wan${i}`, name: i ? `WAN ${i}` : '<img src=x onerror=alert(1)>',
      profile: 'DIA', fault: 'normal', sla_pass: true, quality: 100, down_mbps: 2, up_mbps: 0,
      traffic_available: true, delay_ms: 10, jitter_ms: 2, loss_pct: 0}))};
  win.fetch = async (url, options) => {
    assert.equal(url, '/api/snapshot');
    assert.equal(options.cache, 'no-store');
    if (failed) throw new Error('offline');
    return {ok: true, json: async () => snapshot};
  };
  win.eval(source);
  await new Promise(resolve => setImmediate(resolve));
  // No session and no test: the waiting screen covers the dashboard, which still renders underneath.
  assert.equal(win.document.body.dataset.mode, 'waiting');
  assert.equal(win.document.getElementById('waiting').hidden, false);
  assert.equal(win.document.getElementById('waiting-site').hidden, true);
  assert.equal(win.document.querySelectorAll('.path').length, 2);
  assert.equal(win.document.querySelector('.path h3').textContent, '<img src=x onerror=alert(1)>');
  assert.equal(win.document.querySelectorAll('.path img').length, 0);
  rotate();
  assert.equal(win.document.querySelector('.path h3').textContent, 'WAN 2');
  rotate();
  assert.equal(win.document.querySelectorAll('.path').length, 1);
  assert.equal(win.document.querySelector('.path h3').textContent, 'WAN 4');
  failed = true;
  await nextPoll();
  assert.equal(win.document.querySelector('[data-field="down"]').textContent, '—');
  assert.match(win.document.querySelector('.badge').textContent, /Last known/);
  assert.match(win.document.getElementById('connection').textContent, /Connection lost/);
  failed = false;
  snapshot.timestamp = 104;
  await nextPoll();
  assert.equal(win.document.querySelector('[data-field="down"]').textContent, '2');
  assert.equal(win.document.getElementById('connection').textContent, 'Live');
  snapshot.links = [];
  await nextPoll();
  assert.match(win.document.querySelector('.empty').textContent, /No WAN paths/);
  dom.window.close();
});

test('chart draws smooth curves through lightly averaged samples', () => {
  const points = [0, 2, 4, 6].map((offset, index) => ({timestamp: 90 + offset, down_mbps: [1, 9, 1, 9][index]}));
  const drawing = ui.chart(points, 'down_mbps', 100, 10);
  assert.equal((drawing.match(/M/g) || []).length, 1);
  assert.equal((drawing.match(/C/g) || []).length, 3);
  assert.equal((drawing.match(/L/g) || []).length, 0);
  // Averaging with neighbours damps a 1-9-1-9 zigzag (36.8 units tall when drawn raw).
  const heights = drawing.trim().split(/(?=C)/).map(part => Number(part.trim().split(' ').pop().split(',')[1]));
  assert.equal(heights.length, 4);
  assert.ok(Math.max(...heights) - Math.min(...heights) < 20);
});

const running = {active: true, scenario_name: 'Brownout', link: 'WAN1', phase_index: 1, phase: 'Brownout',
  phases: [{name: 'Baseline', planned_s: 60}, {name: 'Brownout', planned_s: 120}, {name: 'Recovery', planned_s: 60}],
  planned_s: 240, elapsed_s: 100, phase_elapsed_s: 30, phase_remaining_s: 90, next_phase: 'Recovery', paused: false};

test('phase timeline uses the real phase start and counts down to the next phase', () => {
  const phases = ui.phaseTimeline(running);
  assert.deepEqual(plain(phases.map(phase => [phase.name, phase.state, Math.round(phase.share), Math.round(phase.fill)])),
    [['Baseline', 'done', 25, 100], ['Brownout', 'current', 50, 25], ['Recovery', 'upcoming', 25, 0]]);
  assert.equal(ui.phaseCountdown(running), 'Next: Recovery in 1:30');
  assert.equal(ui.phaseCountdown({...running, paused: true}), 'Recovery follows 1:30 after resuming');
  assert.equal(ui.phaseCountdown({...running, phase_remaining_s: 0}), 'Next: Recovery shortly');
  assert.equal(ui.phaseCountdown({...running, next_phase: null, phase_remaining_s: 5}), 'Ends in 0:05');
  assert.equal(ui.phaseCountdown({active: false}), '');
  const later = ui.advance(running, 2);
  assert.deepEqual(plain([later.elapsed_s, later.phase_elapsed_s, later.phase_remaining_s]), [102, 32, 88]);
  assert.equal(running.elapsed_s, 100);
  assert.equal(ui.advance({...running, paused: true}, 2).elapsed_s, 100);
});

const report = {name: 'Brownout', link: 'WAN1', site: 'Automotive plant', result: 'failed', duration_s: 245, ended_at: 1000,
  steering_target_s: 10, assertions: {passed: 2, total: 3},
  conclusion: ['Brownout failed: 2 of 3 checks passed.', 'Experience fell to 60 during Brownout.'],
  phases: [{name: 'Baseline', reached: true, duration_s: 60, experience_score: 92, success_pct: 100, worst_success_pct: 100,
            interactive_p95_ms: 80, wans: [{label: 'WAN1', down_mbps: 41.2, up_mbps: 5.04, health: 'healthy'}],
            steering: {'File transfers': 'balanced', 'Voice & video': 'balanced'}},
           {name: 'Brownout', reached: true, duration_s: 125, experience_score: 60, success_pct: 99.1, worst_success_pct: 96.5,
            interactive_p95_ms: 410, wans: [{label: 'WAN1', down_mbps: 3, up_mbps: null, health: 'degraded'}],
            steering: {'File transfers': 'stuck_impact', 'Voice & video': 'steered'}},
           {name: 'Recovery', reached: false, steering: {}, wans: []}],
  remediation: [{traffic_class: 'Voice & video', wan: 'WAN1', seconds: 12, within_target: false}]};

test('report rows show each phase, its lowest success and steering in class order', () => {
  const rows = ui.summaryRows(report);
  assert.deepEqual(plain(rows.map(row => [row.name, row.reached, row.duration, row.experience, row.success, row.low, row.interactive])),
    [['Baseline', true, '1:00', '92', '100.0%', '', '80 ms'], ['Brownout', true, '2:05', '60', '99.1%', 'low 96.5%', '410 ms'],
     ['Recovery', false, '', '—', '—', '', '—']]);
  assert.deepEqual(plain(rows[0].traffic.map(item => item.text)), ['WAN1 41↓ 5.0↑']);
  assert.deepEqual(plain(rows[1].traffic.map(item => [item.text, item.health])), [['WAN1 3.0↓ —↑', 'degraded']]);
  assert.deepEqual(plain(rows[1].steering.map(item => [item.label, item.text, item.tone])), [['Voice', 'steered', 'pass'], ['Files', 'users affected', 'fail']]);
  const view = ui.reportView(report);
  assert.equal(view.result, 'Failed');
  assert.equal(view.tone, 'fail');
  assert.match(view.meta, /^on WAN1 · Automotive plant · 4:05 long · 2 of 3 checks passed · finished /);
  assert.deepEqual(plain(view.conclusion), ['Experience fell to 60 during Brownout.']);
  assert.deepEqual(plain(view.remediation), [{text: 'Voice & video moved off WAN1 in 12 s · slower than the 10 s target', tone: 'fail'}]);
  assert.match(view.remediationNote, /kept traffic on an impaired WAN/);
  assert.match(ui.reportView({...report, phases: []}).remediationNote, /Not assessed/);
  assert.equal(ui.showReport({timestamp: 1100, scenario: {active: false}, last_test: report}), true);
  assert.equal(ui.showReport({timestamp: 1100, scenario: {active: true}, last_test: report}), false);
  assert.equal(ui.showReport({timestamp: 1000 + 1801, scenario: {active: false}, last_test: report}), false);
});

test('screen shows the site, the running test with phases, then the report between tests', async () => {
  const dom = new JSDOM(fs.readFileSync('templates/showroom.html', 'utf8'), {runScripts: 'outside-only'});
  const win = dom.window, doc = win.document;
  let nextPoll;
  // The next poll: 1 s while nothing runs, 2 s during a test (5 s is the request timeout).
  win.setTimeout = (fn, ms) => { if (ms !== 5000) nextPoll = fn; return 1; };
  win.clearTimeout = () => {};
  win.setInterval = () => {};
  const snapshot = {timestamp: 1100, links: [], scenario: running, session: {active: true, name: '<b>PoC</b>', site: 'Automotive plant'},
    site: {industry: 'Manufacturing', sub_industry: 'Automotive', function: 'Manufacturing plant', size: 'Large', criticality: 'Business-critical',
      employees: 1200, simulated_users: 960, devices: {ot_device: 300}, targets: {experience_min: 80, success_min_pct: 99, interactive_p95_max_ms: 400, steering_max_s: 30},
      wan_lines: {primary: {preset: 'dia', download_mbit: 1000, upload_mbit: 1000}, backup: {preset: 'dia', download_mbit: 1000, upload_mbit: 1000}}},
    experience: {available: false, targets: {}}, traffic: {}, findings: [], steering: [], plan: {}, last_test: null};
  win.fetch = async () => ({ok: true, json: async () => snapshot});
  win.eval(source);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(doc.getElementById('site-title').textContent, 'Automotive · Manufacturing plant');
  assert.equal(doc.getElementById('session-name').textContent, '<b>PoC</b>');
  assert.equal(doc.querySelectorAll('#session-name b').length, 0);
  assert.equal(doc.querySelectorAll('#demo-phases .phase').length, 3);
  assert.equal(doc.querySelector('#demo-phases .current').textContent, 'Brownout');
  assert.match(doc.getElementById('demo-stage').textContent, /On WAN1 · Phase 2 of 3: Brownout/);
  assert.match(doc.getElementById('demo-time').textContent, /Next: Recovery in 1:(29|30)/);
  assert.equal(doc.getElementById('report').hidden, true);
  snapshot.scenario = {...running, paused: true};
  await nextPoll();
  assert.ok(doc.querySelector('.demo').classList.contains('paused'));
  assert.match(doc.getElementById('demo-time').textContent, /Paused by the operator, holding Brownout/);
  snapshot.scenario = {active: false};
  snapshot.last_test = {...report, name: '<img src=x>'};
  await nextPoll();
  assert.equal(doc.getElementById('report').hidden, false);
  assert.equal(doc.getElementById('live-outcome').hidden, true);
  assert.equal(doc.getElementById('demo-phases').hidden, true);
  assert.equal(doc.getElementById('report-name').textContent, '<img src=x>');
  assert.equal(doc.querySelectorAll('#report img').length, 0);
  assert.equal(doc.querySelectorAll('#report-phases tr').length, 3);
  assert.ok(doc.querySelector('#report-phases tr:last-child').classList.contains('skipped'));
  assert.match(doc.getElementById('report-remediation').textContent, /slower than the 10 s target/);
  assert.match(doc.getElementById('demo-stage').textContent, /report on the right/);
  dom.window.close();
});

test('calm view helpers smooth rates and phrase carried traffic, routes and plan progress', () => {
  const points = [3, 5, null, 7, 9, 11, 13].map((value, index) => ({timestamp: index, down_mbps: value}));
  assert.equal(ui.smoothedRate(points, 'down_mbps'), 9);
  assert.equal(ui.smoothedRate([], 'down_mbps'), null);
  assert.equal(ui.rateText(47.31), '47');
  assert.equal(ui.rateText(4.26), '4.3');
  assert.equal(ui.rateText(null), '—');
  assert.equal(ui.carriesText(['voice and video', 'web and apps', 'file transfers']), 'Carries voice and video, web and apps and file transfers');
  assert.equal(ui.carriesText([]), 'No simulated users on this WAN');
  assert.equal(ui.carriesText(null), '');
  assert.deepEqual(plain(ui.routeView({label: 'Voice and video', wan: 'WAN2', state: 'good'})), {label: 'Voice and video', text: 'WAN2', tone: 'pass'});
  assert.equal(ui.routeView({label: 'Files', wan: 'WAN1', state: 'warn'}).tone, 'warn');
  assert.equal(ui.routeView({label: 'Files', wan: null, state: 'unknown'}).text, 'Not identified');
  assert.equal(ui.routeView({label: 'Files', wan: null, state: 'idle'}).text, 'No traffic');
  assert.equal(ui.planProgress({active: true, tests: [{status: 'passed'}, {status: 'running'}, {status: 'pending'}]}), 'Site test plan · test 2 of 3');
  assert.equal(ui.planProgress({active: false, tests: []}), '');
});

test('the live view shows the story, impacts with causes and where traffic goes', async () => {
  const dom = new JSDOM(fs.readFileSync('templates/showroom.html', 'utf8'), {runScripts: 'outside-only'});
  const win = dom.window, doc = win.document;
  win.setTimeout = () => 1;
  win.clearTimeout = () => {};
  win.setInterval = () => {};
  const snapshot = {timestamp: 100, scenario: {active: false}, session: {active: false}, plan: {}, last_test: null,
    experience: {available: true, targets: {}, verdicts: {}}, traffic: {},
    links: [{id: 'wan1', name: 'WAN1', profile: 'DIA', fault: 'normal', display_health: 'degraded', download_limit_mbit: 100,
             upload_limit_mbit: 100, impairment: 'Impaired to 60% quality: 45 ms delay, 2% loss', down_mbps: 47.3, up_mbps: 44.4,
             carries: ['file transfers']}],
    story: {status: 'Users are affected. WAN1 is degraded. The appliance moved voice and video off WAN1 in 12 s.',
            impacts: [{impact: 'Voice and video: breaking up', cause: '2% packet loss on WAN1', state: 'active', severity: 'bad'},
                      {impact: '<img src=x>', cause: 'WAN1 is full', state: 'resolved', severity: 'warn'}],
            routes: [{label: 'Voice and video', wan: 'WAN2', state: 'good'}, {label: 'File transfers', wan: 'WAN1', state: 'warn'}]}};
  win.fetch = async () => ({ok: true, json: async () => snapshot});
  win.eval(source);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(doc.getElementById('story-status').textContent, /^Users are affected/);
  const impacts = [...doc.querySelectorAll('#impacts .impact')];
  assert.equal(impacts.length, 2);
  assert.equal(impacts[0].querySelector('strong').textContent, 'Voice and video: breaking up');
  assert.equal(impacts[0].querySelector('span').textContent, '2% packet loss on WAN1');
  assert.equal(impacts[1].querySelector('span').textContent, 'Resolved · WAN1 is full');
  assert.equal(doc.querySelectorAll('#impacts img').length, 0);
  assert.deepEqual([...doc.querySelectorAll('#routes .route')].map(row => [row.className, row.textContent]),
    [['route pass', 'Voice and videoWAN2'], ['route warn', 'File transfersWAN1']]);
  const card = doc.querySelector('.path');
  assert.ok(card.classList.contains('bad'));
  assert.equal(card.querySelector('[data-field="health"]').textContent, 'Degraded');
  assert.equal(card.querySelector('[data-field="profile"]').textContent, 'DIA · 100/100 Mbit/s');
  assert.equal(card.querySelector('[data-field="impairment"]').textContent, 'Impaired to 60% quality: 45 ms delay, 2% loss');
  assert.equal(card.querySelector('[data-field="down"]').textContent, '47');
  assert.equal(card.querySelector('[data-field="carries"]').textContent, 'Carries file transfers');
  dom.window.close();
});

test('the screen waits before the session, announces each test, then shows the live dashboard', () => {
  const report = {name: 'Brownout', ended_at: 90};
  const data = (scenario, session = false, extra = {}) => Object.assign({timestamp: 100, scenario, session: {active: session}}, extra);
  assert.equal(ui.stageMode(data({active: false})), 'waiting');
  assert.equal(ui.stageMode(data({active: false}, true)), 'live');
  assert.equal(ui.stageMode(data({active: true, intro: true})), 'intro');
  assert.equal(ui.stageMode(data({active: true, intro: true}, true)), 'intro');
  assert.equal(ui.stageMode(data({active: true})), 'live');
  // A recent report and a running site plan count as activity; the waiting screen does not hide them.
  assert.equal(ui.stageMode(data({active: false}, false, {last_test: report})), 'live');
  assert.equal(ui.stageMode(data({active: false}, false, {last_test: {name: 'Old', ended_at: -5000}})), 'waiting');
  assert.equal(ui.stageMode(data({active: false}, false, {plan: {active: true}})), 'live');
  assert.deepEqual(plain(ui.waitingView(null)), {site: false, title: '', meta: ''});
  assert.equal(ui.waitingView({sub_industry: 'Automotive', function: 'Manufacturing plant', industry: 'Manufacturing',
    size: 'Large', criticality: 'Business-critical'}).title, 'Automotive · Manufacturing plant');
});

test('an announced test shows its plan, its checks and a countdown that ticks locally', () => {
  const scenario = {active: true, intro: true, starts_in_s: 4.2, link: 'WAN1', planned_s: 245,
    scenario_name: 'Automotive plant: Primary WAN outage', description: 'The primary fails.',
    phases: [{name: 'Warm-up', planned_s: 60, what: 'Start site workload'}, {name: 'Outage', planned_s: 75, what: null}],
    checks: ['Voice & video steered within 30 s']};
  const plan = {active: true, label: 'Automotive plant', tests: [{status: 'passed'}, {status: 'running'}, {status: 'pending'}]};
  const view = ui.introView(scenario, plan);
  assert.equal(view.where, 'Coming up on WAN1');
  assert.equal(view.name, 'Primary WAN outage');
  assert.equal(view.countdown, '5');
  assert.deepEqual(plain(view.phases), [{number: 1, name: 'Warm-up', duration: '1:00', what: 'Start site workload'},
    {number: 2, name: 'Outage', duration: '1:15', what: ''}]);
  assert.deepEqual(plain(view.checks), ['Voice & video steered within 30 s']);
  assert.equal(view.meta, 'About 4:05 long · Site test plan · test 2 of 3');
  // Outside a plan the full name stays; at zero the countdown says the test is starting.
  const single = ui.introView(Object.assign({}, scenario, {starts_in_s: 0}), {active: false});
  assert.equal(single.name, 'Automotive plant: Primary WAN outage');
  assert.equal(single.countdown, 'Now');
  assert.equal(ui.advance(scenario, 3).starts_in_s.toFixed(1), '1.2');
  assert.equal(ui.advance(scenario, 9).starts_in_s, 0);
});

test('after a run sequence the results replace the dashboard until the next test', () => {
  const summary = {label: 'Automotive plant', result: 'failed', title: '3 of 5 tests passed', ended_at: 100, duration_s: 1295,
    checks: {passed: 4, total: 5},
    tests: [{name: 'Primary WAN outage', result: 'failed', link: 'WAN1', measured: true, checks: {passed: 1, total: 2},
      experience: 92, experience_low: 71, low_phase: 'Outage', success_low: 97.4, p95_max: 620,
      moved: [{traffic_class: 'Voice & video', wan: 'WAN1', seconds: 9, within_target: true}]},
      {name: 'Baseline experience', result: 'passed', link: 'WAN1', measured: true, checks: {passed: 3, total: 3},
        experience: 92, experience_low: 92, low_phase: 'Steady state', success_low: 99.98, p95_max: 140, moved: []},
      {name: 'Flaky primary WAN', result: 'skipped', measured: false, checks: {passed: 0, total: 0}, moved: []}],
    insights: [{tone: 'fail', text: 'Primary WAN outage missed: Request success ≥ 99%.'}, {tone: 'odd', text: 'Unknown tone'}]};
  const data = (extra = {}) => Object.assign({timestamp: 160, scenario: {active: false}, session: {active: false}, plan: {active: false}, plan_summary: summary}, extra);
  assert.equal(ui.stageMode(data()), 'summary');
  assert.equal(ui.stageMode(data({session: {active: true}})), 'summary');
  assert.equal(ui.stageMode(data({scenario: {active: true, intro: true}})), 'intro');
  assert.equal(ui.stageMode(data({scenario: {active: true}})), 'live');
  assert.equal(ui.stageMode(data({plan: {active: true}})), 'live');
  assert.equal(ui.stageMode(data({timestamp: 100 + 1800})), 'waiting');
  const view = ui.planSummaryView(summary);
  assert.equal(view.eyebrow, 'Site test plan results · Automotive plant');
  assert.equal(view.title, '3 of 5 tests passed');
  assert.equal(view.result, 'Failed');
  assert.equal(view.tone, 'fail');
  assert.match(view.meta, /^21:35 in total · 4 of 5 checks met · finished /);
  assert.deepEqual(plain(view.tests[0]), {name: 'Primary WAN outage', result: 'Failed', tone: 'fail', where: 'WAN1 · 1/2 checks',
    facts: 'Experience 92 → 71 in Outage · lowest success 97.4% · slowest response 620 ms', moved: ['Voice & video moved off WAN1 in 9 s']});
  // No drop is not shown as one, and above 99% the second decimal stays.
  assert.equal(view.tests[1].facts, 'Experience 92 · lowest success 99.98% · slowest response 140 ms');
  assert.equal(view.tests[2].facts, 'Not run: the plan stopped before this test');
  assert.equal(view.tests[2].tone, 'unknown');
  assert.deepEqual(plain(view.insights.map(item => item.tone)), ['fail', 'pass']);
});
