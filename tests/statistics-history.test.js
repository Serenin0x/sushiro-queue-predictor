"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const stats=fs.readFileSync(process.argv[2],"utf8"),model=fs.readFileSync(process.argv[3],"utf8");
const base=Date.parse("2026-10-09T03:00:00Z"),iso=ms=>new Date(ms).toISOString();
class Element{constructor(tag){this.tag=tag;this.children=[];this.value="";this.hidden=false;this.textContent="";this.classList={add(){}};}append(e){this.children.push(e);}replaceChildren(){this.children=[];}setAttribute(){}getBoundingClientRect(){return {width:600};}getContext(){return new Proxy({},{get:()=>()=>{}});}}
function curve(day,count=91,store="900001"){
  const start=Date.parse(`${day}T03:00:00Z`),points=Array.from({length:count},(_,i)=>({request_started_at:iso(start+i*60000-100),queue_received_at:iso(start+i*60000),pair_ok:true,scheduled_pause:false,error_codes:{},comparison_state:i?"comparable_display_sets":"insufficient",queues:{mixedQueue:[String(100+2*i)],reservationQueue:[String(7000+2*i)]}}));
  return {daily_schema_version:1,source:"crm_remote_v1_1",requested_store_id:store,local_date:day,generated_at:points.at(-1).queue_received_at,points,graph_truncated:false,network_performed_by_read:false,eta_available:false,first_label_is_confirmed_call:false,call_reference_semantics:"user_assumed_first_displayed_label",summary:{date_type:"ordinary_workday"}};
}
function setup(historyDates=["2026-10-08","2026-10-07"]){
  const clock={value:base+3*60000},nodes=new Map(),reads=[];
  class Clock extends Date{constructor(...args){super(...(args.length?args:[clock.value]));}static now(){return clock.value;}}
  const index={days:{},store_names:{900001:"第一店",900002:"第二店"},unavailable_store_ids:[]};
  for(const day of [...historyDates,"2026-10-09"])index.days[day]={900001:{store_id:"900001",observations:91,successful_pairs:91,failed_pairs:0,scheduled_pause_slots:0,expected_background_slots_so_far:91,observed_background_slots:91,observed_slot_fraction_so_far:1}};
  const ctx={console,Intl,Date:Clock,Math,Number,String,Object,Array,RegExp,JSON,Promise,Set,Map,AbortSignal,setInterval(){},window:{devicePixelRatio:1,addEventListener(){}},document:{getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},createElement:tag=>new Element(tag)}};
  ctx.fetch=async path=>{reads.push(path);if(path==="/api/v1/days")return {ok:true,json:async()=>index};if(path.startsWith("/api/v1/months/"))return {ok:true,json:async()=>({days:{}})};const parts=path.split("/");return {ok:true,json:async()=>curve(parts.at(-1),parts.at(-1)==="2026-10-09"?4:91,parts[4])};};
  vm.createContext(ctx);vm.runInContext(model,ctx);ctx.window.SushiWaitReference=ctx.SushiWaitReference;vm.runInContext(stats,ctx);
  return {ctx,nodes,reads,clock,index,run:source=>vm.runInContext(source,ctx)};
}
async function settle(){await new Promise(resolve=>setImmediate(resolve));}
async function select(t){await settle();t.nodes.get("store").value="900001";t.nodes.get("queue").value="mixedQueue";await t.run("showDay()");await settle();t.nodes.get("ticket-number").value="136";t.nodes.get("ticket-issued").value="11:00";t.nodes.get("ticket-scope").checked=true;t.nodes.get("ticket-scope").onchange();}
(async()=>{
  const t=setup();await select(t);
  assert(t.nodes.get("prediction").textContent.includes("历史初估"));assert(t.nodes.get("prediction-note").textContent.includes("2 天"));
  const count=t.reads.length;await t.run("showDay()");await settle();assert.equal(t.reads.length,count+1); // Only today's curve refreshes.
  t.nodes.get("ticket-number").value="138";t.nodes.get("ticket-number").oninput();assert.equal(t.reads.length,count+1);
  assert(t.reads.every(p=>/^\/api\/v1\/(days|months\/2026-09|stores\/900001\/days\/2026-10-\d\d)$/.test(p)));
  assert(!t.reads.some(p=>p.includes("ticket")||p.includes("136")||p.includes("11:00")||p.includes("?")));
  t.clock.value+=90001;t.run("renderPrediction()");assert(t.nodes.get("prediction").textContent.includes("超过90秒"));assert(!t.nodes.get("prediction-note").textContent.includes("暂以连续实时趋势"));

  const censored=setup();const fetch=censored.ctx.fetch;censored.ctx.fetch=async path=>path.includes("/days/2026-10-0")&&!path.endsWith("2026-10-09")?{ok:true,json:async()=>curve(path.split("/").at(-1),61)}:fetch(path);
  await select(censored);assert(censored.nodes.get("prediction").textContent.includes("较晚端尚不确定"));assert(!censored.nodes.get("prediction").textContent.includes("08:00"));

  const failure=setup();const read=failure.ctx.fetch;let fail=true;failure.ctx.fetch=async path=>{if(fail&&path.endsWith("2026-10-07")){failure.reads.push(path);throw Error("history offline");}return read(path);};
  await select(failure);assert(failure.nodes.get("prediction-note").textContent.includes("部分历史读取失败"));
  fail=false;failure.nodes.get("refresh").onclick();await settle();await settle();assert(failure.nodes.get("prediction").textContent.includes("历史初估"));

  const race=setup();await settle();let resolveOld;const readRace=race.ctx.fetch;
  race.ctx.fetch=path=>path.endsWith("2026-10-08")?new Promise(resolve=>{resolveOld=resolve;}):readRace(path);
  race.nodes.get("store").value="900001";race.nodes.get("queue").value="mixedQueue";await race.run("showDay()");await settle();assert(resolveOld);
  race.nodes.get("store").value="900002";await race.run("showDay()");await settle();const before=race.nodes.get("prediction").textContent;
  resolveOld({ok:true,json:async()=>curve("2026-10-08")});await settle();assert.equal(race.nodes.get("prediction").textContent,before);assert.equal(race.run("detail.requested_store_id"),"900002");

  const selection=setup(Array.from({length:8},(_,i)=>`2026-10-${String(i+1).padStart(2,"0")}`)),readSelection=selection.ctx.fetch;
  selection.ctx.fetch=async path=>{if(path==="/api/v1/months/2026-09"){selection.reads.push(path);return {ok:true,json:async()=>({days:{"2026-09-25":{900001:{observations:91}},"2026-09-18":{900001:{observations:91}}}})};}return readSelection(path);};
  await select(selection);const archived=selection.reads.filter(p=>p.includes("/stores/")&&!p.endsWith("2026-10-09"));
  assert.equal(archived.length,7);assert(archived.some(p=>p.endsWith("2026-09-25")));assert(archived.some(p=>p.endsWith("2026-09-18")));
  assert.equal(selection.run("index.days['2026-09-25']"),undefined); // History reads never replace the visible month.
  console.log("history cold start, unknown tail, bounded cache, retry, private inputs, store race and repeated weekdays passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
