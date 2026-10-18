// State transitions use synthetic status/QR responses, never a real authorization.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../deploy/recovery_web/app.js'),'utf8');
const settle=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
function fixture({cap='A'.repeat(43),login=false,verification=true,status='issued'}={}){
 const nodes=new Map(),calls=[],timers=new Map(),events={},card={dataset:{}};let now=100000,serial=0;
 const state={status,poll:'pending',login,verification,decodeFailure:false,pollDeferred:null,qrDeferred:null};
 const node=id=>{
  if(!nodes.has(id))nodes.set(id,{hidden:!['portal-visual','message','heading','mark','state-label'].includes(id),disabled:false,value:'',textContent:'',src:'',href:'',dataset:{},attrs:{},listeners:{},
   setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];},addEventListener(k,fn){this.listeners[k]=fn;},focus(){},select(){},async decode(){if(state.decodeFailure)throw Error('synthetic decode');}});
  return nodes.get(id);
 };
 class Clock extends Date{static now(){return now;}}
 const schedule=(fn,ms,repeat=false)=>{const id=++serial;timers.set(id,{fn,at:now+ms,ms,repeat});return id;};
 const context={console,URL,Date:Clock,location:{hash:cap?'#'+cap:'',pathname:'/recover/',origin:'https://124.222.57.236'},history:{replaceState(){}},sessionStorage:{getItem(){return null;},setItem(){}},navigator:{clipboard:{async writeText(){}}},
  document:{hidden:false,getElementById:node,querySelector(){return card;},addEventListener(k,fn){events[k]=fn;}},window:{addEventListener(k,fn){events[k]=fn;}},
  setTimeout:(fn,ms)=>schedule(fn,ms),clearTimeout:id=>timers.delete(id),setInterval:(fn,ms)=>schedule(fn,ms,true),clearInterval:id=>timers.delete(id),
  async fetch(url,options){
   const action=url.split('/').at(-1),body=JSON.parse(options.body);calls.push({action,body});
   assert.equal(options.method,'POST');assert.equal(options.credentials,'same-origin');assert.equal(body.cap,'A'.repeat(43));
   if(action==='qr'&&state.qrDeferred)await state.qrDeferred;
   if(action==='poll'&&state.pollDeferred)await state.pollDeferred;
   if(action==='login'){if(body.password!=='synthetic'){return {ok:false,json:async()=>({error:'login_required'})};}state.login=false;}
   const result=action==='status'?{state:state.status,login_required:state.login,verification:state.verification,next_open_at:'2026-10-19T11:00:00+08:00'}:action==='qr'?{image_url:'/recover/image/'+'b'.repeat(32),expires_at:(now+180000)/1000}:action==='poll'?{state:state.poll,verification:state.verification}:{};
   return {ok:true,json:async()=>result};
  }
 };
 vm.runInNewContext(source,context);return {node,state,calls,card,events,context,timers,async flush(){await settle();},async advance(ms){const target=now+ms;while(true){const due=[...timers].filter(([,v])=>v.at<=target).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;const [id,t]=due;now=t.at;if(t.repeat)t.at+=t.ms;else timers.delete(id);await t.fn();await settle();}now=target;await settle();},setNow(value){now=value;}};
}
(async()=>{
 // Merely opening the page only reads status; creating QR requires an explicit click.
 let f=fixture();await f.flush();assert.equal(f.card.dataset.stage,'ready');assert.deepEqual(f.calls.map(v=>v.action),['status']);
 await f.node('start').listeners.click();assert.equal(f.card.dataset.stage,'pending');assert.equal(f.node('qr-wrap').hidden,false);assert.equal(f.node('portal-visual').hidden,true);assert.equal(f.node('qr-countdown').textContent,'2:00 后过期');
 f.state.poll='scanned';await f.advance(4000);assert.equal(f.card.dataset.stage,'scanned');assert.match(f.node('scan-hint').textContent,/已扫描/);
 f.state.poll='authorized';await f.advance(4000);assert.equal(f.card.dataset.stage,'success');assert.equal(f.node('qr-wrap').hidden,true);assert.equal(f.node('statistics').hidden,false);assert.equal(f.timers.size,0);assert.equal(f.node('auth-step-3').dataset.state,'done');
 // Password is cleared immediately; duplicate taps during generation share one request.
 f=fixture({login:true});await f.flush();assert.equal(f.card.dataset.stage,'login');f.node('password').value='synthetic';let release;f.state.qrDeferred=new Promise(r=>release=r);
 const submission=f.node('login-form').listeners.submit({preventDefault(){}});await f.flush();assert.equal(f.node('password').value,'');assert.equal(f.card.dataset.stage,'generating');await f.node('retry').listeners.click();assert.equal(f.calls.filter(v=>v.action==='qr').length,1);release();await submission;assert.equal(f.card.dataset.stage,'pending');
 // Hidden pages stop timers; resuming after expiry never auto-creates another QR.
 f.context.document.hidden=true;f.events.visibilitychange();assert.equal(f.timers.size,0);f.setNow(250001);f.context.document.hidden=false;f.events.visibilitychange();assert.equal(f.card.dataset.stage,'expired');assert.equal(f.node('qr-wrap').hidden,true);assert.equal(f.calls.filter(v=>v.action==='qr').length,1);
 // Failed image decoding cannot expose an old image; refresh has an explicit recovery action.
 f=fixture();await f.flush();f.state.decodeFailure=true;await f.node('start').listeners.click();assert.equal(f.card.dataset.stage,'error');assert.equal(f.node('qr-wrap').hidden,true);assert.equal(f.node('retry').hidden,false);assert.equal(f.node('start').disabled,false);
 // Expiring during a pending poll cannot resurrect an unusable QR or claim success.
 f=fixture();await f.flush();await f.node('start').listeners.click();let releasePoll;f.state.pollDeferred=new Promise(r=>releasePoll=r);const pending=f.advance(4000);await f.flush();f.setNow(250001);f.context.document.hidden=true;f.events.visibilitychange();f.context.document.hidden=false;f.events.visibilitychange();releasePoll();await pending;assert.equal(f.card.dataset.stage,'expired');assert.equal(f.node('qr-wrap').hidden,true);
 // Incident authorization waits for saved source progress; no success animation is fabricated.
 f=fixture({verification:false,status:'authorized'});await f.flush();assert.equal(f.card.dataset.stage,'restoring');assert.equal(f.node('statistics').hidden,true);f.state.status='scheduled';await f.advance(4000);assert.equal(f.card.dataset.stage,'success');assert.match(f.node('message').textContent,/11:00/);
 f=fixture({verification:false,status:'confirmation_unknown'});await f.flush();assert.equal(f.card.dataset.stage,'unknown');assert.equal(f.node('retry').hidden,true);assert.equal(f.node('recheck').hidden,false);assert.equal(f.timers.size,0);
 f=fixture({cap:null});await f.flush();assert.equal(f.calls.length,0);assert.equal(f.card.dataset.stage,'error');assert.equal(f.node('retry').hidden,true);
 console.log('8 authorization animation, state, race and failure workflows passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
