const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync('static/component-updates.js', 'utf8');

function cards(available = false) {
  return '<div data-update-cards data-components-url="/updates/components">' + ['netem', 'simulator', 'target'].map(name =>
    `<section class="card" data-component="${name}"><div class="card-head"><span class="status">${available ? 'Update available' : 'Not checked'}</span></div>` +
    (name === 'netem' ? '<details><summary>Git details</summary>Details</details>' : '') +
    `<form ${name === 'netem' ? 'data-update-form' : ''}><input name="tag" value="${available ? 'v2.0.0' : ''}"><button ${available ? '' : 'disabled'}>Install update</button></form></section>`).join('') + '</div>';
}

function setup(responses) {
  const dom = new JSDOM('<body><form data-all-update-check><button>Check for updates</button></form><span data-component-check-status></span>' + cards() + '</body>',
    {url: 'https://netem.test/updates', runScripts: 'outside-only'});
  const {window: win} = dom;
  const timers = [], calls = [], installs = [];
  win.confirm = () => true;
  win.NetEmUpdate = {run: form => installs.push(form)};
  win.eval(source);
  const client = win.NetEmComponentUpdates.mount({
    schedule: fn => timers.push(fn),
    request: async (url, options) => {
      calls.push([url, options]);
      const value = responses.shift();
      if (value instanceof Error) throw value;
      return value;
    },
  });
  return {dom, win, doc: win.document, calls, installs, client, poll: () => timers.shift()()};
}

const reply = (available, running = false) => ({ok: true, json: async () => ({html: cards(available),
  checks: {running, components: {netem: {state: 'complete'}, simulator: {state: 'complete'}, target: {state: 'complete'}}}})});

test('all three badges, install controls and checked tags refresh without a reload', async () => {
  const ui = setup([reply(false, true), reply(true)]);
  await ui.poll();
  assert.equal(ui.doc.querySelector('[data-all-update-check] button').disabled, true);
  ui.doc.querySelector('details').open = true;
  await ui.poll();
  assert.equal(ui.doc.querySelector('[data-all-update-check] button').disabled, false);
  assert.equal(ui.doc.querySelector('[data-component-check-status]').textContent, 'All components checked.');
  assert.ok([...ui.doc.querySelectorAll('.card-head .status')].every(node => node.textContent === 'Update available'));
  assert.ok([...ui.doc.querySelectorAll('[data-update-cards] button')].every(node => !node.disabled));
  assert.ok([...ui.doc.querySelectorAll('[name="tag"]')].every(node => node.value === 'v2.0.0'));
  assert.equal(ui.doc.querySelector('details').open, true);
  const form = ui.doc.querySelector('[data-update-form]');
  const event = new ui.win.Event('submit', {bubbles: true, cancelable: true});
  form.dispatchEvent(event);
  assert.ok(event.defaultPrevented);
  assert.equal(ui.installs[0], form); // Existing NetEm restart screen survives card replacement.
  assert.equal(ui.calls[0][1].cache, 'no-store');
  ui.dom.window.close();
});

test('a temporary outage disables stale installs and restores them on retry', async () => {
  const ui = setup([reply(true), new Error('Agent offline'), reply(true)]);
  await ui.poll();
  await ui.poll();
  assert.ok([...ui.doc.querySelectorAll('[data-update-cards] button')].every(node => node.disabled));
  assert.equal(ui.doc.querySelector('[data-component-check-status]').textContent, 'Agent offline');
  await ui.poll();
  assert.ok([...ui.doc.querySelectorAll('[data-update-cards] button')].every(node => !node.disabled));
  ui.dom.window.close();
});

test('polling does not replace submitted install forms or continue after leaving the page', async () => {
  const ui = setup([reply(true)]);
  const form = ui.doc.querySelector('[data-update-form]');
  form.setAttribute('aria-busy', 'true');
  await ui.poll();
  assert.equal(ui.doc.querySelector('[data-update-form]'), form);
  ui.client.stop();
  await ui.poll();
  assert.equal(ui.calls.length, 1);
  ui.dom.window.close();
});
