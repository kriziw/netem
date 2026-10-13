const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/time-format.js', 'utf8');

// Local times, so the expectations hold in any time zone.
const at = (hours, minutes = 0, seconds = 0) => new Date(2026, 9, 13, hours, minutes, seconds);
const load = () => {
  const context = {window: {}};
  vm.createContext(context);
  vm.runInContext(source, context);
  return context.window.NetEmTime;
};

test('24-hour is the default, with leading zeros and seconds', () => {
  const time = load();
  assert.equal(time.format(), '24h');
  assert.equal(time.clock(at(14, 5, 9)), '14:05:09');
  assert.equal(time.clock(at(0, 5, 9)), '00:05:09');
  assert.equal(time.clock(at(23, 59)), '23:59:00');
  assert.equal(time.clock(at(14, 5, 9), {seconds: false}), '14:05');
});

test('12-hour uses AM and PM and no leading zero, with midnight and noon named by the clock', () => {
  const time = load();
  time.setFormat('12h');
  assert.equal(time.format(), '12h');
  assert.equal(time.clock(at(14, 5, 9)), '2:05:09 PM');
  assert.equal(time.clock(at(9, 30)), '9:30:00 AM');
  assert.equal(time.clock(at(0, 5)), '12:05:00 AM');
  assert.equal(time.clock(at(12, 0)), '12:00:00 PM');
  assert.equal(time.clock(at(12, 0, 59), {seconds: false}), '12:00 PM');
});

test('switching back to 24-hour restores it, and unknown values fall back to it', () => {
  const time = load();
  time.setFormat('12h');
  time.setFormat('24h');
  assert.equal(time.clock(at(14, 5)), '14:05:00');
  time.setFormat('12h');
  time.setFormat('bogus');
  assert.equal(time.format(), '24h');
  assert.equal(time.clock(at(14, 5)), '14:05:00');
});

test('Unix seconds and Dates both work, and a missing time shows a dash', () => {
  const time = load();
  const seconds = at(14, 5, 9).getTime() / 1000;
  assert.equal(time.clock(seconds), '14:05:09');
  assert.equal(time.clock(String(seconds)), '14:05:09');
  assert.equal(time.clock(null), '—');
  assert.equal(time.clock(''), '—');
  assert.equal(time.clock('not a time'), '—');
  assert.equal(time.stamp(null), '—');
});

test('records show a date with the clock in the chosen format', () => {
  const time = load();
  const seconds = at(14, 5, 9).getTime() / 1000;
  assert.ok(time.stamp(seconds).endsWith(' 14:05:09'));
  time.setFormat('12h');
  assert.ok(time.stamp(seconds).endsWith(' 2:05:09 PM'));
  assert.ok(time.stamp(seconds, {seconds: false}).endsWith(' 2:05 PM'));
});

test('the page sets the format from its body, and defaults to 24-hour without one', () => {
  const page = body => {
    const context = {window: {}, document: {body}};
    vm.createContext(context);
    vm.runInContext(source, context);
    return context.window.NetEmTime;
  };
  assert.equal(page({dataset: {timeFormat: '12h'}}).format(), '12h');
  assert.equal(page({dataset: {timeFormat: '12h'}}).clock(at(14, 5)), '2:05:00 PM');
  assert.equal(page({dataset: {}}).format(), '24h');
  assert.equal(page(null).format(), '24h');
});
