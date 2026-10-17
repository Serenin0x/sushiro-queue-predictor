'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
// Exercise actual navigation and async reads, not CSS implementation snapshots.
const nodes=new Map(),reads=[],listeners={},routes=[];
let document;
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.value='';this.hidden=false;this.textContent='';this.dataset={};this.attributes={};this.style={};this.checked=false;this.isConnected=true;this.open=false;this.listeners={};const classes=new Set();this.classList={add:n=>classes.add(n),remove:n=>classes.delete(n),contains:n=>classes.has(n),toggle(n,on){const next=on===undefined?!classes.has(n):on;next?classes.add(n):classes.delete(n);}};}
 append(...items){this.children.push(...items);items.forEach(e=>e.isConnected=true);}
 replaceChildren(...items){this.children.forEach(e=>e.isConnected=false);this.children=[];this.append(...items);}
 setAttribute(k,v){this.attributes[k]=v;}removeAttribute(k){delete this.attributes[k];}
 addEventListener(name,handler){this.listeners[name]=handler;}
 get selectedOptions(){return this.children.filter(e=>e.value===this.value);}get options(){return this.children;}
 focus(){document.activeElement=this;}getBoundingClientRect(){return {width:700,height:300,left:0,top:0,right:700,bottom:300};}
 getContext(){return new Proxy({},{get:()=>()=>{}});}showModal(){this.open=true;}close(){this.open=false;this.listeners.close?.();}
}
const $=id=>{if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);};
document={getElementById:$,createElement:tag=>new Element(tag),body:new Element('body'),activeElement:null,addEventListener(name,fn){listeners['document:'+name]=fn;}};
$('sushiwait-reference').dataset={engineeringPanels:'true',engineeringLayout:'tasks-v2',statisticsSource:'sapi_miniapp_gateway'};
$('queue').value='mixedQueue';$('overview-scope').value='observed';$('overview-sort').value='latest';$('records-sort').value='desc';$('records-size').value='20';$('quality-filter').value='all';$('series-1').checked=true;
const at=Date.parse('2026-10-13T21:10:00+08:00');class Clock extends Date{constructor(...args){super(...(args.length?args:[at]));}static now(){return at;}}
const points=[
 {minute:'00',numbers:['100','103','106'],ok:true,comparison:'packet_continuity_unknown'},
 {minute:'03',numbers:['108','109','110'],ok:true,comparison:'comparable_display_sets',interval:180},
 {minute:'06',numbers:null,ok:false,comparison:'failed_pair'},
 {minute:'09',numbers:['150'],ok:true,comparison:'packet_continuity_unknown'},
 {minute:'12',numbers:[],ok:true,comparison:'packet_continuity_unknown'},
 {minute:'15',numbers:null,ok:true,comparison:'packet_continuity_unknown'},
].map(v=>({request_started_at:`2026-10-13T18:${v.minute}:00+08:00`,queue_received_at:v.ok?`2026-10-13T18:${v.minute}:01+08:00`:null,count_received_at:v.ok?`2026-10-13T18:${v.minute}:01+08:00`:null,pair_ok:v.ok,scheduled_pause:false,queues:v.numbers===null?null:{mixedQueue:v.numbers,reservationQueue:[]},count_raw:v.ok?v.numbers?.length??0:null,comparison_state:v.comparison,interval_seconds:v.interval??null,error_codes:v.ok?{}:{detail:'read_failed'}}));
const row=id=>({store_id:id,observations:6,successful_pairs:5,failed_pairs:1,scheduled_pause_slots:0,expected_background_slots_so_far:6,observed_background_slots:5,observed_slot_fraction_so_far:5/6,last_observation_at:'2026-10-13T18:15:01+08:00'});
const month={source:'sapi_miniapp_gateway',api_profile:'miniapp_gateway',month:'2026-10',days:{'2026-10-13':{'3014':row('3014'),'3004':row('3004')}},store_names:{3014:'合成大融城',3004:'合成西单',2009:'合成世豪'},configured_store_ids:['3014','3004','2009'],unavailable_store_ids:[]};
let deferredMonth=null,resolveMonth=null,deferredStore=null,resolveStore=null;
const location={pathname:'/statistics-legacy',search:'?section=analysis&date=2026-10-13&store=3014'};
const context={console,Intl,Date:Clock,Math,Number,String,Object,Array,RegExp,JSON,Promise,Set,Map,URL,URLSearchParams,AbortSignal,setInterval:()=>0,clearInterval(){},document,window:{devicePixelRatio:1,location,matchMedia:()=>({matches:false}),addEventListener(name,fn){listeners[name]=fn;},history:{pushState(a,b,url){routes.push(url);location.search=url.slice(url.indexOf('?'));},replaceState(a,b,url){location.search=url.slice(url.indexOf('?'));}}}};
context.fetch=async path=>{reads.push(path);if(path.includes('/statistics/settings'))return {ok:true,json:async()=>({schema_version:1,poll_seconds:600})};
 if(path.includes('/months/')||path==='/api/v1/days'){if(deferredMonth){await deferredMonth;deferredMonth=null;}return {ok:true,json:async()=>month};}
 const match=path.match(/stores\/(\d+)\/days\/(\d{4}-\d{2}-\d{2})/);assert(match,'unexpected public path '+path);
 if(deferredStore&&match[1]==='3004'){await deferredStore;deferredStore=null;}
 return {ok:true,json:async()=>({source:'sapi_miniapp_gateway',requested_store_id:match[1],local_date:match[2],points:match[2]==='2026-10-13'?points:[],graph_truncated:false})};
};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const run=code=>vm.runInContext(code,context),settle=async()=>{await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));};
assert.equal(run('archiveReadStatus({days:{},store_names:{}})'),'归档加载中');
(async()=>{
 await settle();assert.equal($('store').value,'3014');assert.equal(run('engineeringSection'),'analysis');assert.equal(run('detail.points.length'),6);
 assert.equal($('snapshot-state').textContent,'已保存，字段未显示');assert.equal($('records-page-note').textContent,'6 条筛选记录，第 1 / 1 页');
 const actual=run('workspaceGraphSeries(workspaceRows())');assert.equal(actual.series.length,3);assert.equal(actual.speed.length,1);assert.equal(actual.speed[0].value,8*60/180);assert.equal(actual.series[2].connect,false,'failed/unknown continuation cannot be joined');
 $('range-dinner').onclick();assert.equal(run('workspaceState.from'),'17:30');const selected=run('workspaceRows()[1].key');run('workspaceSelectRecord(workspaceRows()[1].key)');assert.equal(run('workspaceState.selected'),selected);
 $('records-size').value='2';$('records-size').onchange();$('records-next').onclick();assert.equal(run('workspaceState.page'),1);
 await run('refresh()');assert.equal(run('workspaceState.range'),'dinner');assert.equal(run('workspaceState.selected'),selected);assert.equal(run('workspaceState.page'),1);
 $('range-from').value='20:00';$('range-to').value='18:00';$('range-apply').onclick();assert.match($('range-error').textContent,/结束时间不能早于/);assert.equal(run('workspaceState.range'),'dinner');
 $('ticket-number').value='4045';$('ticket-issued').value='18:01';$('experience-party').value='2';
 for(const section of ['quality','experience','overview','analysis']){$('nav-'+section).onclick();assert.equal(run('engineeringSection'),section);assert.equal($('ticket-number').value,'4045');assert.equal($('experience-party').value,'2');}
 // A delayed transition restores only the latest section's scroll position.
 const frames=[],scrolls=[];context.window.requestAnimationFrame=fn=>frames.push(fn);context.window.scrollTo=value=>scrolls.push(value.top);
 context.window.scrollY=380;$('nav-experience').onclick();context.window.scrollY=70;$('nav-analysis').onclick();
 frames.splice(0).forEach(fn=>fn());assert.deepEqual(scrolls,[380]);
 $('nav-analysis').onclick();assert.equal(frames.length,0,'same-section action must not restart a page transition');
 assert.equal($('ticket-number').value,'4045');assert.equal($('experience-party').value,'2');
 assert(!routes.some(v=>v.includes('4045')||v.includes('18%3A01')||v.includes('party')),'private input must never enter route');
 $('quality-filter').value='failed';$('quality-filter').onchange();assert.equal($('quality-rows').children.length,1);
 $('quality-filter').value='empty';$('quality-filter').onchange();assert.equal($('quality-rows').children.length,1);
 $('records-search').value='108';$('records-search').oninput();assert.equal($('points').children.length,1);assert.match(run('workspaceCSV()'),/108/);assert(!run('workspaceCSV()').includes('4045'));
 run('workspaceOpenRecord(workspaceRows()[1])');assert.equal($('record-dialog').open,true);$('record-dialog-close').onclick();assert.equal($('record-dialog').open,false);
 // A slow superseded detail cannot overwrite a newer store selection.
 $('records-search').value='';deferredStore=new Promise(r=>resolveStore=r);$('store').value='3004';$('store').onchange();await settle();assert.equal($('snapshot-store').textContent,'合成西单');assert.equal($('snapshot-numbers').children.length,0,'new selection cannot briefly show old store numbers');
 $('store').value='3014';$('store').onchange();await settle();resolveStore();await settle();assert.equal(run('detail.requested_store_id'),'3014');
 // A month refresh started before a user's new selection retains the latest choice.
 $('store').value='3004';deferredMonth=new Promise(r=>resolveMonth=r);const refresh=run('refresh()');await settle();$('store').value='3014';$('store').onchange();await settle();resolveMonth();await refresh;assert.equal($('store').value,'3014');
 // Back/forward restores public context without destroying mounted drafts.
 location.search='?section=quality&date=2026-10-13&store=3004&queue=reservationQueue';listeners.popstate();await settle();assert.equal(run('engineeringSection'),'quality');assert.equal($('store').value,'3004');assert.equal($('queue').value,'reservationQueue');assert.equal($('experience-party').value,'2');
 // City changes clear the superseded store, preserve private drafts and limit choices.
 $('city').value='成都';$('city').onchange();await settle();assert.equal($('store').value,'');assert.deepEqual($('store').children.map(v=>v.value),['','2009']);assert.equal($('ticket-number').value,'4045');assert.equal(run('detail'),null);
 $('city').value='all';$('city').onchange();await settle();assert.equal($('store').disabled,true);assert.equal($('store').children.length,1);
 run("workspaceSelectStore('3014')");await settle();assert.equal($('city').value,'北京');assert.equal($('store').value,'3014');assert(!$('store').children.some(v=>v.value==='2009'));
 assert(reads.every(p=>(p.startsWith('/api/v1/')||p==='/model-advice.json')&&!p.includes('4045')));
 console.log('workspace async selection, private drafts, chart boundaries, quality states, pagination, route restoration and CSV isolation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
