const {test, afterEach} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync('static/actions.js', 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const settle = async () => { await tick(); await tick(); };
const windows = [];
afterEach(() => windows.splice(0).forEach(dom => dom.window.close()));
async function waitFor(condition) {
  for (let attempt = 0; attempt < 40 && !condition(); attempt++) await new Promise(resolve => setTimeout(resolve, 5));
  assert.ok(condition(), 'Expected browser history to settle');
}
function html(name, body = '', script = '') {
  return `<html><head><title>${name}</title></head><body><aside class="sidebar"><a class="nav-link ${name === 'Overview' ? 'active' : ''}" href="/">Overview</a><a class="nav-link ${name === 'Tests' ? 'active' : ''}" href="/tests">Tests</a></aside><header><div class="breadcrumb">${name}</div><span id="page-guide">${name} guide</span><span id="top-status"></span></header><main class="page" data-ui-version="v1"><h1>${name}</h1>${body}</main><template id="page-scripts"><script>${script}</script></template><div id="drawer-backdrop" class="drawer-backdrop"></div></body></html>`;
}
function setup(body = '', script = '') {
  const dom = new JSDOM(html('Overview', body, script), {url: 'https://netem.test/', runScripts: 'outside-only', pretendToBeVisual: true});
  windows.push(dom);
  const w = dom.window;
  w.scrollTo = () => {};
  w.eval(source);
  w.NetEmActions.mount();
  w.fetch = async url => ({ok: true, url: String(url), text: async () => html(new URL(url).pathname === '/tests' ? 'Tests' : 'Overview')});
  return {dom, w, doc: w.document};
}

test('internal links preserve the shell, update navigation and support Back and Forward', async () => {
  const {dom, w, doc} = setup();
  const sidebar = doc.querySelector('.sidebar'), header = doc.querySelector('header');
  doc.querySelector('a[href="/tests"]').click();
  await settle();
  assert.equal(w.location.pathname, '/tests');
  assert.equal(doc.querySelector('.sidebar'), sidebar);
  assert.equal(doc.querySelector('header'), header);
  assert.equal(doc.querySelector('.nav-link.active').textContent, 'Tests');
  assert.equal(doc.querySelector('.breadcrumb').textContent, 'Tests');
  assert.equal(doc.querySelector('#page-guide').textContent, 'Tests guide');
  assert.equal(doc.querySelector('h1').textContent, 'Tests');
  w.history.back();
  await waitFor(() => w.location.pathname === '/' && doc.querySelector('h1').textContent === 'Overview');
  assert.equal(w.location.pathname, '/');
  assert.equal(doc.querySelector('h1').textContent, 'Overview');
  w.history.forward();
  await waitFor(() => w.location.pathname === '/tests' && doc.querySelector('h1').textContent === 'Tests');
  assert.equal(w.location.pathname, '/tests');
  assert.equal(doc.querySelector('h1').textContent, 'Tests');
  dom.window.close();
});

test('unsaved edits survive a cancelled navigation and can be discarded explicitly', async () => {
  const {dom, w, doc} = setup('<form><input name="name" value="Original"></form>');
  const input = doc.querySelector('input');
  input.value = 'Unsaved';
  input.dispatchEvent(new w.Event('input', {bubbles: true}));
  let prompts = 0, requests = 0;
  w.confirm = () => { prompts++; return false; };
  const fetch = w.fetch; w.fetch = url => { requests++; return fetch(url); };
  doc.querySelector('a[href="/tests"]').click(); await settle();
  assert.equal(requests, 0);
  assert.equal(input.value, 'Unsaved');
  assert.equal(w.location.pathname, '/');
  w.confirm = () => { prompts++; return true; };
  doc.querySelector('a[href="/tests"]').click(); await settle();
  assert.equal(prompts, 2);
  assert.equal(w.location.pathname, '/tests');
  dom.window.close();
});

test('the unsaved-edit prompt stays inside the app and respects both choices', async () => {
  const {dom, w, doc} = setup('<form><input name="name" value="Original"></form>');
  const dialog = doc.createElement('dialog');
  dialog.id = 'navigation-confirm-dialog';
  dialog.showModal = () => dialog.setAttribute('open', '');
  dialog.close = choice => {
    dialog.returnValue = choice;
    dialog.removeAttribute('open');
    dialog.dispatchEvent(new w.Event('close'));
  };
  doc.body.append(dialog);
  w.confirm = () => { throw new Error('A native prompt must not be used'); };
  const input = doc.querySelector('input'); input.value = 'Unsaved';
  input.dispatchEvent(new w.Event('input', {bubbles: true}));
  const cancelled = w.NetEmActions.navigate('/tests');
  assert.equal(dialog.open, true);
  dialog.close('stay'); await cancelled;
  assert.equal(input.value, 'Unsaved');
  assert.equal(w.location.pathname, '/');
  const accepted = w.NetEmActions.navigate('/tests');
  dialog.close('leave'); await accepted;
  assert.equal(w.location.pathname, '/tests');
  dom.window.close();
});

test('cancelling Back restores the current history entry and its edits', async () => {
  const {dom, w, doc} = setup();
  w.fetch = async url => ({ok: true, url: String(url), text: async () => html('Tests', '<form><input name="duration" value="3"></form>')});
  await w.NetEmActions.navigate('/tests');
  const input = doc.querySelector('input'); input.value = '9'; input.dispatchEvent(new w.Event('input', {bubbles: true}));
  w.confirm = () => false;
  const traversed = new Promise(resolve => w.addEventListener('popstate', resolve, {once: true}));
  w.history.back(); await traversed;
  await waitFor(() => w.location.pathname === '/tests');
  assert.equal(w.location.pathname, '/tests');
  assert.equal(doc.querySelector('input').value, '9');
  dom.window.close();
});

test('repeated Back clicks share one edit prompt and restore the current page when cancelled', async () => {
  const {dom, w, doc} = setup();
  w.fetch = async url => ({ok: true, url: String(url), text: async () => html(new URL(url).pathname, '<form><input name="name" value="Original"></form>')});
  await w.NetEmActions.navigate('/tests'); await w.NetEmActions.navigate('/settings');
  const input = doc.querySelector('input'); input.value = 'Unsaved'; input.dispatchEvent(new w.Event('input', {bubbles: true}));
  const dialog = doc.createElement('dialog'); dialog.id = 'navigation-confirm-dialog';
  let prompts = 0;
  dialog.showModal = () => { prompts++; dialog.setAttribute('open', ''); };
  doc.body.append(dialog);
  const first = new Promise(resolve => w.addEventListener('popstate', resolve, {once: true}));
  w.history.back(); await first;
  const second = new Promise(resolve => w.addEventListener('popstate', resolve, {once: true}));
  w.history.back(); await second;
  assert.equal(prompts, 1);
  dialog.returnValue = 'stay'; dialog.removeAttribute('open'); dialog.dispatchEvent(new w.Event('close'));
  await waitFor(() => w.location.pathname === '/settings');
  assert.equal(doc.querySelector('input').value, 'Unsaved');
  dom.window.close();
});

test('a late navigation cannot overwrite the newest page', async () => {
  const {dom, w, doc} = setup();
  const pending = {};
  w.fetch = url => new Promise(resolve => { pending[new URL(url).pathname] = resolve; });
  const first = w.NetEmActions.navigate('/tests');
  const second = w.NetEmActions.navigate('/settings');
  pending['/settings']({ok: true, url: 'https://netem.test/settings', text: async () => html('Settings')});
  await second;
  pending['/tests']({ok: true, url: 'https://netem.test/tests', text: async () => html('Tests')});
  await first;
  assert.equal(doc.querySelector('h1').textContent, 'Settings');
  assert.equal(w.location.pathname, '/settings');
  assert.equal(w.history.length, 2);
  dom.window.close();
});

test('edits made while navigation is loading are protected before replacing the page', async () => {
  const {dom, w, doc} = setup('<form><input name="name" value="Original"></form>'); let resolve;
  w.fetch = () => new Promise(done => { resolve = done; });
  w.confirm = () => false;
  const pending = w.NetEmActions.navigate('/tests');
  const input = doc.querySelector('input'); input.value = 'Edited during loading';
  input.dispatchEvent(new w.Event('input', {bubbles: true}));
  resolve({ok: true, url: 'https://netem.test/tests', text: async () => html('Tests')});
  await pending;
  assert.equal(w.location.pathname, '/');
  assert.equal(doc.querySelector('input').value, 'Edited during loading');
  dom.window.close();
});

test('returning to the visible page cancels an unfinished navigation', async () => {
  const {dom, w, doc} = setup(); let resolve;
  w.fetch = () => new Promise(done => { resolve = done; });
  const pending = w.NetEmActions.navigate('/tests');
  await w.NetEmActions.navigate('/');
  resolve({ok: true, url: 'https://netem.test/tests', text: async () => html('Tests')}); await pending;
  assert.equal(w.location.pathname, '/');
  assert.equal(doc.querySelector('h1').textContent, 'Overview');
  dom.window.close();
});

test('failed navigation keeps the current page, URL and live client', async () => {
  const {dom, w, doc} = setup(); let stops = 0;
  w.NetEmActions.trackClient({stop: () => { stops++; }});
  const original = doc.querySelector('main');
  w.fetch = async () => ({ok: false, status: 503});
  await w.NetEmActions.navigate('/tests');
  assert.equal(doc.querySelector('main'), original);
  assert.equal(w.location.pathname, '/');
  assert.equal(stops, 0);
  assert.match(doc.querySelector('#action-error').textContent, /current page has been kept/);
  dom.window.close();
});

test('navigation stops old live clients and mounts the next page scripts once', async () => {
  const {dom, w} = setup(); let stops = 0; w.mounts = 0;
  w.NetEmActions.trackClient({stop: () => { stops++; }});
  w.fetch = async url => ({ok: true, url: String(url), text: async () => html('Tests', '', 'window.mounts++;')});
  await w.NetEmActions.navigate('/tests');
  assert.equal(stops, 1);
  assert.equal(w.mounts, 1);
  dom.window.close();
});

test('downloads, external URLs, modified clicks and modal launchers retain their own behavior', () => {
  const {dom, w, doc} = setup('<a href="/sessions/id/report.json">JSON</a><a href="/tests" download>Download</a><a href="https://other.test/tests">External</a><a href="/tests" target="_blank">New tab</a><a href="/tests" data-scenario-modal="/tests/scenario/test">Popup</a>');
  for (const link of doc.querySelectorAll('main a')) {
    const click = new w.MouseEvent('click', {bubbles: true, cancelable: true});
    doc.addEventListener('click', event => { assert.equal(event.defaultPrevented, false); event.preventDefault(); }, {once: true});
    link.dispatchEvent(click);
  }
  const click = new w.MouseEvent('click', {bubbles: true, cancelable: true, ctrlKey: true});
  doc.addEventListener('click', event => { assert.equal(event.defaultPrevented, false); event.preventDefault(); }, {once: true});
  doc.querySelector('a[href="/tests"]').dispatchEvent(click);
  dom.window.close();
});

test('GET forms and destination anchors navigate within the shell', async () => {
  const {dom, w, doc} = setup('<form action="/docs" method="get"><input name="q" value="WAN"></form>');
  w.fetch = async url => ({ok: true, url: String(url), text: async () => html('Docs', '<details id="section"><summary>Guide</summary><h2 id="wan">WAN</h2></details>')});
  doc.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true})); await settle();
  assert.equal(w.location.pathname + w.location.search, '/docs?q=WAN');
  await w.NetEmActions.navigate('/docs?q=WAN#wan');
  assert.equal(w.location.hash, '#wan');
  assert.equal(doc.getElementById('section').open, true);
  dom.window.close();
});
