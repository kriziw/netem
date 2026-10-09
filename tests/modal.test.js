const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');
const wait=()=>new Promise(resolve=>setTimeout(resolve,15));
const dialogHTML='<dialog id="traffic-simulator-dialog"><h2 data-simulator-title></h2><button data-simulator-close>Close</button><div data-simulator-feedback></div><div data-simulator-content></div></dialog>';
const workload=(state='idle')=>'<div id="corporate-traffic"></div><section class="card"><span data-tg-status>'+state.toUpperCase()+'</span><form method="post" action="/integrations/traffic-generator/start"><input name="integration_csrf" value="csrf-secret"><input name="users" value="50"><button type="submit">Start</button></form></section>';
function setup(){
  const dom=new JSDOM('<main class="page"><svg id="live-chart"></svg><a data-simulator-modal="workload" href="/tests#corporate-traffic">Control</a></main>'+dialogHTML,{url:'https://netem.test/',runScripts:'outside-only'});
  const w=dom.window;let cleared=0;
  w.setInterval=()=>1;w.clearInterval=()=>{cleared++;};
  const dialog=w.document.querySelector('dialog');
  dialog.showModal=()=>dialog.setAttribute('open','');dialog.close=()=>dialog.removeAttribute('open');
  w.eval(fs.readFileSync('static/actions.js','utf8'));
  w.eval(fs.readFileSync('static/simulator-dialog.js','utf8'));
  return {dom,w,dialog,cleared:()=>cleared};
}

test('workload controls open and submit in a modal without replacing live charts or navigating',async()=>{
  const {dom,w,dialog}=setup();const requests=[];
  w.fetch=async(url,options={})=>{requests.push({url,options});return {ok:true,text:async()=>workload(options.method==='POST'?'running':'idle')};};
  const chart=w.document.getElementById('live-chart');w.document.querySelector('[data-simulator-modal]').click();await wait();
  assert.equal(dialog.open,true);
  const form=dialog.querySelector('form');form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await wait();
  const posted=requests.find(r=>r.options.method==='POST');
  assert.equal(posted.url,'https://netem.test/integrations/traffic-generator/start');
  assert.equal(posted.options.body.get('integration_csrf'),'csrf-secret');
  assert.equal(posted.options.body.get('users'),'50');
  assert.equal(w.document.getElementById('live-chart'),chart);
  assert.equal(w.location.href,'https://netem.test/');
  assert.equal(dialog.querySelector('[data-tg-status]').textContent,'RUNNING');dom.window.close();
});

test('unconfigured simulator loads integration settings in the same dialog and discovery fills its fields',async()=>{
  const {dom,w,dialog}=setup();
  const integration='<div id="traffic-simulator"></div><div class="grid"><section class="card"><form><select id="traffic-generator-discovered"><option value="manual">Manual</option><option value="192.168.0.135" data-port="8443" data-name="Lab" data-version="0.3.2" data-fingerprint="fingerprint">Lab</option></select><input id="traffic-generator-port"><input id="traffic-generator-instance"><input id="traffic-generator-version"><input id="traffic-generator-fingerprint"></form></section></div>';
  w.fetch=async url=>({ok:true,text:async()=>url.includes('integrations')?integration:'<div id="corporate-traffic"></div><section><a href="/integrations?discover=1">Configure</a></section>'});
  w.document.querySelector('[data-simulator-modal]').click();await wait();
  assert.equal(dialog.querySelector('[data-simulator-title]').textContent,'Configure Traffic Simulator');
  const select=dialog.querySelector('select');select.value='192.168.0.135';select.dispatchEvent(new w.Event('change',{bubbles:true}));
  assert.equal(dialog.querySelector('#traffic-generator-port').value,'8443');
  assert.equal(dialog.querySelector('#traffic-generator-instance').value,'Lab');
  assert.equal(dialog.querySelector('#traffic-generator-fingerprint').value,'fingerprint');
  assert.equal(w.location.pathname,'/');dom.window.close();
});

test('closing a dialog stops polling and ignores late responses',async()=>{
  const {dom,w,dialog,cleared}=setup();let resolve;
  w.fetch=()=>new Promise(r=>{resolve=r;});w.document.querySelector('[data-simulator-modal]').click();
  dialog.querySelector('[data-simulator-close]').click();assert.equal(dialog.open,false);
  resolve({ok:true,text:async()=>workload()});await wait();
  assert.equal(dialog.querySelector('[data-simulator-content]').children.length,0);
  assert.ok(cleared()>0);dom.window.close();
});
