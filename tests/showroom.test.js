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
  win.setTimeout = (fn, ms) => { if (ms === 2000) nextPoll = fn; return 1; };
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
  assert.equal(win.document.querySelectorAll('.path').length, 2);
  assert.equal(win.document.querySelector('.path h3').textContent, '<img src=x onerror=alert(1)>');
  assert.equal(win.document.querySelectorAll('.path img').length, 0);
  assert.match(win.document.getElementById('total-down').textContent, /^10 /);
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
  win.setTimeout = (fn, ms) => { if (ms === 2000) nextPoll = fn; return 1; };
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
  assert.match(doc.getElementById('site-lines').textContent, /Primary DIA 1000\/1000 · Backup DIA 1000\/1000/);
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
