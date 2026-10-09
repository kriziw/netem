const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const markup = `<form id="site-selection"><select name="industry"></select><select name="sub_industry"></select><select name="function"></select><select name="size"></select><select name="criticality"></select><button id="site-save"></button></form><p id="site-criticality-help"></p><p id="site-save-hint"></p><div id="site-preview"></div><form id="site-run" data-connected="true" data-busy="false"><select name="primary_link"><option value="wan1">WAN1</option></select><select name="backup_link"><option value="wan2">WAN2</option><option value="wan1">WAN1</option></select><input name="users"><button data-site-action></button><button data-site-run></button><div id="site-test-list"></div></form><p id="site-run-hint"></p><div id="site-results"></div>`;
const options = {industries:{manufacturing:{label:'Manufacturing',sub_industries:{automotive:{label:'Automotive'}},functions:['plant']},retail:{label:'Retail',sub_industries:{grocery:{label:'Grocery'}},functions:['store']}},functions:{plant:{label:'Plant'},store:{label:'Store'}},sizes:{large:'Large'},criticality:{business_critical:{label:'Business-critical',description:'Seconds'}}};
const selection = {industry:'manufacturing',sub_industry:'automotive',function:'plant',size:'large',criticality:'business_critical'};
function plan(selection) { return {selection,label:'<script>unsafe</script>',warnings:['<img onerror="bad">'],workload:{employees:100,personas:{engineer:40,ot_device:20},application_weights:{mes:1.3},scaled_to_limit:false},start:{users:60},targets:{experience_min:80,success_min_pct:99,interactive_p95_max_ms:400,steering_max_s:30,media_mode:'realistic'},wan_lines:{primary:{preset:'dia',download_mbit:1000,upload_mbit:1000},backup:{preset:'dia',download_mbit:500,upload_mbit:500},notes:[]},tests:[{id:'baseline',name:'Baseline',description:'Check',role:'primary'}]}; }
function setup(fetch) {
  const dom = new JSDOM(markup,{runScripts:'outside-only'});
  dom.window.fetch = fetch;
  dom.window.eval(fs.readFileSync('static/site-scenarios.js','utf8'));
  return dom;
}
const tick = () => new Promise(resolve => setImmediate(resolve));
test('preview, tests and results escape dynamic content', () => {
  const dom = setup(); const ui = dom.window.NetEmSites;
  assert.ok(ui.previewHtml(plan(selection)).includes('&lt;script&gt;'));
  assert.ok(!ui.previewHtml(plan(selection)).includes('<img'));
  assert.ok(ui.testsHtml([{id:'x" onclick="bad',name:'<b>',description:'&',role:'primary'}]).includes('&quot;'));
  assert.ok(ui.resultsHtml({label:'<b>',tests:[{name:'<img>',status:'failed',error:'<script>'}]}).includes('&lt;script&gt;'));
});
test('cascade filters choices; an unsaved preview disables run and WAN apply', async () => {
  const dom = setup(async url => ({ok:true,json:async () => plan(Object.fromEntries(new URLSearchParams(url.split('?')[1])))}));
  const doc = dom.window.document;
  dom.window.NetEmSites.mount(options,plan(selection),{});
  await tick();
  assert.equal(doc.querySelector('[data-site-run]').disabled,false);
  doc.querySelector('[name="industry"]').value = 'retail';
  doc.querySelector('[name="industry"]').dispatchEvent(new dom.window.Event('change'));
  await tick();
  assert.equal(doc.querySelector('[name="function"]').value,'store');
  assert.equal(doc.querySelector('[name="sub_industry"]').value,'grocery');
  assert.equal(doc.querySelector('[data-site-run]').disabled,true);
  assert.equal(doc.querySelector('[data-site-action]').disabled,true);
});
test('out-of-order previews cannot replace the latest selection; failures disable actions', async () => {
  const requests = [];
  const dom = setup(url => new Promise(resolve => requests.push({url,resolve})));
  const doc = dom.window.document;
  dom.window.NetEmSites.mount(options,plan(selection),{});
  doc.querySelector('[name="industry"]').value = 'retail';
  doc.querySelector('[name="industry"]').dispatchEvent(new dom.window.Event('change'));
  requests[1].resolve({ok:false,json:async () => ({error:'Preview failed'})});
  await tick();
  requests[0].resolve({ok:true,json:async () => plan(selection)});
  await tick();
  assert.equal(doc.getElementById('site-preview').textContent,'Preview failed');
  assert.equal(doc.getElementById('site-save').disabled,true);
  assert.equal(doc.querySelector('[data-site-run]').disabled,true);
});
test('same WAN roles block actions even with a saved site', async () => {
  const dom = setup(async () => ({ok:true,json:async () => plan(selection)}));
  const doc = dom.window.document;
  dom.window.NetEmSites.mount(options,plan(selection),{});
  await tick();
  doc.querySelector('[name="backup_link"]').value = 'wan1';
  doc.getElementById('site-run').dispatchEvent(new dom.window.Event('change'));
  assert.equal(doc.querySelector('[data-site-run]').disabled,true);
  assert.equal(doc.querySelector('[data-site-action]').disabled,true);
});
test('active site targets judge DEM and late steering and require interactive timings', () => {
  const dom = setup();
  dom.window.eval(fs.readFileSync('static/app.js','utf8'));
  const root = dom.window.document.createElement('div');
  root.innerHTML = ['experience','success','response'].map(key => `<div data-dem-indicator="${key}"><span data-dem-symbol></span><span data-dem-value></span><span data-dem-status></span><p data-dem-explanation></p></div>`).join('');
  dom.window.NetEmUI.renderDemSummary(root,{requests:100,experience_score:85,availability_pct:99.5,interactive_p95_ms:300},true,{experience_min:90,success_min_pct:99.9,interactive_p95_max_ms:200});
  assert.equal(root.querySelectorAll('.bad').length,3);
  dom.window.NetEmUI.renderDemSummary(root,{requests:100,p95_ms:100},true,{experience_min:90,success_min_pct:99.9,interactive_p95_max_ms:200});
  assert.ok(root.querySelector('[data-dem-indicator="response"]').classList.contains('unknown'));
  const html = dom.window.NetEmUI.steeringHtml({classes:[{label:'Voice',severity:'good',verdict:'steered',shares:[],text:'Moved',reactions:[{was_used:true,steered_after_seconds:15,label:'WAN1'}]}]}, {steering_max_s:10});
  assert.ok(html.includes('Steering target missed'));
});
