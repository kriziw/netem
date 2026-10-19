const {test, afterEach} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const windows = [];
afterEach(() => windows.splice(0).forEach(w => w.close()));
const source = fs.readFileSync('static/fortinet.js', 'utf8');
const template = fs.readFileSync('templates/fortinet.html', 'utf8');
const appliance = 'a'.repeat(32);
function discovery() {
  return {state: 'discovered', appliance_id: appliance, started_at: 100, message: 'Discovery complete.', steps: [], plan: null,
    discovery: {identity: {hostname: '<img src=x onerror=alert(1)>', model: 'FGT-VM', version: 'v7.6.7'},
      interfaces: [{name: 'port1', role: 'undefined', type: 'physical', cidr: '192.0.2.10/24'},
        {name: 'port2', role: 'lan', type: 'physical', cidr: '10.250.10.1/24'},
        ...[1,2,3].map(n => ({name: 'port' + (n+2), role: 'wan', type: 'physical', cidr: `10.10.${n}.2/30`, gateway: `10.10.${n}.1`}))],
      management_interface: 'port1', sdwan: {status: 'disable', zones: []}, routes: [], policy_count: 0, central_nat: 'disable', suggested_target: '198.18.0.1'}};
}
function planned() {
  return {...discovery(), plan: {id: 'reviewed-plan', appliance_id: appliance, expires_at: Date.now()/1000+600,
    spec: {wans: [{},{},{}]}, warnings: ['Keep console access.'], actions: [{method: 'PUT', table: 'system/sdwan', key: null, before: {}, after: {status:'enable'}}]}};
}
const settle = async () => { for (let n=0;n<5;n++) await new Promise(resolve => setImmediate(resolve)); };
function setup(initial=discovery()) {
  let html = template.slice(template.indexOf('<div id="fortinet-lab"'), template.indexOf('{% endblock %}', template.indexOf('<div id="fortinet-lab"')));
  html = html.replace(/\{%[\s\S]*?%\}/g, '').replace(/\{\{[\s\S]*?\}\}/g, 'token');
  const dom = new JSDOM(html, {url:'https://netem.test/integrations/fortinet', runScripts:'outside-only'});
  const w = dom.window; windows.push(w);
  const find = id => w.document.querySelector('#fortinet-' + id);
  const choice = find('appliance').querySelector('option'); choice.value = appliance;
  const other = w.document.createElement('option'); other.value='b'.repeat(32); other.textContent='Other'; find('appliance').append(other);
  const root = find('lab'); Object.assign(root.dataset, {statusUrl:'/status', discoverUrl:'/discover', previewUrl:'/preview', applyUrl:'/apply', connectionUrl:'/connection'});
  let data = initial, fail = null; const posts=[], timers=[];
  w.setTimeout = (fn, ms) => {if(ms===3000) timers.push(fn);return 1;}; w.clearTimeout=()=>{};
  w.fetch = async (url, options={}) => {
    if (options.method==='POST') {
      posts.push({url, body:options.body});
      if(fail) throw new Error(fail);
      if(url==='/connection') {
        return {ok:true,json:async()=>({saved:true,appliance_id:appliance,name:'Lab FortiGate',endpoint:'https://192.0.2.10',vdom:'root',fingerprint:'',self_signed:false})};
      }
      if(url==='/preview') data=planned();
      if(url==='/apply') data={...discovery(), state:'applying', message:'Applying…'};
      if(url==='/discover') data={...discovery(), state:'discovering', discovery:null};
      return {ok:true,json:async()=>structuredClone(data)};
    }
    return {ok:true,json:async()=>structuredClone(data)};
  };
  w.eval(source); const client=w.NetEmFortinet.mount();
  const submit = id => find(id).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  return {w,find,submit,client,posts,timers,set:value=>{data=value;},fail:value=>{fail=value;}};
}

test('discovery suggests dedicated management, LAN and three WAN interfaces safely', async()=>{
  const t=setup(); await settle();
  assert.equal(t.find('management').value,'port1'); assert.equal(t.find('lan').value,'port2');
  assert.equal(t.find('wan-count').value,'3');
  assert.equal(t.find('wans').querySelectorAll('[data-wan]').length,3);
  assert.equal(t.find('wans').querySelector('[data-gateway]').value,'10.10.1.1');
  assert.equal(t.find('health').value,'198.18.0.1');
  assert.match(t.find('identity').textContent,/<img/); assert.equal(t.find('identity').querySelector('img'),null);
});

test('first-time setup guides token creation, saves a connection privately and continues to discovery', async()=>{
  const t=setup({state:'idle',message:'Begin setup',steps:[]}); await settle();
  assert.equal(t.find('prepare').hidden,false);
  assert.match(t.find('prepare').textContent,/System → Administrators → Create New → REST API Admin/);
  assert.match(t.find('prepare').textContent,/Trusted Hosts/);
  t.find('token-ready').click(); assert.equal(t.find('connection-panel').hidden,false);
  t.find('connection-choice').value=''; t.find('connection-choice').dispatchEvent(new t.w.Event('change'));
  for(const [field,value] of Object.entries({name:'Lab FortiGate',endpoint:'https://192.0.2.10',vdom:'root',token:'private-api-token'})) t.find('connection-'+field).value=value;
  t.submit('connection'); await settle();
  assert.equal(t.posts.length,1);
  assert.equal(t.posts[0].url,'/connection');
  assert.equal(t.posts[0].body.get('api_token'),'private-api-token');
  assert.equal(t.find('connection-token').value,'');
  assert.equal(t.find('discovery-panel').hidden,false);
  assert.equal(t.find('appliance').value,appliance);
  assert.match(t.find('status').textContent,/Connection saved/);
  t.find('edit-connection').click(); assert.equal(t.find('connection-panel').hidden,false);
  assert.match(t.find('token-help').textContent,/stored privately/);
  assert.equal(t.find('connection-token').required,false);
  t.find('token-back').click(); assert.equal(t.find('prepare').hidden,false);
});

test('successful provisioning guides forwarding verification for the selected appliance', async()=>{
  const t=setup(planned()); await settle();
  t.set({...discovery(),state:'completed',message:'Configuration verified.'});
  await t.timers.pop()();
  assert.equal(t.find('finish').hidden,false);
  assert.match(t.find('finish').textContent,/Verify & select/);
  assert.match(t.find('finish-appliance').href,new RegExp('edit='+appliance));
  t.find('appliance').value='b'.repeat(32); t.find('appliance').dispatchEvent(new t.w.Event('change'));
  await t.timers.pop()(); assert.equal(t.find('finish').hidden,true);
});

test('preview submits selected WAN topology and applies only a confirmed single-use plan', async()=>{
  const t=setup(); await settle(); t.submit('configure'); await settle();
  const spec=JSON.parse(t.posts[0].body.get('configuration'));
  assert.equal(spec.wans.length,3); assert.equal(spec.wans[2].interface,'port5');
  assert.equal(t.posts[0].body.get('integration_csrf'),'token');
  assert.equal(t.find('review').hidden,false);
  t.submit('apply'); await settle(); assert.equal(t.posts.length,1);
  t.find('confirm').checked=true; t.submit('apply'); t.submit('apply'); await settle();
  assert.equal(t.posts.length,2); assert.equal(t.posts[1].body.get('plan_id'),'reviewed-plan');
  assert.equal(t.posts[1].body.get('confirmation'),'apply'); assert.equal(t.find('review').hidden,true);
});

test('editing topology invalidates approval even when a poll returns the previous server plan', async()=>{
  const t=setup(); await settle(); t.submit('configure'); await settle();
  t.find('confirm').checked=true;
  t.find('health').value='198.18.0.2'; t.find('health').dispatchEvent(new t.w.Event('input',{bubbles:true}));
  assert.equal(t.find('review').hidden,true); assert.equal(t.find('confirm').checked,false);
  await t.timers.pop()(); assert.equal(t.find('review').hidden,true);
});

test('switching appliances hides stale discovery and approval across status polling', async()=>{
  const t=setup(planned()); await settle();
  t.find('appliance').value='b'.repeat(32); t.find('appliance').dispatchEvent(new t.w.Event('change'));
  await t.timers.pop()();
  assert.equal(t.find('topology').hidden,true); assert.equal(t.find('review').hidden,true);
});

test('uncertain apply is not retried and polling reports partial changes', async()=>{
  const t=setup(planned()); await settle(); t.fail('Network interrupted');
  t.find('confirm').checked=true; t.submit('apply'); t.submit('apply'); await settle();
  assert.equal(t.posts.length,1); assert.match(t.find('status').textContent,/may have been accepted/);
  t.set({...discovery(),state:'needs_review',message:'Changes may have applied.',steps:[{table:'router/static',key:9000,state:'sending'}]});
  await t.timers.pop()();
  assert.match(t.find('steps').textContent,/sending/); assert.equal(t.find('recovery').hidden,false);
  assert.equal(t.posts.length,1); t.client.stop();
});

// Exercise the actual Flask/base template and shared page loader. Loading fortinet.js
// directly in setup() cannot catch scripts accidentally placed in inert templates.
function renderedFortinetPage() {
  const {execFileSync} = require('node:child_process');
  return execFileSync('python', ['-c', `
from flask import render_template
from app import app
app.config['NETEM_AUTH_REQUIRED'] = False
with app.test_request_context('/integrations/fortinet'):
    print(render_template('fortinet.html', page='integrations', appliances=[dict(id='${appliance}', name='Lab FortiGate', endpoint='https://192.0.2.10', vdom='root')], lab=dict(state='idle', message='Ready', appliance_id=None)))
`], {encoding:'utf8', env:{...process.env, NETEM_AUTH_REQUIRED:'0'}});
}

test('rendered shell loads the FortiGate client before initial mount and remounts it after refresh', async()=>{
  const html = renderedFortinetPage();
  const dom = new JSDOM(html, {url:'https://netem.test/integrations/fortinet', runScripts:'outside-only'});
  const w = dom.window; windows.push(w);
  w.scrollTo=()=>{}; w.setTimeout=()=>1; w.setInterval=()=>1;
  w.clearTimeout=()=>{}; w.clearInterval=()=>{};
  let posts=0, reads=0;
  w.fetch=async (url, options={})=>{
    const path=new URL(url,w.location.href).pathname;
    if (options.method==='POST') {
      posts++;
      assert.equal(path,'/integrations/fortinet/discover');
      assert.equal(options.body.get('id'),appliance);
      assert.ok(options.body.get('integration_csrf'));
      return {ok:true,json:async()=>discovery()};
    }
    if(path==='/api/v1/integrations/fortinet') {
      reads++;
      return {ok:true,json:async()=>({state:'idle',message:'Ready',steps:[]})};
    }
    return {ok:true,url:String(url),text:async()=>html};
  };
  // Match browser parsing: template.content scripts are inert and aren't in this list.
  for(const script of w.document.querySelectorAll('script')) {
    const path=script.getAttribute('src');
    if(path?.endsWith('/actions.js') || path?.endsWith('/fortinet.js')) {
      w.eval(fs.readFileSync('static/'+path.split('/').pop(),'utf8'));
    } else if(!path && script.textContent.trim()==='NetEmActions.mount();') {
      w.eval(script.textContent);
    }
  }
  await settle();
  assert.equal(typeof w.NetEmFortinet?.mount,'function');
  assert.equal(reads,1);
  assert.match(w.document.querySelector('#fortinet-appliance').textContent,/VDOM: root/);
  await w.NetEmActions.refresh(); await settle();
  assert.equal(reads,2);
  const form=w.document.querySelector('#fortinet-discover');
  form.dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  await settle();
  assert.equal(posts,1);
  assert.equal(w.document.querySelector('#fortinet-status').textContent,'Discovery complete.');
});
