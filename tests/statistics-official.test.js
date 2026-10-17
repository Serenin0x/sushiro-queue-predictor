"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const scripts=process.argv.slice(2).map(path=>fs.readFileSync(path,"utf8"));
const base=Date.parse("2026-10-11T10:35:00Z"),iso=ms=>new Date(ms).toISOString();
const source="sapi_miniapp_gateway",single="one_official_detail_response_not_two_query_pair";
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.value="";this.hidden=false;this.textContent="";this.dataset={};this.classList={add(){}};}
  append(value){this.children.push(value);}replaceChildren(){this.children=[];}setAttribute(){}
  getBoundingClientRect(){return {width:600};}getContext(){return new Proxy({},{get:()=>()=>{}});}
}
function curve(date="2026-10-11",labels=["100","101","102"]) {
  const at=Date.parse(`${date}T10:35:00Z`);
  return {daily_schema_version:1,source,api_profile:"miniapp_gateway",
    summary:{date_type:"ordinary_weekend",success_semantics:single},requested_store_id:"900001",
    local_date:date,generated_at:iso(at),network_performed_by_read:false,eta_available:false,
    first_label_is_confirmed_call:false,call_reference_semantics:"user_assumed_first_displayed_label",graph_truncated:false,
    points:[-1800000,-900000,0].map((offset,i)=>({request_started_at:iso(at+offset-200),
      queue_received_at:iso(at+offset),count_received_at:null,count_raw:null,pair_ok:true,scheduled_pause:false,
      success_semantics:single,error_codes:{},comparison_state:"packet_continuity_unknown",queue_field_presence:{mixedQueue:"present",reservationQueue:"present"},
      queues:{mixedQueue:i===2?labels:[String(90+i*4)],reservationQueue:[]}}))};
}
function setup(binding=source) {
  const nodes=new Map(),reads=[],clock={at:base};
  class Clock extends Date {constructor(...args){super(...(args.length?args:[clock.at]));}static now(){return clock.at;}}
  nodes.set("sushiwait-reference",new Element("main"));nodes.get("sushiwait-reference").dataset.statisticsSource=binding;
  const summary={store_id:"900001",observations:3,successful_pairs:3,failed_pairs:0,scheduled_pause_slots:0,
    expected_background_slots_so_far:30,observed_background_slots:3,observed_slot_fraction_so_far:.1,last_observation_at:iso(base)};
  const index={source,api_profile:"miniapp_gateway",month:"2026-10",days:{},store_names:{900001:"合成门店"},unavailable_store_ids:[]};
  for(const date of ["2026-10-11","2026-10-10","2026-10-09"])index.days[date]={900001:summary};
  const ctx={console,Intl,Date:Clock,Math,Number,String,Object,Array,RegExp,JSON,Promise,Set,Map,AbortSignal,setInterval(){},
    window:{devicePixelRatio:1,addEventListener(){}},document:{getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},createElement:tag=>new Element(tag)}};
  ctx.fetch=async path=>{reads.push(path);if(path==="/model-advice.json")return {ok:true,json:async()=>({schema_version:1,policy:"public_display_scenarios_v1",stores:{}})};if(path.includes("/months/"))return {ok:true,json:async()=>path.endsWith("2026-10")?index:{days:{}}};
    return {ok:true,json:async()=>curve(path.split("/").at(-1))};};
  vm.createContext(ctx);vm.runInContext(scripts[1],ctx);vm.runInContext(scripts[2],ctx);
  ctx.window.SushiWaitReference=ctx.SushiWaitReference;ctx.window.SushiWaitReferenceAlerts=ctx.SushiWaitReferenceAlerts;
  vm.runInContext(scripts[0],ctx);
  return {ctx,nodes,reads,clock,run:code=>vm.runInContext(code,ctx)};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function select(t) {
  await settle();t.nodes.get("store").value="900001";t.nodes.get("queue").value="mixedQueue";
  await t.run("showDay()");await settle();t.nodes.get("ticket-number").value="200";
  t.nodes.get("ticket-issued").value="18:00";t.nodes.get("ticket-offset").value="0";
  t.nodes.get("ticket-scope").checked=true;t.nodes.get("ticket-scope").onchange();
}
(async()=>{
  const t=setup();await select(t);
  assert.equal(t.reads[0],"/api/v1/official/months/2026-10");
  assert(t.reads.includes("/api/v1/official/stores/900001/days/2026-10-11"));
  assert(t.reads.includes("/api/v1/official/stores/900001/days/2026-10-10"));
  assert(t.reads.includes("/api/v1/official/months/2026-09"));
  assert.equal(t.run("latestReferenceResult.available"),false);
  assert.equal(t.run("latestReferenceResult.reason"),"insufficient_sparse_trend"); // Disconnected zero-exposure origins are not usable history.
  const count=t.reads.length;t.nodes.get("ticket-number").value="201";t.nodes.get("ticket-number").oninput();
  assert.equal(t.reads.length,count);
  t.ctx.visible=curve("2026-10-11",["100","201","202"]);
  t.ctx.visible.generated_at=iso(base+60000);
  t.ctx.visible.points.at(-1).request_started_at=iso(base+59800);
  t.ctx.visible.points.at(-1).queue_received_at=iso(base+60000);
  t.clock.at=base+60000;t.run("detail=visible;renderPrediction()");
  assert(t.nodes.get("ticket-precaution").textContent.includes("进入前两位"));
  assert.equal(t.nodes.get("ticket-tracking-state").textContent,"已到号");
  // Arrival is saved historical evidence. Staleness suspends an unarrived
  // ticket's forecast, rather than erasing a previously observed arrival.
  t.clock.at=base+720001;t.run("renderPrediction()");
  assert.equal(t.nodes.get("ticket-tracking-state").textContent,"已到号");
  t.nodes.get("ticket-number").value="300";t.nodes.get("ticket-number").oninput();
  assert.equal(t.run("latestReferenceResult.reason"),"stale_sample");
  assert(t.nodes.get("ticket-precaution").hidden);
  assert(t.reads.every(path=>path==="/model-advice.json"||/^\/api\/v1\/official\/(?:months\/2026-\d\d|stores\/900001\/days\/2026-10-\d\d)$/.test(path)));
  assert(!t.reads.some(path=>path.includes("ticket")||path.includes("18:00")||path.includes("?")||path.includes("201")));

  const failed=setup();await settle();failed.ctx.fetch=async path=>{failed.reads.push(path);return {ok:false,status:404};};
  await failed.run("refresh()");assert(failed.nodes.get("status").textContent.includes("暂时不可用"));
  assert(failed.reads.every(path=>path.startsWith("/api/v1/official/")||path==="/model-advice.json")); // No fallback to the old provider.
  const invalid=setup("other");await settle();assert.equal(invalid.reads.length,0);
  assert(invalid.nodes.get("status").textContent.includes("暂时不可用"));
  const wrongMonth=setup();await settle();wrongMonth.ctx.fetch=async path=>({ok:true,json:async()=>({source:"crm_remote_v1_1"})});
  await wrongMonth.run("refresh()");assert(wrongMonth.nodes.get("status").textContent.includes("暂时不可用"));
  const wrongDay=setup();await select(wrongDay);
  wrongDay.ctx.fetch=async path=>({ok:true,json:async()=>({...curve(),source:"crm_remote_v1_1"})});
  await wrongDay.run("showDay().catch(showError)");assert.equal(wrongDay.run("detail"),null);
  assert(!wrongDay.nodes.get("prediction").textContent.includes("参考位置预计"));
  const mixed=setup();await select(mixed);mixed.ctx.wrong={...curve(),source:"crm_remote_v1_1"};
  mixed.run("detail=wrong;renderPrediction()");assert(mixed.nodes.get("prediction").textContent.includes("语义不匹配"));
  assert(mixed.nodes.get("ticket-precaution").hidden);
  console.log("official routes, private inputs, sparse boundaries, arrival evidence, cadence freshness and no source fallback passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
