const {test} = require('node:test');
const assert = require('node:assert/strict');
const Recovery = require('../orchestrator/web/static/update-recovery.js');

function fixture(states = ['current']) {
  let time = 1000, context = {machineId:'mac', project:'/project', owner:true}, polls = 0;
  const op = {id:'op', action:'update', state:'requested', created:1, expires:1801};
  const machine = {id:'mac', name:'Mac', online:true, reachable:true, updatable:true, update_control:1, capabilities:{preview_remote:false}};
  const calls = [], shown = [], stored = new Map();
  const options = {context:()=>context, now:()=>time, sleep:async()=>{time+=2000},
    storage:{getItem:k=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,v)},
    render:v=>shown.push(v), reconnect:async(machine,isCurrent)=>{assert.equal(isCurrent(),true);calls.push('reconnect')}, probe:async()=>true,
    request:async(path,{body}={})=>{
      calls.push({path,body});
      if(path==='machines/update-recovery')return {update_operation:op};
      if(path==='machines/cancel-update')return {update_operation:{...op,action:'cancel',state:'cancelling'}};
      return {machines:[{...machine,update_operation:{...op,state:states[Math.min(polls++,states.length-1)]}}]};
    }};
  return {options,calls,shown,stored,machine,op,context: v=>context=v,controller:()=>Recovery.create(options)};
}
const missing = {status:404,message:'Not found'}, oldRunner = {capabilities:{preview_remote:false}};

test('ordinary failures and a supported preview never request an update', async()=>{
  const f=fixture(),c=f.controller();
  for(const e of [{status:403},{status:409},{status:502},{status:404,message:'Branch not found'}]) {
    assert.equal(await c.recover(e,{}),false);
  }
  assert.equal(await c.recover(missing,{capabilities:{preview_remote:true}}),false);
  assert.equal(await c.recover(missing,{}),false);
  assert.equal(await c.recover(missing,undefined),false);
  assert.equal(f.calls.length,0);
});

test('unknown machine capability does not turn a generic 404 into an update',async()=>{
  const f=fixture();f.machine.capabilities={};
  assert.equal(await f.controller().recover(missing,oldRunner),false);
  assert(!f.calls.some(x=>x.path==='machines/update-recovery'));
});
test('confirmed missing capability updates, reconnects and verifies exactly once',async()=>{
  const f=fixture(['requested','downloading','updating','current']),c=f.controller();
  assert.equal(await c.recover(missing,oldRunner),true);
  assert.equal(f.calls.filter(x=>x.path==='machines/update-recovery').length,1);
  assert.equal(f.calls.filter(x=>x==='reconnect').length,1);
  assert(f.shown.some(x=>x.state==='downloading'));
  assert.equal(await c.recover(missing,oldRunner),false);
});
test('legacy and non-owner installations never get a fake automatic update',async()=>{
  for(const override of [{updatable:false},{update_control:0},{online:false}]) {
    const f=fixture();Object.assign(f.machine,override);
    assert.equal(await f.controller().recover(missing,oldRunner),false);
    assert(!f.calls.some(x=>x.path==='machines/update-recovery'));
    assert.equal(f.shown.at(-1).state,'unavailable');
  }
  const f=fixture();f.context({machineId:'mac',project:'/project',owner:false});
  assert.equal(await f.controller().recover(missing,oldRunner),false);assert.equal(f.calls.length,0);
});
test('cancel waits for acknowledgement, survives lost connection, suppresses repeat',async()=>{
  const f=fixture(['requested','downloading','cancelling','cancelled']);let c,asked=false;
  f.options.render=v=>{f.shown.push(v);if(v.state==='downloading'&&!asked){asked=true;void c.cancel()}};
  c=f.controller();assert.equal(await c.recover(missing,oldRunner),false);
  assert(f.calls.some(x=>x.path==='machines/cancel-update'&&x.body.operation_id==='op'));
  assert.equal(f.shown.at(-1).state,'cancelled');
  assert.equal(await c.recover(missing,oldRunner),false);
});
test('machine/project change never reconnects or opens stale preview',async()=>{
  const f=fixture(['requested','updating','current']);
  f.options.sleep=async()=>f.context({machineId:'another',project:'/project',owner:true});
  assert.equal(await f.controller().recover(missing,oldRunner),false);
  assert(!f.calls.includes('reconnect'));
});
test('reload resumes recorded operation without requesting another update',async()=>{
  const f=fixture(['requested','checking','current']);let interrupt=true;
  f.options.sleep=async()=>{if(interrupt){interrupt=false;f.context({machineId:'other',project:'/project',owner:true})}};
  await f.controller().recover(missing,oldRunner);
  f.context({machineId:'mac',project:'/project',owner:true});
  assert.equal(await f.controller().resume(),true);
  assert.equal(f.calls.filter(x=>x.path==='machines/update-recovery').length,1);
});
test('an updater saying current without preview support is a terminal failure',async()=>{
  const f=fixture();f.options.probe=async()=>false;
  assert.equal(await f.controller().recover(missing,oldRunner),false);
  assert.equal(f.shown.at(-1).state,'failed');
  assert.equal(await f.controller().recover(missing,oldRunner),false);
});
test('install claim wins cancel race without a false cancellation message',async()=>{
  const f=fixture(['requested','checking','updating','current']);let c;
  const request=f.options.request;
  f.options.request=async(path,args)=>{if(path==='machines/cancel-update')throw Object.assign(Error('Installing'),{status:409});return request(path,args)};
  f.options.render=v=>{f.shown.push(v);if(v.state==='checking')void c.cancel()};c=f.controller();
  assert.equal(await c.recover(missing,oldRunner),true);
  assert(!f.shown.some(x=>x.state==='cancelled'));
});
test('offline polling has a deadline and installing is never falsely cancelled',async()=>{
  const f=fixture(['requested','updating']);f.options.sleep=async()=>{f.options.now=()=>time;};
  let time=1000;f.options.now=()=>time;f.options.sleep=async()=>{time+=1800001};
  assert.equal(await f.controller().recover(missing,oldRunner),false);
  assert(f.shown.at(-1).message.includes('Check Computers'));
});

test('a disconnected requested operation times out without claiming cancellation',async()=>{
  const f=fixture(['requested']);let time=1000;f.options.now=()=>time;f.options.sleep=async()=>{time+=1800001};
  assert.equal(await f.controller().recover(missing,oldRunner),false);
  assert.equal(f.shown.at(-1).state,'failed');assert.equal(f.shown.at(-1).done,true);
});

test('pending request deduplicates and accepts cancellation before delivery returns',async()=>{
  const f=fixture(['requested','cancelling','cancelled']);let c,asked=false;
  f.options.render=v=>{f.shown.push(v);if(v.state==='requested'&&!asked){asked=true;void c.cancel()}};
  c=f.controller();const a=c.recover(missing,oldRunner),b=c.recover(missing,oldRunner);
  assert.equal(a,b);assert.equal(await a,false);
  assert(f.calls.some(x=>x.path==='machines/cancel-update'));
});
test('lost cancellation connection is retried until acknowledged',async()=>{
  const f=fixture(['requested','downloading','cancelling','cancelled']);let c,asked=false,fail=true;
  const request=f.options.request;
  f.options.request=async(path,args)=>{if(path==='machines/cancel-update'&&fail){fail=false;throw Error('offline')}return request(path,args)};
  f.options.render=v=>{f.shown.push(v);if(v.state==='downloading'&&!asked){asked=true;void c.cancel()}};
  c=f.controller();assert.equal(await c.recover(missing,oldRunner),false);
  assert(f.shown.some(x=>x.message?.includes('Cancellation is pending')));
  assert.equal(f.shown.at(-1).state,'cancelled');
});
test('denied cancellation is reported without pretending update stopped',async()=>{
  const f=fixture(['requested','checking','current']);let c,asked=false;
  const request=f.options.request;
  f.options.request=async(path,args)=>{if(path==='machines/cancel-update')throw Object.assign(Error('denied'),{status:403});return request(path,args)};
  f.options.render=v=>{f.shown.push(v);if(v.state==='checking'&&!asked){asked=true;void c.cancel()}};
  c=f.controller();await c.recover(missing,oldRunner);
  assert.equal(f.shown.at(-1).state,'failed');assert(!f.shown.some(x=>x.state==='cancelled'));
});
test('removed computer, replaced operation and denied polling are terminal',async()=>{
  for(const mode of ['removed','replaced','denied','legacy-control']){
    const f=fixture(),request=f.options.request;let n=0;
    f.options.request=async(path,args)=>{
      if(mode==='legacy-control'&&path==='machines/update-recovery')return {};
      if(path==='machines'&&n++>0){
        if(mode==='removed')return {machines:[]};
        if(mode==='replaced')return {machines:[{...f.machine,update_operation:{...f.op,id:'another'}}]};
        if(mode==='denied')throw Object.assign(Error('denied'),{status:401});
      }return request(path,args);
    };
    assert.equal(await f.controller().recover(missing,oldRunner),false);
    assert.equal(f.shown.at(-1).done,true);
  }
});
test('transient polling/reconnect failures recover without another update request',async()=>{
  const f=fixture(),request=f.options.request;let failPoll=true,failReconnect=true;
  f.options.request=async(path,args)=>{if(path==='machines'&&f.calls.some(x=>x.path==='machines/update-recovery')&&failPoll){failPoll=false;throw Error('network')}return request(path,args)};
  f.options.reconnect=async()=>{if(failReconnect){failReconnect=false;throw Error('restarting')}f.calls.push('reconnect')};
  assert.equal(await f.controller().recover(missing,oldRunner),true);
  assert(f.shown.some(x=>x.message?.includes('Waiting for the computer connection')));
  assert.equal(f.calls.filter(x=>x.path==='machines/update-recovery').length,1);
});
test('no context, invalid storage, throwing storage and absent resume are safe',async()=>{
  const f=fixture(),c=f.controller();assert.equal(await c.cancel(),false);assert.equal(await c.resume(),false);
  f.context({owner:true});assert.equal(await c.recover(missing,oldRunner),false);
  for(const getItem of [()=>'{invalid',()=>{throw Error('blocked')}]){
    const g=fixture();g.options.storage={getItem,setItem:()=>{throw Error('blocked')}};
    assert.equal(await g.controller().recover(missing,oldRunner),true);
  }
  const g=fixture();g.machine.capabilities.preview_remote=true;
  assert.equal(await g.controller().recover(missing,oldRunner),false);
  assert(!g.calls.some(x=>x.path==='machines/update-recovery'));
});
test('initial request error is actionable, explicit retry can start a fresh attempt',async()=>{
  const f=fixture(),request=f.options.request;let first=true;
  f.options.request=async(path,args)=>{if(path==='machines/update-recovery'&&first){first=false;throw Object.assign(Error('request failed'),{status:403})}return request(path,args)};
  const c=f.controller();assert.equal(await c.recover(missing,oldRunner),false);
  assert.equal(f.shown.at(-1).state,'failed');assert.equal(await c.recover(missing,oldRunner,{force:true}),true);
});
test('cancel is disabled after install begins and timeout preserves an installing operation',async()=>{
  const f=fixture(['requested','updating']);let c,time=1000,installed=false;
  f.options.now=()=>time;f.options.sleep=async()=>{time+=installed?1800001:2000};
  f.options.render=v=>{f.shown.push(v);if(v.state==='updating'&&!v.done){installed=true;void c.cancel()}};
  c=f.controller();assert.equal(await c.recover(missing,oldRunner),false);
  assert(!f.calls.some(x=>x.path==='machines/cancel-update'));
  assert.equal(f.shown.at(-1).state,'updating');assert.equal(f.shown.at(-1).canRetry,false);
});
test('context changes during initial lookup/reconnection/probe never retry preview',async()=>{
  for(const stage of ['lookup','reconnect','probe']){
    const f=fixture(),request=f.options.request;
    const change=()=>f.context({machineId:'other',project:'/project',owner:true});
    if(stage==='lookup')f.options.request=async(...args)=>{const r=await request(...args);change();return r};
    if(stage==='reconnect')f.options.reconnect=async()=>change();
    if(stage==='probe')f.options.probe=async()=>{change();return true};
    assert.equal(await f.controller().recover(missing,oldRunner),false);
  }
});

test('modal renders progress safely and sends real Cancel, Close and explicit Retry actions',async()=>{
  const fs=require('fs'),vm=require('vm');let options,cancelled=0,retried=0,routed=0;
  const listeners={},dialog={open:false,innerHTML:'',showModal(){this.open=true},close(){this.open=false},
    addEventListener:(name,fn)=>listeners[name]=fn,
    querySelector(selector){
      if(selector.includes('close'))return this.innerHTML.includes('data-preview-update-close')?{}:null;
      if(selector.includes('cancel'))return this.innerHTML.includes('data-preview-update-cancel')?{disabled:this.innerHTML.includes(' disabled')}:null;
    }};
  const controller={cancel:()=>{cancelled++},recover:async(e,r,p)=>{assert.equal(p.force,true);retried++}};
  const c={previewRecovery:null,PreviewUpdateRecovery:{create:o=>{options=o;return controller}},$:()=>dialog,
    cpApi:()=>{},previewRecoveryContext:()=>({}),localStorage:{getItem:()=>null,setItem:()=>{}},
    signInToMachine:async()=>({token:'test'}),refreshState:async()=>{},api:async()=>({runner:{capabilities:{preview_remote:true}}}),
    esc:s=>String(s).replaceAll('<','&lt;').replaceAll('>','&gt;'),state:{runner:oldRunner},current:{page:'preview'},route:()=>{routed++},Date};
  const source=fs.readFileSync(require('path').join(__dirname,'../orchestrator/web/static/app.js'),'utf8');
  const start=source.indexOf('function getPreviewRecovery('),end=source.indexOf('// After Google',start);
  vm.createContext(c);vm.runInContext(source.slice(start,end),c);c.getPreviewRecovery();
  options.render({state:'downloading',name:'<Mac>',message:'Downloading',canCancel:true,done:false});
  assert(dialog.open);assert(dialog.innerHTML.includes('&lt;Mac&gt;'));assert(!dialog.innerHTML.includes('<Mac>'));
  const target=selector=>({closest:s=>s===selector?{}:null});
  listeners.click({target:target('[data-preview-update-cancel]')});assert.equal(cancelled,1);
  options.render({state:'updating',name:'Mac',message:'Finishing',canCancel:false,done:false});
  let prevented=false;listeners.cancel({preventDefault:()=>{prevented=true}});assert(prevented);assert.equal(cancelled,1);
  options.render({state:'failed',name:'Mac',message:'Failed',done:true,canRetry:true});
  listeners.click({target:target('[data-preview-update-close]')});assert(!dialog.open);
  listeners.click({target:target('[data-preview-update-retry]')});await Promise.resolve();assert.equal(retried,1);assert.equal(routed,1);
  options.render({state:'current'});assert(!dialog.open);
  await options.reconnect({id:'mac'},()=>true);assert.equal(await options.probe(),true);
  await assert.rejects(options.reconnect({id:'mac'},()=>false),/selected computer changed/);
});

test('default poll scheduling, active state and memory-only dedup work',async()=>{
  const f=fixture(['requested','checking','current']);delete f.options.sleep;
  f.options.storage={getItem:()=>null,setItem:()=>{throw Error('blocked')}};
  const original=setTimeout;global.setTimeout=(callback,ms)=>{assert.equal(ms,2000);callback()};
  try {
    const c=f.controller();assert.equal(c.active,false);
    const pending=c.recover(missing,oldRunner);assert.equal(c.active,true);assert.equal(c.resume(),pending);
    assert.equal(await pending,true);assert.equal(c.active,false);
    assert.equal(await c.recover(missing,oldRunner),false);
  } finally { global.setTimeout=original; }
});
test('recover resumes persisted operation, rejects lost resume state and empty machine list',async()=>{
  const f=fixture(['requested','checking','current']);let pause=true;
  f.options.sleep=async()=>{if(pause){pause=false;f.context({machineId:'other',project:'/project',owner:true})}};
  await f.controller().recover(missing,oldRunner);f.context({machineId:'mac',project:'/project',owner:true});
  assert.equal(await f.controller().recover(missing,oldRunner),true);
  assert.equal(f.calls.filter(x=>x.path==='machines/update-recovery').length,1);
  for(const invalid of [undefined,{id:'other'}]){
    const g=fixture(['requested','checking']);g.options.sleep=async()=>g.context({machineId:'other',project:'/project',owner:true});
    await g.controller().recover(missing,oldRunner);g.context({machineId:'mac',project:'/project',owner:true});
    g.options.request=async()=>({machines:[{...g.machine,update_operation:invalid}]});
    assert.equal(await g.controller().resume(),false);assert.equal(g.shown.at(-1).state,'failed');
  }
  const g=fixture();g.options.request=async()=>({});assert.equal(await g.controller().recover(missing,oldRunner),false);
});
test('context change during monitor lookup and cancellation rejection inside monitor terminate safely',async()=>{
  const f=fixture(),request=f.options.request;
  f.options.request=async(path,args)=>{const reply=await request(path,args);if(path==='machines'&&f.calls.some(x=>x.path==='machines/update-recovery'))f.context({machineId:'other',project:'/project',owner:true});return reply};
  assert.equal(await f.controller().recover(missing,oldRunner),false);
  const g=fixture(['requested','checking','cancelling']);let c,cancelAttempts=0,asked=false;
  const send=g.options.request;
  g.options.request=async(path,args)=>{if(path==='machines/cancel-update'){cancelAttempts++;if(cancelAttempts===1)throw Error('offline');throw Object.assign(Error('denied'),{status:401})}return send(path,args)};
  g.options.render=v=>{g.shown.push(v);if(v.state==='requested'&&!asked){asked=true;void c.cancel()}};
  c=g.controller();assert.equal(await c.recover(missing,oldRunner),false);assert.equal(g.shown.at(-1).state,'failed');
});
test('network outages while cancelling and installing retain truthful progress',async()=>{
  for(const stage of ['cancelling','updating']){
    const f=fixture(['requested',stage,'current']),request=f.options.request;let poll=0,c;
    f.options.request=async(path,args)=>{if(path==='machines'&&++poll===3)throw Error('offline');return request(path,args)};
    if(stage==='cancelling'){
      let asked=false;f.options.render=v=>{f.shown.push(v);if(v.state==='cancelling'&&!asked){asked=true;void c.cancel()}};
    }
    c=f.controller();await c.recover(missing,oldRunner);
    assert(f.shown.some(v=>v.message.includes('Waiting for the computer connection')));
  }
  const f=fixture();f.options.request=async()=>{throw {}};
  assert.equal(await f.controller().recover(missing,oldRunner),false);assert.equal(f.shown.at(-1).message,Recovery.messages.failed);
});

test('committed update with lost response remains cancellable without duplicate submission',async()=>{
  const f=fixture(['requested','downloading','cancelling','cancelled']),request=f.options.request;let c,asked=false;
  f.options.request=async(path,args)=>{if(path==='machines/update-recovery'){await request(path,args);throw Error('response lost')}return request(path,args)};
  f.options.render=v=>{f.shown.push(v);if(v.state==='downloading'&&!asked){asked=true;void c.cancel()}};
  c=f.controller();assert.equal(await c.recover(missing,oldRunner),false);
  assert.equal(f.shown.at(-1).state,'cancelled');
  assert.equal(f.calls.filter(x=>x.path==='machines/update-recovery').length,1);
  assert(f.calls.some(x=>x.path==='machines/cancel-update'));
});
test('reload recovers an ambiguous update creation after delayed server commit',async()=>{
  const f=fixture(['requested','downloading','current']),request=f.options.request;let after=false,interrupted=false,visibilityLag=0;
  f.options.request=async(path,args)=>{
    if(path==='machines/update-recovery'){after=true;await request(path,args);throw Error('response lost')}
    if(after&&!interrupted){interrupted=true;f.context({machineId:'other',project:'/project',owner:true});return {machines:[f.machine]}}
    if(after&&interrupted&&visibilityLag++===0)return {machines:[f.machine]};
    return request(path,args);
  };
  await f.controller().recover(missing,oldRunner);f.context({machineId:'mac',project:'/project',owner:true});
  assert.equal(await f.controller().resume(),true);
  assert.equal(f.calls.filter(x=>x.path==='machines/update-recovery').length,1);
});
test('late remote-preview rejection cannot initiate recovery after a context switch',async()=>{
  const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(require('path').join(__dirname,'../orchestrator/web/static/app.js'),'utf8');
  const start=source.indexOf('const showRemoteLive = async'),end=source.indexOf('if (card.dataset.remoteLive',start);
  let reject,current=true,requests=0;
  const c={showOverlay:()=>{},api:()=>new Promise((ok,no)=>{reject=no}),branch:'main',samePreview:()=>current,
    PreviewUpdateRecovery:Recovery,Account:{active:()=>true},state:{runner:oldRunner},getPreviewRecovery:()=>({recover:async()=>{requests++;return false}})};
  vm.createContext(c);vm.runInContext(source.slice(start,end).replace('const showRemoteLive','globalThis.showRemoteLive'),c);
  const result=c.showRemoteLive();current=false;reject(missing);await result;
  assert.equal(requests,0);
});

test('ambiguous creation waits for delayed operation visibility and preserves early cancellation',async()=>{
  for(const shouldCancel of [false,true]) {
    const f=fixture(shouldCancel?['requested','checking','cancelling','cancelled']:['requested','checking','current']);
    const request=f.options.request;let started=false,hidden=0,c,asked=false;
    f.options.request=async(path,args)=>{
      if(path==='machines/update-recovery'){started=true;await request(path,args);throw Error('response lost')}
      if(path==='machines'&&started&&hidden++<2)return {machines:[f.machine]};
      return request(path,args);
    };
    f.options.render=v=>{f.shown.push(v);if(shouldCancel&&v.message.includes('Confirming whether')&&!asked){
      asked=true;assert(v.canCancel);void c.cancel();
    }};
    c=f.controller();assert.equal(await c.recover(missing,oldRunner),!shouldCancel);
    assert.equal(f.calls.filter(x=>x.path==='machines/update-recovery').length,1);
  }
});

test('immediately completed update is reconciled after its creation response is lost',async()=>{
  const f=fixture(['current']),request=f.options.request;
  f.options.request=async(path,args)=>{if(path==='machines/update-recovery'){await request(path,args);throw Error('lost response')}return request(path,args)};
  assert.equal(await f.controller().recover(missing,oldRunner),true);
});

test('Computers renders recovery progress and blocks duplicate manual updates',()=>{
  require('../orchestrator/web/static/html.js');
  require('../orchestrator/web/static/account.js');
  for(const state of ['requested','checking','downloading','waiting_for_work','updating','cancelling']) {
    const html=global.Account.renderMachines([{id:'mac',name:'Mac',online:true,updatable:true,update_operation:{state}}]);
    assert(html.includes(global.Account.UPDATE_PROGRESS[state]));
    assert(!html.includes('data-account-action="update"'));
  }
});

test('reopening a cancelled attempt after reload offers explicit retry without auto-starting',async()=>{
  const f=fixture(['requested','cancelled']);
  await f.controller().recover(missing,oldRunner);
  const count=f.calls.filter(x=>x.path==='machines/update-recovery').length;
  assert.equal(await f.controller().recover(missing,oldRunner),false);
  assert.equal(f.shown.at(-1).state,'cancelled');assert.equal(f.shown.at(-1).canRetry,true);
  assert.equal(f.calls.filter(x=>x.path==='machines/update-recovery').length,count);
});
