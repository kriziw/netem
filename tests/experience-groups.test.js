const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');

function panel() {
  const indicator = name => `<div data-dem-indicator="${name}"><span data-dem-symbol></span><span data-dem-status></span>` +
    `<strong data-dem-value></strong><p data-dem-explanation></p></div>`;
  const dom = new JSDOM(`<div id="root">${indicator('experience')}${indicator('success')}${indicator('response')}` +
    `<p data-dem-window></p><div data-dem-groups></div></div>`);
  const context = {window: {}, document: dom.window.document};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('static/time-format.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('static/app.js', 'utf8'), context);
  return {ui: context.window.NetEmUI, root: dom.window.document.getElementById('root')};
}

test('each traffic group shows its own sub-score, a tone and one short sentence', () => {
  const {ui, root} = panel();
  ui.renderDemSummary(root, {requests: 20, experience_score: 10, experience_groups: [
    {key: 'realtime', label: 'Voice and video', score: 96, tone: 'good', text: 'Performing well.'},
    {key: 'interactive', label: 'Web and business apps', score: 10, tone: 'bad',
      text: 'Limited by DNS (10/100): 67% of transactions met the 120 ms target.'},
    {key: 'bulk', label: 'File transfers', score: null, tone: 'unknown', text: 'No traffic in this group in the window.'}]}, true);
  const groups = [...root.querySelectorAll('[data-dem-groups] .dem-group')];
  assert.equal(groups.length, 3);
  assert.ok(groups[1].classList.contains('bad'));
  assert.match(groups[1].textContent, /Web and business apps\s*10\/100/);
  assert.match(groups[1].textContent, /Limited by DNS/);
  assert.match(groups[2].textContent, /—/);
});

test('group text is escaped, and a summary without groups clears them', () => {
  const {ui, root} = panel();
  ui.renderDemSummary(root, {requests: 5, experience_groups: [
    {key: 'bulk', label: '<b>x</b>', score: 50, tone: 'warn', text: '<img src=x>'}]}, true);
  assert.equal(root.querySelector('[data-dem-groups] b'), null);
  assert.equal(root.querySelector('[data-dem-groups] img'), null);
  ui.renderDemSummary(root, {}, true);
  assert.equal(root.querySelectorAll('[data-dem-groups] .dem-group').length, 0);
});
