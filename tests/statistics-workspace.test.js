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
let at=Date.parse('2026-10-13T21:10:00+08:00');class Clock extends Date{constructor(...args){super(...(args.length?args:[at]));}static now(){return at;}}
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
let deferredMonth=null,resolveMonth=null,deferredStore=null,resolveStore=null,deferredDay=null,resolveDay=null;
const location={pathname:'/statistics-legacy',search:'?section=analysis&date=2026-10-13&store=3014'};
const context={console,Intl,Date:Clock,Math,Number,String,Object,Array,RegExp,JSON,Promise,Set,Map,URL,URLSearchParams,AbortSignal,setInterval:()=>0,clearInterval(){},document,window:{devicePixelRatio:1,location,matchMedia:()=>({matches:false}),addEventListener(name,fn){listeners[name]=fn;},history:{pushState(a,b,url){routes.push(url);location.search=url.slice(url.indexOf('?'));},replaceState(a,b,url){location.search=url.slice(url.indexOf('?'));}}}};
let health={schema_version:1,state:'running',active_store_count:2,unavailable_store_ids:[],display_date:'2026-10-13',display_switch_at:'2026-10-14T11:00:00+08:00'};
context.fetch=async path=>{reads.push(path);if(path==='/api/v1/collection/health')return {ok:true,json:async()=>health};if(path.includes('/statistics/settings'))return {ok:true,json:async()=>({schema_version:1,poll_seconds:600})};
 if(path.includes('/months/')||path==='/api/v1/days'){if(deferredMonth){await deferredMonth;deferredMonth=null;}return {ok:true,json:async()=>({...month,month:path.match(/months\/(\d{4}-\d{2})/)?.[1]||month.month})};}
 const match=path.match(/stores\/(\d+)\/days\/(\d{4}-\d{2}-\d{2})/);assert(match,'unexpected public path '+path);
 if(deferredStore&&match[1]==='3004'){await deferredStore;deferredStore=null;}
 if(deferredDay&&match[2]==='2026-10-14'){await deferredDay;deferredDay=null;}
 return {ok:true,json:async()=>({source:'sapi_miniapp_gateway',requested_store_id:match[1],local_date:match[2],points:match[2]==='2026-10-13'?points:[],graph_truncated:false})};
};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const run=code=>vm.runInContext(code,context),settle=async()=>{await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));};
assert.equal(run('archiveReadStatus({days:{},store_names:{}})'),'归档加载中');
(async()=>{
 await settle();assert.equal($('store').value,'3014');assert.equal(run('engineeringSection'),'analysis');assert.equal(run('detail.points.length'),6);
 assert.equal($('snapshot-state').textContent,'等待更新');assert.equal($('snapshot-numbers').children[0].textContent,'—');assert.equal($('snapshot-reservation').children[0].textContent,'—');assert.equal($('records-page-note').textContent,'6 条观测，第 1 / 1 页');
 const actual=run('workspaceGraphSeries(workspaceRows())');assert.equal(actual.series.length,3);assert.equal(actual.speed.length,1);assert.equal(actual.speed[0].value,8*60/180);assert.equal(actual.series[2].connect,false,'failed/unknown continuation cannot be joined');
 // A restored all-off form must not hide archived data or survive a date round trip.
 for(const i of [1,2,3])$('series-'+i).checked=false;
 run('graphs()');assert.equal($('series-1').checked,true);assert.equal(run('workspaceState.chartPositions.length'),3);
 // Switching to the second position is preserved; switching off the last one is rejected.
 $('series-2').checked=true;$('series-2').onchange();$('series-1').checked=false;$('series-1').onchange();
 assert.equal(run('workspaceState.chartPositions.length'),2);assert(run('workspaceState.chartPositions.every(v=>v.series===1)'));
 $('series-2').checked=false;$('series-2').onchange();assert.equal($('series-2').checked,true);
 run("workspaceSetDate('2026-10-14')");await settle();assert.equal(run('workspaceState.chartPositions.length'),0);assert.match($('chart-text-summary').textContent,/当天没有已保存的观测/);
 $('numbers').listeners.pointerleave();assert.equal(run('workspaceState.charts.get("numbers").emptyText'),'当天没有已保存的观测');
 run("workspaceSetDate('2026-10-13')");await settle();assert.equal($('series-1').checked,false);assert.equal($('series-2').checked,true);assert.equal(run('workspaceState.chartPositions.length'),2);
 deferredDay=new Promise(r=>resolveDay=r);run("workspaceSetDate('2026-10-14')");await settle();run("workspaceSetDate('2026-10-13')");await settle();resolveDay();await settle();assert.equal(run('detail.local_date'),'2026-10-13');assert.equal(run('workspaceState.chartPositions.length'),2,'a late empty date must not erase the restored curve');
 run("workspaceSetDate('2026-09-30')");await settle();assert.equal(run('workspaceState.chartPositions.length'),0);run("workspaceSetDate('2026-10-13')");await settle();assert.equal($('series-2').checked,true);assert.equal(run('workspaceState.chartPositions.length'),2,'cross-month return preserves the chosen position');
 // Position absence is distinct from a day with no archive, and hover retains that message.
 $('queue').value='reservationQueue';$('queue').onchange();assert.match($('chart-text-summary').textContent,/所选号码位置/);assert.equal(run('workspaceState.charts.get("numbers").emptyText'),'所选号码位置没有可绘制的展示号');
 $('queue').value='mixedQueue';$('queue').onchange();$('series-1').checked=true;$('series-1').onchange();$('series-2').checked=false;$('series-2').onchange();
 const selected=run('workspaceRows()[1].key');run('workspaceSelectRecord(workspaceRows()[1].key)');assert.equal(run('workspaceState.selected'),selected);
 $('records-size').value='2';$('records-size').onchange();$('records-next').onclick();assert.equal(run('workspaceState.page'),1);
 await run('refresh()');assert.equal(run('workspaceState.range'),'all');assert.equal(run('workspaceState.selected'),selected);assert.equal(run('workspaceState.page'),1);
 $('ticket-number').value='4045';$('ticket-issued').value='18:01';$('experience-party').value='2';
 for(const section of ['experience','overview','analysis']){run(`selectEngineeringSection('${section}')`);assert.equal(run('engineeringSection'),section);assert.equal($('ticket-number').value,'4045');assert.equal($('experience-party').value,'2');}
 // A delayed transition restores only the latest section's scroll position.
 const frames=[],scrolls=[];context.window.requestAnimationFrame=fn=>frames.push(fn);context.window.scrollTo=value=>scrolls.push(value.top);
 context.window.scrollY=380;$('nav-experience').onclick();context.window.scrollY=70;$('nav-analysis').onclick();
 frames.splice(0).forEach(fn=>fn());assert.deepEqual(scrolls,[380]);
 $('nav-analysis').onclick();assert.equal(frames.length,0,'same-section action must not restart a page transition');
 assert.equal($('ticket-number').value,'4045');assert.equal($('experience-party').value,'2');
 assert(!routes.some(v=>v.includes('4045')||v.includes('18%3A01')||v.includes('party')),'private input must never enter route');
 $('records-search').value='108';$('records-search').oninput();assert.equal($('points').children.length,1);assert.match(run('workspaceCSV()'),/108/);assert(!run('workspaceCSV()').includes('4045'));
 run('workspaceOpenRecord(workspaceRows()[1])');assert.equal($('record-dialog').open,true);$('record-dialog-close').onclick();assert.equal($('record-dialog').open,false);
 // A slow superseded detail cannot overwrite a newer store selection.
 $('records-search').value='';deferredStore=new Promise(r=>resolveStore=r);$('store').value='3004';$('store').onchange();await settle();assert.equal($('snapshot-store').textContent,'合成西单');assert.equal($('snapshot-numbers').children.length,0,'new selection cannot briefly show old store numbers');
 $('store').value='3014';$('store').onchange();await settle();resolveStore();await settle();assert.equal(run('detail.requested_store_id'),'3014');
 // A month refresh started before a user's new selection retains the latest choice.
 $('store').value='3004';deferredMonth=new Promise(r=>resolveMonth=r);const refresh=run('refresh()');await settle();$('store').value='3014';$('store').onchange();await settle();resolveMonth();await refresh;assert.equal($('store').value,'3014');
 // Back/forward restores public context without destroying mounted drafts.
 location.search='?section=quality&date=2026-10-13&store=3004&queue=reservationQueue';listeners.popstate();await settle();assert.equal(run('engineeringSection'),'analysis');assert.equal($('store').value,'3004');assert.equal($('queue').value,'reservationQueue');assert.equal($('experience-party').value,'2');
 // City changes clear the superseded store, preserve private drafts and limit choices.
 $('city').value='成都';$('city').onchange();await settle();assert.equal($('store').value,'');assert.deepEqual($('store').children.map(v=>v.value),['','2009']);assert.equal($('ticket-number').value,'4045');assert.equal(run('detail'),null);
 $('city').value='all';$('city').onchange();await settle();assert.equal($('store').disabled,true);assert.equal($('store').children.length,1);
 run("workspaceSelectStore('3014')");await settle();assert.equal($('city').value,'北京');assert.equal($('store').value,'3014');assert(!$('store').children.some(v=>v.value==='2009'));
 // New observations highlight once; browsing another page keeps the same first record.
 $('records-size').value='2';$('records-size').onchange();$('records-next').onclick();const anchor=$('points').children[0].dataset.observationKey;
 const nextPoint={...points[1],request_started_at:'2026-10-13T21:11:00+08:00',queue_received_at:'2026-10-13T21:11:01+08:00',count_received_at:'2026-10-13T21:11:01+08:00',queues:{mixedQueue:['190','191','192'],reservationQueue:['7001','7002']}};
 at=Date.parse('2026-10-13T21:11:02+08:00');points.push(nextPoint);await run('refresh()');assert.equal($('points').children[0].dataset.observationKey,anchor);
 $('records-previous').onclick();assert($('points').children[0].classList.contains('observation-new'));assert.equal($('snapshot-numbers').children[0].textContent,'190');assert.equal($('snapshot-reservation').children[0].textContent,'7001');
 const rowNode=$('points').children[0];run('workspaceRenderTable()');assert.equal($('points').children[0],rowNode,'unchanged records keep their DOM nodes');at+=3000;run('workspaceClock()');assert(!rowNode.classList.contains('observation-new'),'highlight expires even when reduced motion suppresses animationend');
 run('workspaceState.readError=true;workspaceSnapshot()');assert.equal($('snapshot-numbers').children[0].textContent,'—');assert.equal($('snapshot-reservation').children[0].textContent,'—');run('workspaceState.readError=false');
 // Hover from anywhere inside the plotting area snaps to a real point and shows guides.
 run('graphs()');const position=run('workspaceState.charts.get("count").positions[0]');$('count').listeners.pointermove({clientX:position.x,clientY:100});assert.equal($('chart-tooltip-count').hidden,false);assert.equal(run('workspaceState.hover.get("count").key'),position.key);assert.equal(run('workspaceAxisLabel("count")'),'已签到等待（桌）');assert.match($('chart-tooltip-count').textContent,/3 桌/);assert.equal($('count-chart-title').textContent,'已签到等待桌数');assert.equal($('count-table-title').textContent,'已签到等待桌数（桌）');assert.match(run('workspaceCSV()'),/已签到等待桌数（桌）/);$('count').listeners.pointerleave();assert.equal($('chart-tooltip-count').hidden,true);
 const countArchive=run('JSON.stringify(detail)');run('workspaceCountMetric()');assert.equal(run('JSON.stringify(detail)'),countArchive);
 run("detail.source='crm_remote_v1_1';graphs()");assert.match(run('workspaceAxisLabel("count")'),/单位待核实/);assert.match(run('workspaceCSV()'),/接口原始数量（单位未核实）/);run("detail.source='sapi_miniapp_gateway';graphs()");
 run('workspaceOpenRecord(workspaceRows()[4])');assert($('record-details').children.some(v=>v.textContent==='0 桌'),'zero is an observed count, not missing');$('record-dialog-close').onclick();
 // Visual archive guides never change analytical comparison or bridge failures/boundaries.
 const guideBefore={...points[0],observation_id:'a',record_sha256:'hash-a',continuity:{writer_id:'w'}},guideNow={...points[1],comparison_state:'gap',continuity:{writer_id:'w',predecessor_id:'a',predecessor_sha256:'hash-a'}};
 context.guideBefore=guideBefore;context.guideNow=guideNow;assert.equal(run('workspaceVisualLink(guideNow,guideBefore)'),true);
 for(const patch of [{pair_ok:false},{comparison_state:'run_boundary'},{continuity:{writer_id:'other',predecessor_id:'a',predecessor_sha256:'hash-a'}},{queue_received_at:'2026-10-13T18:20:00+08:00'}]){context.guideNow={...guideNow,...patch};assert.equal(run('workspaceVisualLink(guideNow,guideBefore)'),false);}
 // Automatic overview holds the previous session before opening, including after reload.
 location.search='?section=overview';run('workspaceSessionAuto=true');health={...health,state:'closed',active_store_count:0,display_date:'2026-10-13',display_switch_at:'2026-10-14T11:00:00+08:00'};at=Date.parse('2026-10-14T09:00:00+08:00');await run('refresh()');assert.equal(run('day'),'2026-10-13');assert($('rows').children.length>0);assert(!location.search.includes('date='));
 // At the scheduled boundary even a still-closed collector must show the new empty day.
 health={...health,display_date:'2026-10-14',display_switch_at:'2026-10-15T11:00:00+08:00'};at=Date.parse('2026-10-14T11:00:00+08:00');await run('refresh()');assert.equal(run('day'),'2026-10-14');assert.equal($('summary').textContent,'无已保存观测');assert.equal($('snapshot-numbers').children[0].textContent,'—');assert.equal($('snapshot-total').textContent,'0');
 run("workspaceSetDate('2026-10-13')");await settle();await run('refresh()');assert.equal(run('day'),'2026-10-13','manual history remains pinned');
 // Manual collection has an explicit owner login and sends one idempotent job,
 // independently of the selected city. It never turns archive refresh into a GET.
 context.crypto=require('node:crypto').webcrypto;context.setTimeout=()=>1;context.clearTimeout=()=>{};
 const archiveFetch=context.fetch,manualPosts=[];let manualAuthorized=false,manualJob=null;
 context.fetch=async(path,options)=>{
  if(path!=='/api/v1/collection/manual')return archiveFetch(path,options);
  assert.equal(options.method,'POST');assert.equal(options.credentials,'same-origin');const value=JSON.parse(options.body);manualPosts.push(value);
  if(value.action==='login'){manualAuthorized=true;return {ok:true,json:async()=>({authorized:true})};}
  if(!manualAuthorized)return {ok:false,json:async()=>({error_code:'manual_login_required'})};
  if(value.action==='request')manualJob={request_id:value.request_id,store_id:value.store_id,state:'running',total:2,pending:2,successful:0,failed:0,skipped:0,local_date:'2026-10-14'};
  return {ok:true,json:async()=>({authorized:true,job:manualJob,collection_state:'running'})};
 };
 health={...health,state:'running',manual_collection_enabled:true,manual_store_ids:['3014']};run('workspaceHealth={state:"running",manual_collection_enabled:true,manual_store_ids:["3014"]};workspaceRenderManual()');await settle();
 assert.equal($('manual-all').hidden,false);await $('manual-all').onclick();assert.equal($('manual-login-dialog').open,true);
 const requestId=manualPosts.find(v=>v.action==='request').request_id;
 $('manual-password').value='synthetic-test-only';await run('workspaceLoginManual({preventDefault(){}})');
 assert.equal($('manual-password').value,'');assert.equal($('manual-login-dialog').open,false);assert.equal($('manual-all').disabled,true);
 assert.deepEqual(manualPosts.filter(v=>v.action==='request').map(v=>[v.request_id,v.store_id]),[[requestId,null],[requestId,null]]);
 const sent=manualPosts.length;await $('manual-all').onclick();assert.equal(manualPosts.length,sent,'active job rejects duplicate button clicks');
 manualJob={...manualJob,state:'completed',pending:0,successful:2,targets:['3014','3004'].map(store=>({store_id:store,state:'saved',sample_at:'2026-10-14T11:01:00+08:00'}))};await run('workspacePollManual()');assert.match($('manual-progress-text').textContent,/已获取 2 家/);assert.equal(run('workspaceManual.syncing'),true,'archive lag cannot be displayed as fully synced');assert.equal($('manual-all').disabled,true);
 month.days['2026-10-14']=Object.fromEntries(['3014','3004'].map(store=>[store,{...row(store),local_date:'2026-10-14',last_observation_at:'2026-10-14T11:01:00+08:00'}]));await run('workspaceSyncManual(workspaceManual.job,1)');assert.equal(run('workspaceManual.syncing'),false);assert.equal($('manual-all').disabled,false);
 await $('manual-store').onclick();assert.equal(manualPosts.at(-1).store_id,'3014');
 run('workspaceHealth.state="closed";workspaceRenderManual()');assert.equal($('manual-all').disabled,true);assert.equal($('manual-store').disabled,true);
 // Proactive authorization works while closed and cannot submit a collection job.
 const beforeAuthorization=manualPosts.filter(v=>v.action==='request').length,authorizationRoutes=[];
 context.window.location.assign=path=>authorizationRoutes.push(path);manualAuthorized=false;
 const originalManualFetch=context.fetch;let unsafeAuthorization=false;
 context.fetch=async(path,options)=>{
  const value=path==='/api/v1/collection/manual'?JSON.parse(options.body):null;
  if(value?.action==='reauthorize'&&manualAuthorized){manualPosts.push(value);return {ok:true,json:async()=>({path:unsafeAuthorization?'https://other.invalid':'/recover/#'+'A'.repeat(43)})};}
  return originalManualFetch(path,options);
 };
 await $('wechat-authorize').onclick();assert.equal($('manual-login-dialog').open,true);
 assert.equal($('manual-login-title').textContent,'微信授权');assert.match($('manual-login-submit').textContent,/二维码/);
 $('manual-password').value='synthetic-test-only';await run('workspaceLoginManual({preventDefault(){}})');
 assert.deepEqual(authorizationRoutes,['/recover/#'+'A'.repeat(43)]);assert.equal($('manual-password').value,'');
 assert.equal(manualPosts.filter(v=>v.action==='request').length,beforeAuthorization,'authorization must never create a query job');
 unsafeAuthorization=true;await $('wechat-authorize').onclick();assert.equal(authorizationRoutes.length,1);
 assert.match($('wechat-authorize-status').textContent,/暂不可用/);assert.equal($('wechat-authorize').disabled,false);
 assert(reads.every(p=>(p.startsWith('/api/v1/')||p==='/model-advice.json')&&!p.includes('4045')));
 console.log('workspace async selection, private drafts, chart boundaries, quality states, pagination, route restoration and CSV isolation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
