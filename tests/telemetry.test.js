const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {window: {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/app.js', 'utf8'), context);
const ui = context.window.NetEmUI;
function sample(t=10, down=100, up=200, downPackets=10, upPackets=20) {
  return {id:'wan1', monotonic_timestamp:t, timestamp:1000+t, counters_valid:true,
          inner:{interface:'inside',ifindex:1}, outer:{interface:'outside',ifindex:2},
          traffic:{download:{bytes:down,packets:downPackets}, upload:{bytes:up,packets:upPackets}}};
}
const sampler = {sampler_id:'process-one'};
function rates(current, before, now=sampler, previous=sampler) {
  return JSON.parse(JSON.stringify(ui.trafficRates(current,before,now,previous)));
}
const missing = {down:null,up:null,rxpps:null,txpps:null};
test('decimal Mbps, correct direction, PPS, and clock correction', () => {
  const current = sample(12,250100,125200,210,120);
  current.timestamp=-5000;
  assert.deepEqual(rates(current,sample()), {down:1,up:.5,rxpps:100,txpps:50});
});
test('idle is zero; first observations and service restarts are missing', () => {
  assert.deepEqual(rates(sample(12),sample()), {down:0,up:0,rxpps:0,txpps:0});
  assert.deepEqual(rates(sample(),null), missing);
  assert.deepEqual(rates(sample(12),sample(),{sampler_id:'restarted'}), missing);
});
test('counter reset, unreadable stats, remapping, device replacement and nonpositive time rebaseline', () => {
  const baseline=sample();
  for(const changed of [Object.assign(sample(12),{counters_valid:false}),
                        sample(10), sample(9), sample(12,99), sample(12,100,199),
                        sample(12,100,200,9), sample(12,100,200,10,19)]) {
    assert.deepEqual(rates(changed,baseline),missing);
  }
  for(const side of ['inner','outer']) {
    for(const field of ['interface','ifindex']) {
      const changed=sample(12);
      changed[side][field]='replacement';
      assert.deepEqual(rates(changed,baseline),missing);
    }
  }
});
test('failed read and recovery cannot turn cumulative counters into a rate spike', () => {
  const failed=sample(12); failed.counters_valid=false;
  const recovered=sample(14,100000000,200000000);
  assert.deepEqual(rates(recovered,failed),missing);
  assert.deepEqual(rates(sample(16,100250000,200125000),recovered),{down:1,up:.5,rxpps:0,txpps:0});
});
test('missing readings display as unknown and charts retain gaps', () => {
  assert.equal(ui.formatRate(null),'—');
  assert.equal(ui.formatPps(null),'—');
  assert.equal(ui.formatRate(0),'0 bit/s');
  const element={setAttribute(name,value){this[name]=value;}};
  ui.setPath(element,[1,null,2]);
  assert.equal((element.d.match(/M/g)||[]).length,2);
  assert.equal((element.d.match(/L/g)||[]).length,0);
  ui.renderSeries([element],[[null,null]]);
  assert.equal(element.d,'');
});
test('live client propagates gaps and valid samples through polling', async () => {
  const series=[sample(),sample(12,250100,125200,210,120),sample(14)];
  const liveContext={window:{},setTimeout:()=>1,clearTimeout:()=>{}};
  let turn=0;
  liveContext.fetch=async (url)=>({ok:true,json:async()=>url.includes('telemetry')
    ? {timestamp:series[turn].timestamp,sampler_id:'one',links:[series[turn]]}
    : {links:[{id:'wan1',effective:{delay_ms:20},quality:80}]}});
  let done;
  liveContext.setTimeout=(fn)=>{done=fn;return 1;};
  vm.createContext(liveContext);
  vm.runInContext(fs.readFileSync('static/app.js','utf8'),liveContext);
  const client=liveContext.window.NetEmUI.createLiveClient();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(client.history.wan1.down[0],null);
  turn=1; await done();
  assert.equal(client.history.wan1.down[1],1);
  turn=2; await done();
  assert.equal(client.history.wan1.down[2],null);
  client.stop();
});
