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

test('branded login updates identity and favicon without clearing entered credentials', async () => {
  const {execFileSync} = require('node:child_process');
  const html = execFileSync('python', ['-c', `
from flask import render_template
from app import app
with app.test_request_context('/login'):
    print(render_template('login.html', next_url='/tests', oidc=True))
`], {encoding:'utf8', env:{...process.env,NETEM_AUTH_REQUIRED:'0'}});
  const dom = new JSDOM(html, {url:'https://netem.test/login', runScripts:'outside-only'});
  const w = dom.window;
  let poll;
  w.setInterval = callback => {poll=callback;};
  w.fetch = async () => ({ok:true,json:async()=>({revision:'changed-login-brand',enabled:true,name:'Updated Lab',
    subtitle:'Configured subtitle',mark:'UL',logo:'/branding/assets/new-logo.svg?brand=new',favicon:'/branding/assets/new-icon.svg?brand=new'})});
  const username=w.document.getElementById('username'), password=w.document.getElementById('password');
  username.value='lab-operator'; password.value='entered-test-value';
  w.eval(source);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(w.document.title,'Sign in · Updated Lab');
  assert.equal(w.document.querySelector('[data-brand-logo]').getAttribute('src'),'/branding/assets/new-logo.svg?brand=new');
  assert.equal(w.document.querySelector('[data-brand-icon]').getAttribute('href'),'/branding/assets/new-icon.svg?brand=new');
  assert.equal(username.value,'lab-operator'); assert.equal(password.value,'entered-test-value');
  assert.equal(w.document.querySelector('input[name=next]').value,'/tests');
  assert.ok(w.document.querySelector('input[name=integration_csrf]').value);
  assert.equal(w.document.getElementById('access-pause-motion').type,'checkbox');
  assert.equal(w.document.querySelector('.access-backdrop').getAttribute('aria-hidden'),'true');
  assert.ok(w.document.querySelector('a[href="/auth/oidc"]') || [...w.document.querySelectorAll('a')].some(a=>a.textContent.includes('Authentik')));
  await poll();
  dom.window.close();
});
