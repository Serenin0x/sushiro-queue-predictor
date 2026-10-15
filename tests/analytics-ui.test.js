'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Node{constructor(){this.children=[];this.value='';this.disabled=false;this.textContent='';this.dataset={};}append(...v){this.children.push(...v);}replaceChildren(...v){this.children=v;}setAttribute(k,v){this[k]=v;}scrollIntoView(){}}
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);},events={},requests=[];
let section='intelligence';
const selection={from:'2026-10-01',to:'2026-10-16',source:'sapi_miniapp_gateway',queue:'ordinary',store_id:''};
const row={store_id:'3014',store_name:'合成门店',city:'北京',observations:20,successful:20,visible:10,covered_slots:10,expected_slots:44,sampling_gaps:0,coverage_percent:22.7,days_observed:1,comparable_intervals:8,readiness_score:20,readiness_level:'待积累',coverage_from:'2026-10-15',unread_dates:[],read_failures:0,hourly:Array.from({length:12},(_,n)=>({hour:n+10,rate:1,active_ratio:50,observations:2,days:1})),daily:[],gaps:[],gap_count:0,suggestions:[{priority:'high',text:'合成补采建议'}]};
const report={selection,stores:[row],report_id:'a'.repeat(64),generated_at:'2026-10-16T00:00:00Z',archive_start:'2026-10-15',archive_end:'2026-10-15',errors:[],incomplete:false};
const context={window:{addEventListener:(name,fn)=>events[name]=fn},document:{visibilityState:'visible',getElementById:$,createElement:()=>new Node(),createElementNS:()=>new Node(),addEventListener:(name,fn)=>events[name]=fn},setInterval:()=>0,Date,Intl,Blob,AbortSignal,navigator:{sendBeacon:()=>true},fetch:async(path,options)=>{const body=options?.body&&JSON.parse(options.body);requests.push({path,body});return {ok:true,json:async()=>body?.action==='report'?{...report,selection:body.selection}:body?{state:'queued'}:{report_id:report.report_id,state:'waiting_for_open'}};}};
vm.createContext(context);vm.runInContext(fs.readFileSync('src/sushiwait/web/analytics-ui.js','utf8'),context);
const api=context.window.SushiWaitAnalytics,settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 $('insight-source').value='sapi_miniapp_gateway';$('insight-queue').value='ordinary';
 api.attach({today:()=>selection.to,section:()=>section,navigateStore(){}});await settle();
 assert.equal($('insight-ai-run').disabled,false);
 await $('insight-ai-run').onclick();assert.equal($('insight-ai-run').disabled,true);
 const paidIntent=()=>requests.filter(r=>r.body?.action==='explain'&&r.body.visible);
 assert.equal(paidIntent().length,1);
 context.document.visibilityState='hidden';events.visibilitychange();await settle();
 assert(requests.some(r=>r.body?.action==='explain'&&r.body.visible===false));
 context.document.visibilityState='visible';events.visibilitychange();await settle();
 assert.equal($('insight-ai-run').disabled,false,'returning to a waiting report must allow a new explicit request');
 assert.equal(paidIntent().length,1,'returning without clicking must not renew the old demand');
 $('insight-store').value='3014';$('insight-store').onchange();await $('insight-ai-run').onclick();
 assert(requests.some(r=>r.body?.action==='report'&&r.body.selection.store_id==='3014'));
 assert.equal(paidIntent().at(-1).body.selection.store_id,'3014');
 section='tickets';api.notify();await settle();assert.equal($('insight-ai-run').disabled,false);
 console.log('analytics visible demand, release/re-entry and independent store scope passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
