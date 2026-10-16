const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync('static/update-screen.js', 'utf8');
const form = '<form data-update-form method="post" action="/updates" data-from="0.12.0" data-to="0.13.0" data-mark="&lt;b&gt;N&lt;/b&gt;" data-instance="old" data-status-url="/updates/status"><input name="action" value="update"></form>';

function setup(post, statuses) {
  const dom = new JSDOM('<body>' + form + '</body>', {url: 'https://netem.test/updates', runScripts: 'outside-only'});
  const win = dom.window, timers = [], requests = [], posts = [];
  let tick = null;
  win.setTimeout = (fn, ms) => { timers.push({fn, ms}); return timers.length; };
  win.clearTimeout = () => {};
  win.setInterval = fn => { tick = fn; return 1; };
  win.clearInterval = () => { tick = null; };
  win.fetch = async (url, options = {}) => {
    requests.push([String(url), options.method || 'GET']);
    if (options.method === 'POST') { posts.push(options); return post; }
    const next = statuses.shift();
    if (next instanceof Error) throw next;
    return {ok: true, headers: {get: () => 'application/json'}, json: async () => next};
  };
  win.eval(source);
  const doc = win.document;
  const steps = () => [...doc.querySelectorAll('.update-steps li')].map(item => item.className);
  // Run the next scheduled poll (2 s timers) and let its fetch settle.
  const poll = async () => {
    const index = timers.findIndex(timer => timer.ms === 2000);
    const [timer] = timers.splice(index, 1);
    await timer.fn();
  };
  return {dom, win, doc, steps, poll, timers, requests, posts, tick: () => tick && tick()};
}

const page = body => ({ok: true, status: 200, text: async () => '<html><body><main class="page">' + body + '</main></body></html>'});

test('phase waits for a different process to answer twice', () => {
  const {win} = setup();
  const {phase} = win.NetEmUpdate;
  assert.equal(phase(null, 'old', 0), 'restarting');
  assert.equal(phase({instance: 'old'}, 'old', 0), 'restarting');
  assert.equal(phase({instance: 'new'}, 'old', 1), 'reconnecting');
  assert.equal(phase({instance: 'new'}, 'old', 2), 'complete');
});

test('installs, follows the restart through downtime and reloads on the new process', async () => {
  const restarting = page('<div id="update-restarting" data-instance="old" data-version="0.13.0" hidden></div>');
  const ui = setup(restarting, [{instance: 'old', version: '0.12.0'}, new Error('offline'),
                                {instance: 'new', version: '0.13.0'}, {instance: 'new', version: '0.13.0'}]);
  const running = ui.win.NetEmUpdate.run(ui.doc.querySelector('form'));
  assert.equal(ui.doc.getElementById('update-title').textContent, 'Installing update');
  assert.deepEqual(ui.steps(), ['active', '', '']);
  assert.ok(ui.doc.body.classList.contains('update-active'));
  assert.equal(ui.doc.querySelector('.update-versions').textContent, 'v0.12.0 → v0.13.0');
  assert.equal(ui.doc.querySelector('.update-mark').textContent, '<b>N</b>');
  assert.equal(ui.doc.querySelectorAll('.update-mark b').length, 0);
  await running;
  assert.deepEqual(ui.requests[0], ['https://netem.test/updates', 'POST']);
  assert.equal(ui.doc.getElementById('update-title').textContent, 'Restarting NetEm');
  assert.deepEqual(ui.steps(), ['done', 'active', '']);
  await ui.poll();
  await ui.poll();
  assert.deepEqual(ui.steps(), ['done', 'active', '']);
  await ui.poll();
  assert.equal(ui.doc.getElementById('update-title').textContent, 'Finishing update');
  assert.deepEqual(ui.steps(), ['done', 'done', 'active']);
  await ui.poll();
  assert.equal(ui.doc.getElementById('update-title').textContent, 'Update complete');
  assert.equal(ui.doc.querySelector('.update-message').textContent, 'Now running v0.13.0. Reloading…');
  assert.deepEqual(ui.steps(), ['done', 'done', 'done']);
  assert.ok(ui.doc.querySelector('.update-screen').classList.contains('complete'));
  assert.equal(ui.requests.filter(([url]) => url === '/updates/status').length, 4);
  assert.equal(ui.timers.filter(timer => timer.ms === 2000).length, 1);
  assert.equal(ui.win.NetEmUpdate.show({}), null);
  ui.dom.window.close();
});

test('a refused update says why, restarts nothing and offers a way back', async () => {
  const ui = setup(page('<div class="flash error">Update blocked because tracked application files have local changes.</div>'), []);
  await ui.win.NetEmUpdate.run(ui.doc.querySelector('form'));
  assert.equal(ui.doc.getElementById('update-title').textContent, 'Update not installed');
  assert.match(ui.doc.querySelector('.update-message').textContent, /local changes/);
  assert.deepEqual(ui.steps(), ['failed', '', '']);
  assert.equal(ui.doc.querySelector('.update-actions button').textContent, 'Back to updates');
  assert.equal(ui.timers.filter(timer => timer.ms === 2000).length, 0);
  ui.dom.window.close();
});

test('a slow restart points at the service log', async () => {
  const ui = setup(page('<div id="update-restarting" data-instance="old" data-version="0.13.0"></div>'), []);
  const started = Date.now();
  await ui.win.NetEmUpdate.run(ui.doc.querySelector('form'));
  assert.match(ui.doc.querySelector('.update-meta').textContent, /^Elapsed 0:00$/);
  ui.win.Date.now = () => started + 130000;
  ui.tick();
  assert.match(ui.doc.querySelector('.update-meta').textContent, /^Elapsed 2:\d\d · taking longer than usual; on the appliance run journalctl -u netem -n 50$/);
  ui.dom.window.close();
});

const jsonReply = (value, status = 202) => ({ok: status < 400, status, headers: {get: () => 'application/json'}, json: async () => value});

test('JSON install acknowledgement starts verification and requests the JSON protocol', async () => {
  const ui = setup(jsonReply({update_state:'restarting',instance:'old',version:'0.13.0'}), []);
  await ui.win.NetEmUpdate.run(ui.doc.querySelector('form'));
  assert.equal(ui.posts[0].headers.Accept, 'application/json');
  assert.equal(ui.doc.getElementById('update-title').textContent, 'Restarting NetEm');
  ui.dom.window.close();
});

test('502 after a successful install verifies the new version instead of falsely failing', async () => {
  const ui = setup({ok:false,status:502,text:async()=>'<h1>Bad Gateway</h1>'}, [
    {instance:'old',version:'0.12.0'}, {instance:'new',version:'0.13.0'}, {instance:'new',version:'0.13.0'}]);
  await ui.win.NetEmUpdate.run(ui.doc.querySelector('form'));
  assert.equal(ui.doc.getElementById('update-title').textContent, 'Confirming update');
  assert.doesNotMatch(ui.doc.body.textContent, /Nothing was restarted/);
  await ui.poll(); await ui.poll(); await ui.poll();
  assert.equal(ui.doc.getElementById('update-title').textContent, 'Update complete');
  assert.equal(ui.requests.filter(([,method])=>method==='POST').length,1);
  ui.dom.window.close();
});

test('unacknowledged 400 ends as unconfirmed without claiming that nothing restarted', async () => {
  const ui = setup({ok:false,status:400,text:async()=>'<h1>Bad Request</h1>'}, [{instance:'old',version:'0.12.0'}]);
  await ui.win.NetEmUpdate.run(ui.doc.querySelector('form'));
  const now = ui.win.Date.now(); ui.win.Date.now = () => now + 31000;
  await ui.poll();
  assert.equal(ui.doc.getElementById('update-title').textContent, 'Update not confirmed');
  assert.match(ui.doc.querySelector('.update-message').textContent, /HTTP 400/);
  assert.doesNotMatch(ui.doc.body.textContent, /Nothing was restarted/);
  ui.dom.window.close();
});

test('an interrupted acknowledgement body still follows the restart', async () => {
  const ui = setup({ok:true,status:200,text:async()=>{throw new Error('Connection reset');}}, [
    {instance:'new',version:'0.13.0'}, {instance:'new',version:'0.13.0'}]);
  await ui.win.NetEmUpdate.run(ui.doc.querySelector('form'));
  await ui.poll(); await ui.poll();
  assert.equal(ui.doc.getElementById('update-title').textContent,'Update complete');
  ui.dom.window.close();
});

test('two different restarted processes do not count as two stable confirmations', async () => {
  const ui = setup(jsonReply({update_state:'restarting',instance:'old',version:'0.13.0'}), [
    {instance:'new-a',version:'0.13.0'}, {instance:'new-b',version:'0.13.0'}, {instance:'new-b',version:'0.13.0'}]);
  await ui.win.NetEmUpdate.run(ui.doc.querySelector('form'));
  await ui.poll(); await ui.poll();
  assert.equal(ui.doc.getElementById('update-title').textContent,'Finishing update');
  await ui.poll();
  assert.equal(ui.doc.getElementById('update-title').textContent,'Update complete');
  ui.dom.window.close();
});

test('a restarted process with the old version cannot confirm an update', async () => {
  const ui = setup(jsonReply({update_state:'restarting',instance:'old',version:'0.13.0'}), [
    {instance:'new',version:'0.12.0'}, {instance:'new',version:'0.12.0'}]);
  await ui.win.NetEmUpdate.run(ui.doc.querySelector('form'));
  await ui.poll(); await ui.poll();
  assert.notEqual(ui.doc.getElementById('update-title').textContent,'Update complete');
  ui.dom.window.close();
});

test('an authentication wall offers sign-in rather than polling indefinitely', async () => {
  const ui = setup(jsonReply({update_state:'restarting',instance:'old',version:'0.13.0'}), []);
  await ui.win.NetEmUpdate.run(ui.doc.querySelector('form'));
  ui.win.fetch = async () => ({ok:false,status:401,url:'https://netem.test/updates/status'});
  await ui.poll();
  assert.match(ui.doc.getElementById('update-title').textContent,/Sign in/);
  assert.equal(ui.doc.querySelector('.update-actions a').getAttribute('href'),'/login?next=%2Fupdates');
  ui.dom.window.close();
});

test('old live cards recover their missing token from the check form without bypassing CSRF', async () => {
  const ui = setup(jsonReply({update_state:'rejected',message:'Invalid form token'},400), []);
  const check=ui.doc.createElement('form'); check.setAttribute('data-all-update-check','');
  check.innerHTML='<input name="integration_csrf" value="csrf-from-page">';ui.doc.body.append(check);
  await ui.win.NetEmUpdate.run(ui.doc.querySelector('[data-update-form]'));
  assert.equal(ui.posts[0].body.get('integration_csrf'),'csrf-from-page');
  assert.equal(ui.doc.getElementById('update-title').textContent,'Update not installed');
  ui.dom.window.close();
});
