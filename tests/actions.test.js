const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync('static/actions.js', 'utf8');

// A page as base.html renders it: live top-bar pills, the impairment banner slot and the page.
const page = ({version, pills = '', banner = '', body = '', timeFormat = '24h'}) => `<!doctype html><html><head><title>Tests</title></head><body data-time-format="${timeFormat}">
  <header><div class="top-actions"><span class="top-status" id="top-status">${pills}</span><button id="activity-toggle">Activity</button></div></header>
  <div id="global-alert-slot">${banner}</div>
  <main class="page" data-ui-version="${version}">${body}</main>
  <template id="page-scripts"></template></body></html>`;

function open(first, next, {visible = true} = {}) {
  const dom = new JSDOM(page(first), {url: 'http://netem.test/tests', runScripts: 'outside-only', pretendToBeVisual: true});
  const win = dom.window;
  const calls = [];
  let loop = null;
  let serverVersion = first.version;
  win.setTimeout = fn => { loop = fn; return 1; };
  win.scrollTo = () => {};
  if (!visible) Object.defineProperty(win.document, 'visibilityState', {get: () => 'hidden', configurable: true});
  win.fetch = async url => {
    calls.push(String(url));
    if (String(url).endsWith('/api/v1/ui-version')) return {ok: true, json: async () => ({version: serverVersion})};
    return {ok: true, url: 'http://netem.test/tests', text: async () => page(next)};
  };
  win.eval(fs.readFileSync('static/time-format.js', 'utf8'));
  win.eval(source);
  win.NetEmActions.mount();
  return {win, doc: win.document, calls, tick: async () => { await loop(); }, change: version => { serverVersion = version; }};
}

const next = {version: 'v2', timeFormat: '12h', pills: '<span class="top-pill scenario">Scenario · Brownout</span>',
  banner: '<div class="global-alert">Active runtime impairment</div>',
  body: '<form method="post" action="/x"><input name="length_min" value="5"></form>'};

test('expanded Command Center details survive a live refresh', async () => {
  const body = '<details class="command-detail" id="topology"><summary>Topology</summary>Live paths</details>' +
    '<details class="command-detail" id="history"><summary>History</summary>Live chart</details>';
  const screen = open({version: 'v1', body}, {version: 'v2', body});
  screen.doc.getElementById('topology').open = true;
  await screen.win.NetEmActions.refresh();
  assert.equal(screen.doc.getElementById('topology').open, true);
  assert.equal(screen.doc.getElementById('history').open, false);
  screen.win.close();
});

test('a page refreshes in place when the lab changes, top bar and banner included', async () => {
  const page = open({version: 'v1', body: '<form method="post" action="/x"><input name="length_min" value="5"></form>'}, next);
  await page.tick();
  assert.equal(page.doc.querySelector('main.page').dataset.uiVersion, 'v1');
  assert.equal(page.calls.filter(url => url.endsWith('/ui-version')).length, 1);
  page.change('v2');
  await page.tick();
  assert.equal(page.doc.querySelector('main.page').dataset.uiVersion, 'v2');
  assert.equal(page.doc.getElementById('top-status').textContent, 'Scenario · Brownout');
  assert.equal(page.doc.querySelector('#global-alert-slot .global-alert').textContent, 'Active runtime impairment');
});

test('unsaved edits are kept: the page offers the refresh instead', async () => {
  const page = open({version: 'v1', body: '<form method="post" action="/x"><input name="length_min" value="5"></form>'}, next);
  const field = page.doc.querySelector('input[name="length_min"]');
  field.value = '9';
  field.dispatchEvent(new page.win.Event('input', {bubbles: true}));
  page.change('v2');
  await page.tick();
  assert.equal(page.doc.querySelector('main.page').dataset.uiVersion, 'v1');
  assert.equal(page.doc.querySelector('input[name="length_min"]').value, '9');
  assert.match(page.doc.getElementById('stale-notice').textContent, /The lab changed since this page loaded/);
  await page.tick();
  assert.equal(page.doc.querySelectorAll('#stale-notice').length, 1);
  page.doc.querySelector('#stale-notice button').click();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.doc.querySelector('main.page').dataset.uiVersion, 'v2');
  assert.equal(page.doc.getElementById('stale-notice'), null);
});

test('a background tab is not checked, and catches up when shown', async () => {
  const page = open({version: 'v1'}, next, {visible: false});
  page.change('v2');
  await page.tick();
  assert.equal(page.calls.length, 0);
  Object.defineProperty(page.doc, 'visibilityState', {get: () => 'visible', configurable: true});
  page.doc.dispatchEvent(new page.win.Event('visibilitychange'));
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(page.doc.querySelector('main.page').dataset.uiVersion, 'v2');
});

test('action results stay visible across the refresh they cause', () => {
  const page = open({version: 'v1'}, next);
  page.win.NetEmActions.notify('Impairment applied to WAN1.');
  page.doc.querySelector('main.page').replaceWith(page.doc.createElement('main'));
  const toast = page.doc.querySelector('#action-toasts .flash.success');
  assert.equal(toast.textContent, 'Impairment applied to WAN1.');
  assert.equal(page.doc.getElementById('action-toasts').getAttribute('aria-live'), 'polite');
});

test('a saved clock format applies to the open page at once', async () => {
  const page = open({version: 'v1', body: '<form method="post" action="/x"><input name="length_min" value="5"></form>'}, next);
  assert.equal(page.win.NetEmTime.format(), '24h');
  page.change('v2');
  await page.tick();
  assert.equal(page.doc.body.dataset.timeFormat, '12h');
  assert.equal(page.win.NetEmTime.format(), '12h');
  assert.equal(page.win.NetEmTime.clock(new Date(2026, 9, 13, 14, 5)), '2:05:00 PM');
});

test('a slow background refresh does not block navigation or overwrite its destination', async () => {
  const screen = open({version: 'v1'}, next);
  let finish;
  let refreshSignal;
  screen.win.fetch = (url, options) => {
    if (String(url).endsWith('/tests')) {
      refreshSignal = options.signal;
      return new Promise(resolve => { finish = resolve; });
    }
    return Promise.resolve({ok: true, url: 'http://netem.test/sessions', text: async () => page({version: 'sessions'})});
  };
  const refreshing = screen.win.NetEmActions.refresh();
  await screen.win.NetEmActions.navigate('/sessions');
  assert.equal(refreshSignal.aborted, true);
  assert.equal(screen.win.location.pathname, '/sessions');
  assert.equal(screen.doc.querySelector('main.page').dataset.uiVersion, 'sessions');
  assert.doesNotMatch(screen.doc.body.textContent, /An action is still completing/);
  finish({ok: true, url: 'http://netem.test/tests', text: async () => page(next)});
  await refreshing;
  assert.equal(screen.doc.querySelector('main.page').dataset.uiVersion, 'sessions');
  screen.win.close();
});

test('edits made during a background refresh are retained', async () => {
  const screen = open({version: 'v1', body: '<form method="post"><input name="name" value="original"></form>'}, next);
  let finish;
  screen.win.fetch = () => new Promise(resolve => { finish = resolve; });
  const refreshing = screen.win.NetEmActions.refresh();
  const input = screen.doc.querySelector('input');
  input.value = 'my edit';
  input.dispatchEvent(new screen.win.Event('input', {bubbles: true}));
  finish({ok: true, url: 'http://netem.test/tests', text: async () => page(next)});
  await refreshing;
  assert.equal(screen.doc.querySelector('input').value, 'my edit');
  assert.equal(screen.doc.querySelector('main.page').dataset.uiVersion, 'v1');
  screen.win.close();
});

test('a real action still prevents duplicate submission and navigation until acknowledgement', async () => {
  const screen = open({version: 'v1', body: '<form method="post" action="/change"><button>Apply</button></form>'}, next);
  let finish;
  let posts = 0;
  screen.win.fetch = () => { posts++; return new Promise(resolve => { finish = resolve; }); };
  const form = screen.doc.querySelector('form');
  form.dispatchEvent(new screen.win.Event('submit', {bubbles: true, cancelable: true}));
  form.dispatchEvent(new screen.win.Event('submit', {bubbles: true, cancelable: true}));
  await screen.win.NetEmActions.navigate('/sessions');
  assert.equal(posts, 1);
  assert.match(screen.doc.body.textContent, /An action is still completing/);
  finish({ok: true, url: 'http://netem.test/tests', text: async () => page(next)});
  await new Promise(resolve => setImmediate(resolve));
  screen.win.close();
});
