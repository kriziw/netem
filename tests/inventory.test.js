const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

function fixture() {
  const dom = new JSDOM(`<main><tr></tr><div data-inventory-row="a">
    <span data-inventory-availability></span><span data-inventory-health></span>
    <span data-inventory-checked></span><span data-inventory-selection></span>
    <div data-inventory-licenses></div></div><span data-platform-simulator></span>
    <span data-platform-target></span><span data-platform-management></span>
    <span data-platform-freshness></span><span data-platform-workload></span><span data-platform-clock></span><div data-platform-errors></div></main>`, {runScripts:'outside-only'});
  dom.window.CSS = {escape: value => value};
  dom.window.eval(fs.readFileSync(path.join(__dirname,'../static/inventory.js'),'utf8'));
  return dom;
}

test('stale observations cannot retain healthy licenses or active selection', () => {
  const dom = fixture();
  dom.window.NetEmInventory.render({appliances:[{id:'a',selected:true,stale:true,
    observation:{availability:'reachable',health:{state:'healthy'},licenses:[{key:'vm',state:'valid'}]}}],
    selection:{active:true},selection_verified:false,platform_stale:true,
    platform:{connected:true,path:{target:{ok:true}},target_management:{connected:true}}});
  assert.match(dom.window.document.querySelector('[data-inventory-licenses]').textContent,/Unknown/);
  assert.match(dom.window.document.querySelector('[data-inventory-selection]').textContent,/unverified/);
  assert.match(dom.window.document.querySelector('[data-platform-target]').textContent,/stale/);
  assert.match(dom.window.document.querySelector('[data-platform-management]').textContent,/stale/);
  dom.window.close();
});

test('vendor strings and API errors are text, and target paths remain independent', () => {
  const dom = fixture();
  dom.window.NetEmInventory.render({appliances:[{id:'a',stale:false,observation:{licenses:[{key:'<img src=x onerror=alert(1)>',state:'expired'}]}}],
    selection:{},platform_stale:false,platform:{connected:true,path:{target:{ok:false}},
      target_management:{connected:true},errors:['<script>alert(1)</script>']}});
  assert.equal(dom.window.document.querySelectorAll('img,script').length,0);
  assert.equal(dom.window.document.querySelector('[data-platform-target]').textContent,'Not verified');
  assert.equal(dom.window.document.querySelector('[data-platform-management]').textContent,'Connected');
  dom.window.close();
});

test('missing workload status is unknown and clock uses the simulator synchronization flag', () => {
  const dom = fixture();
  dom.window.NetEmInventory.render({platform_stale:false,platform:{simulator:{clock:{synchronized:false}}}});
  assert.equal(dom.window.document.querySelector('[data-platform-workload]').textContent,'Unknown');
  assert.equal(dom.window.document.querySelector('[data-platform-clock]').textContent,'Not synchronized');
  dom.window.close();
});

test('a lost browser connection blanks current health without extra vendor polling', async () => {
  const dom = fixture();
  const urls = [];
  dom.window.fetch = async url => { urls.push(url); throw new Error('offline'); };
  const client = dom.window.NetEmInventory.mount();
  await new Promise(resolve => setTimeout(resolve,10));
  assert.deepEqual(urls,['/api/v1/appliances']);
  assert.match(dom.window.document.querySelector('[data-inventory-licenses]').textContent,/connection lost/);
  assert.match(dom.window.document.querySelector('[data-platform-simulator]').textContent,/connection lost/);
  client.stop(); dom.window.close();
});
