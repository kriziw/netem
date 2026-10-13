const {test, afterEach} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const windows = [];
afterEach(() => windows.splice(0).forEach(dom => dom.window.close()));
const settle = async () => { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); };
const configuration = `<div data-scenario-configuration><section class="test-card"><div class="test-card-title">Brownout</div><p>Progressive impairment</p><form method="post" action="/lab/scenario/start"><input type="hidden" name="integration_csrf" value="csrf-token"><input type="hidden" name="scenario_id" value="progressive_brownout"><input type="hidden" name="return_to" value="overview"><select name="link_id"><option value="wan1">WAN1</option><option value="wan2">WAN2</option></select><input name="length_min" value="3"><input type="checkbox" name="appliance_override" value="1"><input name="appliance_override_reason"><button type="submit">Run</button></form></section></div>`;
function page(active = false) {
  return `<html><head><title>Command Center</title></head><body><main class="page" data-ui-version="v1"><svg><path id="live-chart" data-chart="wan1-down" d="M0 1"></path></svg><a data-scenario-modal="/tests/scenario/progressive_brownout" href="/tests?scenario=progressive_brownout">Degrade a WAN</a>${active ? '<div class="flash success">Started scenario</div><section id="running-test" data-scenario-id="progressive_brownout">Brownout running</section>' : ''}</main><template id="page-scripts"></template></body></html>`;
}
function setup() {
  const dom = new JSDOM(page().replace('</body>', `<dialog id="scenario-configuration-dialog" aria-labelledby="scenario-dialog-title"><h2 id="scenario-dialog-title"></h2><button data-scenario-close>Close</button><div data-scenario-feedback></div><div data-scenario-content></div></dialog></body>`), {url: 'https://netem.test/', runScripts: 'outside-only'});
  windows.push(dom);
  const w = dom.window;
  w.setTimeout = () => 1;
  w.scrollTo = () => {};
  w.CSS = {escape: value => value};
  const dialog = w.document.querySelector('dialog');
  dialog.showModal = () => dialog.setAttribute('open', '');
  dialog.close = () => dialog.removeAttribute('open');
  w.eval(fs.readFileSync('static/actions.js', 'utf8'));
  w.eval(fs.readFileSync('static/scenario-dialog.js', 'utf8'));
  w.NetEmActions.mount();
  return {dom, w, dialog};
}

test('scenario configuration opens without navigating or replacing live charts', async () => {
  const {w, dialog} = setup();
  w.fetch = async () => ({ok: true, text: async () => configuration});
  const chart = w.document.getElementById('live-chart');
  w.document.querySelector('[data-scenario-modal]').click(); await settle();
  assert.equal(dialog.open, true);
  assert.equal(dialog.querySelector('#scenario-dialog-title').textContent, 'Brownout');
  assert.equal(dialog.querySelector('select').value, 'wan1');
  assert.equal(w.document.getElementById('live-chart'), chart);
  assert.equal(w.location.href, 'https://netem.test/');
});

test('starting submits selections once, closes the popup and updates the running test within the app', async () => {
  const {w, dialog} = setup(); const requests = [];
  w.fetch = async (url, options = {}) => {
    requests.push({url, options});
    return {ok: true, url: 'https://netem.test/', text: async () => String(url).includes('/tests/scenario/') ? configuration : page(true)};
  };
  const chart = w.document.getElementById('live-chart');
  w.document.querySelector('[data-scenario-modal]').click(); await settle();
  dialog.querySelector('select').value = 'wan2';
  dialog.querySelector('[name="length_min"]').value = '7.5';
  dialog.querySelector('[name="appliance_override"]').checked = true;
  dialog.querySelector('[name="appliance_override_reason"]').value = 'Known lab license restriction';
  const form = dialog.querySelector('form');
  form.dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true}));
  form.dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true})); await settle();
  const posts = requests.filter(request => request.options.method === 'POST');
  assert.equal(posts.length, 1);
  const data = posts[0].options.body;
  for (const [name, value] of Object.entries({integration_csrf: 'csrf-token', scenario_id: 'progressive_brownout',
    link_id: 'wan2', length_min: '7.5', return_to: 'overview', appliance_override: '1', appliance_override_reason: 'Known lab license restriction'})) assert.equal(data.get(name), value);
  assert.equal(dialog.open, false);
  assert.equal(w.document.querySelector('#running-test').textContent, 'Brownout running');
  assert.equal(w.document.getElementById('live-chart'), chart);
  assert.equal(w.location.pathname, '/');
});

test('a refused start keeps the popup, edited settings and actionable error', async () => {
  const {w, dialog} = setup();
  w.fetch = async (url, options = {}) => ({ok: true, text: async () => options.method === 'POST' ? '<div class="flash error">Required appliance license is expired.</div>' : configuration});
  w.document.querySelector('[data-scenario-modal]').click(); await settle();
  dialog.querySelector('select').value = 'wan2'; dialog.querySelector('[name="length_min"]').value = '8';
  dialog.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true})); await settle();
  assert.equal(dialog.open, true);
  assert.equal(dialog.querySelector('select').value, 'wan2');
  assert.equal(dialog.querySelector('[name="length_min"]').value, '8');
  assert.match(dialog.querySelector('[data-scenario-feedback]').textContent, /license is expired/);
  assert.equal(dialog.querySelector('button[type="submit"]').disabled, false);
  assert.equal(w.location.pathname, '/');
});

test('network failure keeps configuration and warns that the start may have applied', async () => {
  const {w, dialog} = setup();
  w.fetch = async (url, options = {}) => {
    if (options.method === 'POST') throw new w.TypeError('Connection lost');
    return {ok: true, text: async () => configuration};
  };
  w.document.querySelector('[data-scenario-modal]').click(); await settle();
  dialog.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true})); await settle();
  assert.equal(dialog.open, true);
  assert.match(dialog.querySelector('[data-scenario-feedback]').textContent, /start may have applied/);
  assert.equal(dialog.querySelector('button[type="submit"]').disabled, false);
});

test('closing ignores late configuration responses and restores focus', async () => {
  const {w, dialog} = setup(); let resolve;
  w.fetch = () => new Promise(done => { resolve = done; });
  const launcher = w.document.querySelector('[data-scenario-modal]'); launcher.click();
  dialog.querySelector('[data-scenario-close]').click();
  resolve({ok: true, text: async () => configuration}); await settle();
  assert.equal(dialog.open, false);
  assert.equal(dialog.querySelector('[data-scenario-content]').children.length, 0);
  assert.equal(w.document.activeElement, launcher);
});

test('in-app navigation closes scenario controls rather than leaving them over another page', async () => {
  const {w, dialog} = setup();
  w.fetch = async () => ({ok: true, text: async () => configuration});
  w.document.querySelector('[data-scenario-modal]').click(); await settle();
  w.document.dispatchEvent(new w.CustomEvent('netem:navigated'));
  assert.equal(dialog.open, false);
});

test('failed loading is visible and modified clicks retain the Tests-page fallback', async () => {
  const {w, dialog} = setup();
  w.fetch = async () => ({ok: false, status: 503});
  const launcher = w.document.querySelector('[data-scenario-modal]'); launcher.click(); await settle();
  assert.match(dialog.querySelector('[data-scenario-feedback]').textContent, /could not be loaded/);
  dialog.querySelector('[data-scenario-close]').click();
  const click = new w.MouseEvent('click', {bubbles: true, cancelable: true, ctrlKey: true});
  w.document.addEventListener('click', event => { assert.equal(event.defaultPrevented, false); event.preventDefault(); }, {once: true});
  launcher.dispatchEvent(click);
  assert.equal(dialog.open, false);
});

test('a start without a lab session offers a session or acknowledges the warning and runs the test', async () => {
  const {w, dialog} = setup(); const requests = [];
  const warning = '<section class="card" id="no-session-warning"><a class="btn" href="/sessions">Start a session</a><form method="post" action="/lab/scenario/start"><input type="hidden" name="integration_csrf" value="csrf-token"><input type="hidden" name="scenario_id" value="progressive_brownout"><input type="hidden" name="link_id" value="wan2"><input type="hidden" name="no_session_ack" value="1"><button type="submit">Acknowledge and run anyway</button></form></section>';
  w.fetch = async (url, options = {}) => {
    requests.push({url, options});
    if (options.method !== 'POST') return {ok: true, text: async () => String(url).includes('/tests/scenario/') ? configuration : page(true)};
    if (requests.filter(request => request.options.method === 'POST').length === 1) return {ok: false, status: 409, url, text: async () => warning};
    return {ok: true, url: 'https://netem.test/', text: async () => page(true)};
  };
  w.document.querySelector('[data-scenario-modal]').click(); await settle();
  dialog.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true})); await settle();
  assert.equal(dialog.open, true);
  assert.ok(dialog.querySelector('#no-session-warning a[href="/sessions"]'));
  assert.equal(dialog.querySelector('select'), null);
  assert.equal(w.document.getElementById('scenario-dialog-title').textContent, 'No session running');
  dialog.querySelector('input[name="no_session_ack"]').form.dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true})); await settle();
  const posts = requests.filter(request => request.options.method === 'POST');
  assert.equal(posts.length, 2);
  assert.equal(posts[1].options.body.get('no_session_ack'), '1');
  assert.equal(posts[1].options.body.get('scenario_id'), 'progressive_brownout');
  assert.equal(dialog.open, false);
  assert.equal(w.document.querySelector('#running-test').textContent, 'Brownout running');
});
