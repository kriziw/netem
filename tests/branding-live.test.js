const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync('static/branding-live.js', 'utf8');

test('navigation reuses the rendered brand; only a changed revision reloads the theme', async () => {
  const dom = new JSDOM('<html data-brand-revision="r1"><head>' +
    '<link data-brand-theme rel="stylesheet" href="/branding/theme.css?brand=r1"></head>' +
    '<body><span data-brand-name>First brand</span><img data-brand-logo src="/branding/assets/logo.svg?brand=r1">' +
    '<span data-brand-mark>N</span></body></html>', {url: 'http://netem.test/', runScripts: 'outside-only'});
  const win = dom.window;
  let poll;
  let brand = {enabled: true, revision: 'r1', name: 'First brand', mark: 'N', subtitle: 'Lab',
    logo: '/branding/assets/logo.svg?brand=r1'};
  win.fetch = async () => ({ok: true, json: async () => brand});
  win.setInterval = callback => { poll = callback; };
  let changes = 0;
  const theme = win.document.querySelector('[data-brand-theme]');
  const observer = new win.MutationObserver(records => { changes += records.length; });
  observer.observe(theme, {attributes: true});
  win.eval(source);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(changes, 0);
  brand = {...brand, revision: 'r2', name: 'Next brand', logo: '/branding/assets/logo.svg?brand=r2'};
  await poll();
  assert.equal(theme.getAttribute('href'), '/branding/theme.css?brand=r2');
  assert.equal(win.document.querySelector('[data-brand-logo]').getAttribute('src'), brand.logo);
  assert.equal(win.document.querySelector('[data-brand-name]').textContent, 'Next brand');
  const changedOnce = changes;
  await poll();
  assert.equal(changes, changedOnce);
  brand = {...brand, revision: 'r3', enabled: false, logo: null};
  await poll();
  assert.equal(win.document.querySelector('[data-brand-theme]'), null);
  assert.equal(win.document.querySelector('[data-brand-logo]').hidden, true);
  observer.disconnect();
  win.close();
});
