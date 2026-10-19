const {test, afterEach} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const windows = [];
afterEach(() => windows.splice(0).forEach(w => w.close()));
const metrics = {disk: {total: 21474836480, used: 3221225472, available: 18253611008, used_percent: 15},
  databases: [{name: 'Telemetry', main_bytes: 2097152, wal_bytes: 1048576, shm_bytes: 32768, total_bytes: 3178496}],
  event_log_bytes: 512, journal: {bytes: 104857600}, job: {state: 'idle'}, checked_at: 100};
const source = fs.readFileSync('templates/storage.html', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace('{{ storage|tojson }}', JSON.stringify(metrics)).replace('{{ url_for("storage_status") }}', '/api/v1/storage');
const settle = async () => { for (let i=0; i<4; i++) await new Promise(resolve => setImmediate(resolve)); };
function setup() {
  const fields = ['health','total','used','free','events','journals','log-note','job','updated'];
  const dom = new JSDOM(`<div id="storage-panel">${fields.map(f=>`<div data-storage-${f}></div>`).join('')}
    <table><tbody data-storage-databases></tbody></table><form id="storage-compact-form" action="/settings/storage/compact">
    <input name="integration_csrf" value="token"><button data-storage-compact>Compact database</button></form></div>`,
    {url: 'https://netem.test/settings/storage', runScripts: 'outside-only'});
  const w = dom.window; windows.push(w); const timers = [];
  w.setTimeout = (fn, ms) => { if (ms === 5000) timers.push(fn); return 1; };
  w.clearTimeout = () => {};
  let data = structuredClone(metrics), posts = 0, failure = null;
  w.fetch = async (url, options = {}) => {
    if (options.method === 'POST') {
      posts++;
      assert.equal(options.body.get('integration_csrf'), 'token');
      if (failure) return failure;
      data.job = {state: 'running', message: 'Compacting telemetry database…'};
      return {ok: true, status: 202, json: async () => ({accepted: true, job: data.job})};
    }
    return {ok: true, json: async () => data};
  };
  w.eval(source);
  return {w, timers, set: value => {data = value;}, fail: value=>{failure=value;}, posts:()=>posts,
    text: key => w.document.querySelector(`[data-storage-${key}]`).textContent,
    submit: () => w.document.querySelector('form').dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}))};
}

test('storage displays sizes and preserves unknown values instead of showing zero', async () => {
  const t = setup(); await settle();
  assert.equal(t.text('free'), '17.0 GiB');
  assert.equal(t.text('journals'), '100.0 MiB');
  assert.match(t.w.document.querySelector('tbody').textContent, /Telemetry2.0 MiB1.0 MiB32.0 KiB/);
  t.set({...metrics, journal: {bytes: null, error: 'Permission unavailable'}});
  await t.timers.pop()();
  assert.equal(t.text('journals'), 'Unavailable');
  assert.equal(t.text('log-note'), 'Permission unavailable');
});

test('compaction posts once and updates status and reclaimed bytes after completion', async () => {
  const t = setup(); await settle(); t.submit(); t.submit(); await settle();
  assert.equal(t.posts(), 1);
  assert.match(t.text('job'), /Compacting/);
  await t.timers.pop()();
  assert.equal(t.w.document.querySelector('button').disabled, true);
  t.set({...metrics, job: {state:'completed', message:'Database compacted.', reclaimed_bytes:1048576}});
  await t.timers.pop()();
  assert.match(t.text('job'), /Reclaimed 1.0 MiB/);
  assert.equal(t.w.document.querySelector('button').disabled, false);
});

test('a refused compaction explains the idle requirement and does not retry automatically', async () => {
  const t = setup(); await settle();
  t.fail({ok:false, status:409, json:async()=>({error:'Finish the active session before compacting.'})});
  t.submit(); await settle();
  assert.match(t.text('job'), /Finish the active session/);
  assert.equal(t.posts(), 1);
});

test('non-JSON and expired-auth responses show actionable errors', async () => {
  for (const status of [400, 401, 502]) {
    const t = setup(); await settle();
    t.fail({ok:false, status, json:async()=>{throw new Error('HTML response');}});
    t.submit(); await settle();
    assert.match(t.text('job'), status === 401 ? /Sign in again/ : /could not be confirmed.*HTTP/);
    assert.equal(t.posts(), 1);
  }
});
