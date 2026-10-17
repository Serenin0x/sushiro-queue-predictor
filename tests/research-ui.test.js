'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
let now=Date.parse('2026-10-14T04:30:00Z');const requests=[],elements=new Map(),storage=new Map(),events={};
class Clock extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
class Node{constructor(){this.value='';this.checked=false;this.textContent='';this.children=[];this.hidden=false;this.disabled=false;this.attrs={};this.classList={add(){},remove(){},toggle(){}};}append(...v){this.children.push(...v);}replaceChildren(...v){this.children=[...v];}setAttribute(k,v){this.attrs[k]=v;}focus(){}}
const $=name=>{if(!elements.has(name))elements.set(name,new Node());return elements.get(name);};
const state={section:'prediction',source:'sapi_miniapp_gateway',day:'2026-10-14',storeId:'3014',storeName:'大融城',storeNames:{'3014':'大融城'},queueType:'ordinary',mode:'auto',number:'4045',issued:'11:01',checkedIn:'no',confirmed:true,detail:null,samplingSeconds:180};
let confirmDelete=true,failDelete=false,listGate=null,serverRecords=[],serverDeleted=[];
let publicModel=null,archiveIndex={days:{}},forecastInputs=[];
let waitStores={},forecastCalls=0,forecast={available:false,reason:'insufficient_sparse_trend'};
const context={window:{confirm:()=>confirmDelete,addEventListener:(name,fn)=>events[name]=fn,SushiWaitReference:{ticketTiming:require('../src/sushiwait/web/reference-model.js').ticketTiming,arrival:()=>({available:false,state:'waiting'}),trackedForecast:(detail,options,history,advice)=>{forecastCalls++;forecastInputs.push({detail,options,history,advice});return forecast;},storeWait:()=>({available:false})}},location:{protocol:'https:',hostname:'124.222.57.236'},document:{visibilityState:'visible',getElementById:$,createElement:()=>new Node(),addEventListener:(name,fn)=>events[name]=fn},crypto:require('node:crypto').webcrypto,Uint8Array,Date:Clock,Intl,Blob,AbortSignal,navigator:{sendBeacon:()=>true},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},setTimeout:()=>0,clearTimeout:()=>{},setInterval:()=>0,
 fetch:async(path,options)=>{const body=options?.body?JSON.parse(options.body):null;requests.push({path,body});if(body?.action==='list'&&listGate)await listGate;if(body?.action==='delete'&&failDelete)return {ok:false};return {ok:true,json:async()=>path==='/model-advice.json'&&publicModel?publicModel:path==='/wait-reference.json'?{stores:waitStores}:body?.action==='list'?{records:serverRecords,deleted_record_ids:serverDeleted}:body?.action==='forecast'?{found:true,accepted:true,baseline:body.forecast}:['delete','restore'].includes(body?.action)?{found:true,deleted:body.action==='delete'}:body?{saved:true}:{local_date:state.day,points:[]}};}};
vm.createContext(context);vm.runInContext(fs.readFileSync('src/sushiwait/web/research-ui.js','utf8'),context);
const api=context.window.SushiWaitResearch,settle=async()=>new Promise(r=>setImmediate(r));
(async()=>{
 $('ticket-number').value='4045';$('ticket-issued').value='11:01';$('ticket-record-system').checked=true;
 api.attach({getState:()=>state,today:()=>state.day,navigate:section=>state.section=section,history:()=>[],index:()=>archiveIndex});await settle();
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
 // The new composer owns its store and uses the model chosen at explicit submit.
 context.location.protocol='https:';
 const newFields={store_id:'3004',store_name:'合成独立门店',number:'6200',issued_at:'2026-10-14T03:30:00Z',queue_type:'ordinary',checked_in:false};
 const result=await api.submitEntry(newFields,{kind:'prediction',mode:'thinking',record:false,follow:true,inputMethod:'manual'});
 let stored=JSON.parse(storage.get('sushiwait.submitted-tickets.v1')).items.find(i=>i.id===result.id);
 assert.equal(stored.ticket.store_id,'3004');assert.equal(state.storeId,'3014');assert.equal(stored.mode,'thinking');assert.equal(stored.follow,true);assert.equal(stored.ticket.input_method,'manual');
 const again=await api.submitEntry(newFields,{kind:'prediction',mode:'nonthinking',record:true,follow:false,inputMethod:'manual'});
 assert.equal(again.id,result.id);stored=JSON.parse(storage.get('sushiwait.submitted-tickets.v1')).items.find(i=>i.id===again.id);
 assert.equal(stored.mode,'nonthinking');assert.equal(stored.follow,false);assert.equal(stored.saved,true);
 assert(requests.some(r=>r.body?.ticket?.number==='6200'&&r.body.ticket.store_id==='3004'));
 const beforeInsecure=JSON.parse(storage.get('sushiwait.submitted-tickets.v1')).items.length;
 context.location.protocol='http:';
 await assert.rejects(()=>api.submitEntry({...newFields,number:'6201'},{kind:'record',mode:'algorithm',record:true}),/https_required/);
 assert.equal(JSON.parse(storage.get('sushiwait.submitted-tickets.v1')).items.length,beforeInsecure);
 // Collapsed no-ticket reference must not keep a model lease alive.
 context.location.protocol='https:';state.section='prediction';state.mode='thinking';
 const ids=new Set(JSON.parse(storage.get('sushiwait.submitted-tickets.v1')).items.map(i=>i.id));
 const waitRequests=()=>requests.filter(r=>r.path==='/api/v1/prediction/presence'&&!ids.has(r.body?.client_id));
 waitStores={'3014':{ordinary:{sample_count:5}}};$('store-wait-card').hidden=true;now+=60000;api.notify();await settle();assert.equal(waitRequests().length,0);
 $('store-wait-card').hidden=false;api.notify();await settle();api.notify();await settle();assert(waitRequests().some(r=>r.body.visible===true));
 $('store-wait-card').hidden=true;api.notify();await settle();assert.equal(waitRequests().at(-1).body.visible,false);
 assert(requests.every(r=>!r.path.includes('4045')&&!r.path.includes('1234')&&!r.path.includes(submitted.body.owner_secret)));
 // Database filters use issuance date and the row's own store, not global selection.
 state.section='tickets';
 $('tickets-store').value='3004';$('tickets-store').onchange();
 assert(api.filterRecords().every(i=>i.ticket.store_id==='3004'));
 $('tickets-from').value='2026-10-13';$('tickets-from').onchange();
 $('tickets-to').value='2026-10-13';$('tickets-to').onchange();
 assert.equal(api.filterRecords().length,0);
 $('tickets-period').value='all';$('tickets-period').onchange();
 assert(api.filterRecords().length>0);
 $('tickets-store').value='';$('tickets-store').onchange();
 $('tickets-kind').value='record';$('tickets-kind').onchange();
 assert(api.filterRecords().every(i=>i.kind==='record'));
 $('tickets-kind').value='all';$('tickets-kind').onchange();
 $('tickets-search').value='6200';$('tickets-search').oninput();
 assert.equal(api.filterRecords().length,1);
 $('tickets-search').value='';$('tickets-search').oninput();
 assert.equal($('tickets-prev').disabled,true);
 // A multi-page list renders only its page; filters reset pagination.
 for(let n=0;n<22;n++)await api.submitEntry({...newFields,number:String(6500+n)},{kind:'record',mode:'algorithm',record:false,inputMethod:'manual'});
 assert.equal($('my-tickets').children[0].children[1].children.length,10);
 assert.equal($('tickets-next').disabled,false);
 $('tickets-next').onclick();assert.equal($('tickets-prev').disabled,false);
 assert($('tickets-page-note').textContent.startsWith('第 2 /'));
 $('tickets-size').value='50';$('tickets-size').onchange();
 assert.equal($('tickets-prev').disabled,true);assert.equal($('tickets-next').disabled,true);
 assert.equal($('my-tickets').children[0].children[1].children.length,api.filterRecords().length);
 // Private sample planning excludes ambiguous, unsaved and other-queue rows.
 const sample=api.filterRecords().find(i=>i.saved&&i.ticket.store_id==='3004');
 sample.serverResult={result_available:true,state:'inferred_arrived',arrival_interval:{basis:'adjacent_samples',lower_at:'2026-10-14T04:00:00Z',upper_at:'2026-10-14T04:10:00Z'},wait_minutes:{lower:30,upper:40},quality_flags:[]};
 const summary=api.recordSummary('2026-10-01','2026-10-31','ordinary');
 assert(summary['3004'].completed>=1);assert(summary['3004'].inferred>=1);
 assert.equal(Object.keys(api.recordSummary('2026-10-01','2026-10-31','reservation')).length,0);
 sample.serverResult.quality_flags=['number_reversal'];
 assert.equal(api.recordSummary('2026-10-01','2026-10-31','ordinary')['3004'].completed,summary['3004'].completed-1);
 assert(!JSON.stringify(summary).includes('issued_at'));

 // Cancel, server failure, delete, stale list and undo exercise the real controls.
 const buttons=()=>{const out=[];function walk(n){if(n.attrs?.['aria-label']?.startsWith('删除 '))out.push(n);for(const c of n.children||[])walk(c);}walk($('my-tickets'));return out;};
 const target=api.filterRecords().find(i=>i.saved&&i.ticket.store_id==='3004');
 const deleteControl=()=>buttons().find(b=>b.attrs['aria-label']==='删除 '+target.ticket.number+' 号记录');
 const deletes=()=>requests.filter(r=>r.body?.action==='delete');
 confirmDelete=false;const count=deletes().length;await deleteControl().onclick();assert.equal(deletes().length,count);assert(api.filterRecords().some(i=>i.id===target.id));
 confirmDelete=true;failDelete=true;await deleteControl().onclick();assert(api.filterRecords().some(i=>i.id===target.id));assert.match($('my-ticket-status').textContent,/删除失败/);
 failDelete=false;
 let finishList;listGate=new Promise(r=>finishList=r);serverRecords=[{record_id:target.id,reported:target.ticket,result:null,received_at:target.createdAt}];
 const pendingList=api.refreshRecords();await settle();
 await deleteControl().onclick();
 assert(!api.filterRecords().some(i=>i.id===target.id));assert(!JSON.parse(storage.get('sushiwait.submitted-tickets.v1')).items.some(i=>i.id===target.id));
 assert(JSON.parse(storage.get('sushiwait.submitted-tickets.v1')).deleted_ids.includes(target.id));
 finishList();listGate=null;await pendingList;assert(!api.filterRecords().some(i=>i.id===target.id),'stale list must not resurrect a deleted record');
 assert.equal($('ticket-delete-undo').hidden,false);await $('ticket-delete-undo').onclick();assert(api.filterRecords().some(i=>i.id===target.id));assert(requests.some(r=>r.body?.action==='restore'));
 // A deletion made in another tab is synchronized from the owner-only list.
 serverRecords=[];serverDeleted=[target.id];await api.refreshRecords();assert(!api.filterRecords().some(i=>i.id===target.id));
 // Browser-only records use the same UI without sending private delete requests.
 await api.submitEntry({...newFields,number:'6800'},{kind:'prediction',mode:'algorithm',record:false,follow:false});
 const local=api.filterRecords().find(i=>!i.record);const beforeLocal=deletes().length;
 await buttons().find(b=>b.attrs['aria-label']==='删除 '+local.ticket.number+' 号记录').onclick();
 assert.equal(deletes().length,beforeLocal);assert(!api.filterRecords().some(i=>i.id===local.id));
 assert(requests.every(r=>!r.path.includes(submitted.body.owner_secret)));
 // Expanded stores receive the selected model, then incorporate its asynchronous result.
 publicModel={status:'ready',supported_store_ids:['3014','3004','3006'],stores:{'3006':{ordinary:{thinking:{state:'computing',advice:null},nonthinking:{state:'ready',advice:{thinking_mode:'nonthinking'}}}}}};
 state.section='prediction';
 const expanded=await api.submitEntry({...newFields,store_id:'3006',number:'6900'},{kind:'prediction',mode:'thinking',record:false,follow:true});
 assert(requests.some(r=>r.path==='/api/v1/prediction/presence'&&r.body.store_id==='3006'&&r.body.mode==='thinking'&&r.body.visible));
 assert.equal(forecastInputs.at(-1).advice,null,'thinking never silently selects nonthinking');
 const firstReads=requests.filter(r=>r.path.endsWith('/3006/days/2026-10-13')).length;
 archiveIndex={days:{'2026-10-13':{'3006':{observations:10}}}};
 publicModel.stores['3006'].ordinary.thinking={state:'ready',advice:{thinking_mode:'thinking'}};
 state.section='tickets';now+=31000;api.notify();await settle();await settle();
 assert.equal(forecastInputs.at(-1).advice.thinking_mode,'thinking');
 assert(requests.filter(r=>r.path.endsWith('/3006/days/2026-10-13')).length>firstReads,'initial empty index must not permanently freeze history');
 assert.equal(api.filterRecords().find(i=>i.id===expanded.id).modelState,'ready');
 // The human-facing reason must explain a bounded horizon rather than generic missing data.
 forecast={available:false,reason:'beyond_supported_horizon'};
 now+=31000;api.notify();await settle();await settle();
 const expandedItem=api.filterRecords().find(i=>i.id===expanded.id);
 api.openTickets();
 const texts=[];function collect(n){if(n.textContent)texts.push(n.textContent);for(const c of n.children||[])collect(c);}collect($('my-tickets'));
 assert(texts.some(t=>t.includes('超过 6 小时')));
 // Save first numeric forecasts before outcomes, per actual mode; never replace them on refresh.
 const iso=n=>new Date(n).toISOString();
 forecast={policy:'display_arrival_history_live_v6',available:true,ai_participated:false,sample_at:iso(now-60000),reference_time:iso(now+1800000),scenario_earliest:iso(now+1200000),scenario_latest:iso(now+2400000),sample_count:6,continuous_minutes:30,history:{selected_days:0}};
 const ongoing=await api.submitEntry({...newFields,store_id:'3014',number:'7100'},{kind:'prediction',mode:'thinking',record:true,follow:true});
 let tracked=api.filterRecords().find(i=>i.id===ongoing.id),first=tracked.firstForecasts.algorithm.reference_time;
 assert(tracked.forecastAcks.algorithm);assert(requests.some(r=>r.body?.action==='forecast'&&r.body.record_id===ongoing.id));
 forecast={...forecast,ai_participated:true,ai_mode:'thinking',reference_time:iso(now+1500000)};
 now+=31000;api.notify();await settle();await settle();
 tracked=api.filterRecords().find(i=>i.id===ongoing.id);assert(tracked.forecastAcks.thinking);assert.equal(tracked.firstForecasts.algorithm.reference_time,first);
 const thinking=tracked.firstForecasts.thinking.reference_time;
 forecast={...forecast,reference_time:iso(now+1200000)};now+=31000;api.notify();await settle();await settle();
 assert.equal(tracked.firstForecasts.thinking.reference_time,thinking,'later predictions cannot shrink the measured first-prediction error');
 const detected=iso(Date.parse(thinking)-900000);
 serverRecords=[{record_id:tracked.id,reported:{...tracked.ticket,record_kind:'prediction',prediction_mode:'thinking',first_forecasts:tracked.firstForecasts},result:{result_available:true,state:'arrived',detected_at:detected,display_numbers:['7100','7101','7102'],quality_flags:[]},received_at:tracked.createdAt}];
 now=Date.parse(detected)+60000;await api.refreshRecords();
 assert.equal(tracked.follow,false,'a server-detected arrival ends local automatic tracking');
 const allText=()=>{const text=[];function walk(n){if(n.textContent)text.push(n.textContent);for(const c of n.children||[])walk(c);}walk($('my-tickets'));return text;};
 assert(allText().some(t=>t.includes('首次捕获')));assert(allText().some(t=>t.includes('等待 约')));assert(allText().some(t=>t==='滞后 15 分钟'));
 // A saved post-gap crossing is not displayed as a known call time or an error.
 serverRecords[0].result={result_available:true,state:'inferred_arrived',detected_at:detected,arrival_interval:{basis:'issue_to_detection',lower_at:tracked.ticket.issued_at,upper_at:detected},wait_minutes:{lower:0,upper:90}};
 await api.refreshRecords();assert(allText().includes('无叫号时间'));assert(allText().includes('无法计算'));
 // Recording-only rows retain timing and never acquire prediction errors.
 const recording=api.filterRecords().find(i=>i.kind==='record');recording.serverResult={result_available:true,state:'arrived',detected_at:iso(now),display_numbers:[recording.ticket.number]};
 api.openTickets();
 const row=$('my-tickets').children[0].children[1].children.find(r=>r.children[0].children[0].textContent===recording.ticket.number);
 assert(row,'recording row is visible');assert.equal(row.children[5].textContent,'—');
 // A late public sync must be read again soon, not delayed a whole extra source period.
 forecast={policy:'display_arrival_history_live_v6',available:true,ai_participated:true,ai_mode:'thinking',sample_at:iso(now-170000),reference_time:iso(now+600000),scenario_earliest:iso(now+300000),scenario_latest:iso(now+900000),sample_count:6,continuous_minutes:30,history:{selected_days:0}};
 const recovered=await api.submitEntry({...newFields,store_id:'3014',number:'7200'},{kind:'prediction',mode:'thinking',record:false,follow:true});
 assert.equal(api.filterRecords().find(i=>i.id===recovered.id).nextAt,now+30000);
 console.log('explicit submission, unavailable forecast recording, manual/continuous modes, tab switching, hidden leases and HTTPS privacy passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
