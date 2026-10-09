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
  win.setInterval = fn => { rotate = fn; };
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
  assert.equal(win.document.querySelector('h3').textContent, '<img src=x onerror=alert(1)>');
  assert.equal(win.document.querySelectorAll('.path img').length, 0);
  assert.match(win.document.getElementById('total-down').textContent, /^10 /);
  rotate();
  assert.equal(win.document.querySelector('h3').textContent, 'WAN 2');
  rotate();
  assert.equal(win.document.querySelectorAll('.path').length, 1);
  assert.equal(win.document.querySelector('h3').textContent, 'WAN 4');
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
