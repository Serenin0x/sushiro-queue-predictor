'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let now=Date.parse('2026-10-14T04:30:00Z');const requests=[],elements=new Map(),storage=new Map(),events={};
class Clock extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
class Node{constructor(){this.value='';this.checked=false;this.textContent='';this.children=[];this.hidden=false;this.disabled=false;this.attrs={};this.classList={add(){},remove(){},toggle(){}};}append(...v){this.children.push(...v);}replaceChildren(...v){this.children=[...v];}setAttribute(k,v){this.attrs[k]=v;}focus(){}}
const $=name=>{if(!elements.has(name))elements.set(name,new Node());return elements.get(name);};
const state={section:'prediction',source:'sapi_miniapp_gateway',day:'2026-10-14',storeId:'3014',storeName:'大融城',storeNames:{'3014':'大融城'},queueType:'ordinary',mode:'auto',number:'4045',issued:'11:01',checkedIn:'no',confirmed:true,detail:null,samplingSeconds:180};
let forecastCalls=0,forecast={available:false,reason:'insufficient_sparse_trend'};
const context={window:{addEventListener:(name,fn)=>events[name]=fn,SushiWaitReference:{arrival:()=>({available:false,state:'waiting'}),trackedForecast:()=>{forecastCalls++;return forecast;},storeWait:()=>({available:false})}},location:{protocol:'https:',hostname:'124.222.57.236'},document:{visibilityState:'visible',getElementById:$,createElement:()=>new Node(),addEventListener:(name,fn)=>events[name]=fn},crypto:require('node:crypto').webcrypto,Uint8Array,Date:Clock,Intl,Blob,AbortSignal,navigator:{sendBeacon:()=>true},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},setTimeout:()=>0,clearTimeout:()=>{},setInterval:()=>0,
 fetch:async(path,options)=>{const body=options?.body?JSON.parse(options.body):null;requests.push({path,body});return {ok:true,json:async()=>path==='/wait-reference.json'?{stores:{}}:body?.action==='list'?{records:[]}:body?{saved:true}:{local_date:state.day,points:[]}};}};
vm.createContext(context);vm.runInContext(fs.readFileSync('src/sushiwait/web/research-ui.js','utf8'),context);
const api=context.window.SushiWaitResearch,settle=async()=>new Promise(r=>setImmediate(r));
(async()=>{
 $('ticket-number').value='4045';$('ticket-issued').value='11:01';$('ticket-record-system').checked=true;
 api.attach({getState:()=>state,today:()=>state.day,navigate:section=>state.section=section,history:()=>[],index:()=>({days:{}})});await settle();
 assert.equal(requests.filter(r=>r.body?.action==='submit').length,0,'editing or checking recording must never submit');
 await $('ticket-predict-form').onsubmit({preventDefault(){}});await settle();
 const submitted=requests.find(r=>r.body?.action==='submit');assert(submitted,'unavailable forecast still recorded after explicit submission');assert.equal(submitted.body.ticket.prediction_reason,'insufficient_sparse_trend');assert.equal(submitted.body.ticket.number,'4045');assert.equal(state.section,'tickets');assert.equal($('ticket-success').hidden,false);
 const before=forecastCalls;now+=180000;api.notify();await settle();assert.equal(forecastCalls,before,'unchecked continuous tracking cannot refresh automatically after manual window');
 assert(requests.some(r=>r.path==='/api/v1/prediction/presence'&&r.body?.visible),'manual refresh requests a bounded viewing lease');
 $('ticket-follow').checked=true;await $('ticket-predict-form').onsubmit({preventDefault(){}});await settle();now+=180001;api.notify();await settle();assert(forecastCalls>before,'continuous tracking refreshes after source cadence');
 context.document.visibilityState='hidden';events.visibilitychange();await settle();const stopped=forecastCalls;now+=180001;api.notify();await settle();assert.equal(forecastCalls,stopped);assert(requests.some(r=>r.body?.visible===false),'hidden view releases model demand');
 context.document.visibilityState='visible';state.section='prediction';$('entry-tab-record').onclick();assert.equal($('research-form').hidden,false);assert.equal($('ticket-predict-form').hidden,true);assert.equal($('entry-tab-record').attrs['aria-selected'],'true');
 $('research-date').value='2026-10-12';$('research-time').value='12:05';$('research-number').value='1234';$('research-checkin').value='unknown';await $('research-form').onsubmit({preventDefault(){}});await settle();assert(requests.some(r=>r.body?.ticket?.entry_mode==='retrospective'&&r.body.ticket.issued_at.startsWith('2026-10-12')),'historical entry accepted without checkboxes');
 context.location.protocol='http:';state.section='prediction';$('research-number').value='5678';const saves=requests.filter(r=>r.body?.action==='submit').length;await $('research-form').onsubmit({preventDefault(){}});assert.equal(requests.filter(r=>r.body?.action==='submit').length,saves,'private capability must not be sent over HTTP');
 assert(requests.every(r=>!r.path.includes('4045')&&!r.path.includes('1234')&&!r.path.includes(submitted.body.owner_secret)));
 console.log('explicit submission, unavailable forecast recording, manual/continuous modes, tab switching, hidden leases and HTTPS privacy passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
