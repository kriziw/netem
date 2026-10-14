const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');

const dom = new JSDOM('<!doctype html><body></body>');
const context = {
  window: {addEventListener() {}},
  document: dom.window.document,
  setInterval() { return 0; },
  setTimeout() { return 0; },
  clearInterval() {},
  clearTimeout() {},
  requestAnimationFrame() { return 0; },
  fetch() { return new Promise(() => {}); },
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/time-format.js', 'utf8'), context);
vm.runInContext(fs.readFileSync('static/showroom.js', 'utf8'), context);
const ui = context.window.ShowroomUI;

test('each traffic group gets its own card with its score, tone and sentence', () => {
  const cards = ui.groupCards([
    {key: 'interactive', label: 'Web and business apps', score: 10, tone: 'bad',
      text: 'Limited by DNS (10/100): 67% of transactions met the 120 ms target.'},
    {key: 'bulk', label: 'File transfers', score: null, tone: 'unknown', text: 'No traffic in this group in the window.'}], true);
  assert.equal(cards.length, 2);
  assert.ok(cards[0].classList.contains('bad'));
  assert.equal(cards[0].querySelector('strong').textContent, '10 / 100');
  assert.match(cards[0].querySelector('p').textContent, /Limited by DNS/);
  assert.equal(cards[1].querySelector('strong').textContent, '—');
});

test('cards are grey and show no score when the simulator is not live', () => {
  const [card] = ui.groupCards([{label: 'Voice and video', score: 96, tone: 'good', text: 'Performing well.'}], false);
  assert.ok(card.classList.contains('unknown'));
  assert.equal(card.querySelector('strong').textContent, '—');
});

test('an incomplete traffic group labels its score as provisional', () => {
  const [card] = ui.groupCards([{label: 'Voice and video', score: 99, tone: 'unknown', scoring_complete: false,
    text: 'Incomplete application evidence; this group cannot yet be judged.'}], true);
  assert.equal(card.querySelector('strong').textContent, '99 / 100 · provisional');
  assert.ok(card.classList.contains('unknown'));
});
