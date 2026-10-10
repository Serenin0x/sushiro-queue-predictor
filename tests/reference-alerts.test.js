"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const {createTracker}=require(require("node:path").resolve(process.argv[2]));
const base=Date.parse("2026-10-10T03:30:00Z"),iso=ms=>new Date(ms).toISOString();
function sample(labels=["100","101","102"],at=base,overrides={}) {
  return {request_started_at:iso(at-200),queue_received_at:iso(at),scheduled_pause:false,
    error_codes:{},comparison_state:"comparable_display_sets",queues:{mixedQueue:labels,reservationQueue:["7000"]},...overrides};
}
function detail(row=sample()) {return {daily_schema_version:1,source:"crm_remote_v1_1",requested_store_id:"900001",
  local_date:"2026-10-10",network_performed_by_read:false,eta_available:false,first_label_is_confirmed_call:false,
  call_reference_semantics:"user_assumed_first_displayed_label",generated_at:row.queue_received_at,points:[row]};}
function opts(overrides={}) {return {storeId:"900001",queueType:"ordinary",ticket:"200",issuedAt:iso(base-1800000),
  now:base,confirmedScope:true,desiredArrivalAt:null,callOffsetMinutes:0,...overrides};}
function estimate(at=base,reference=base+1800000,overrides={}) {return {available:true,eta_available:false,
  actual_call_verified:false,calibrated_interval:false,queue_field:"mixedQueue",sample_at:iso(at),
  reference_time:iso(reference),scenario_earliest:iso(reference-120000),scenario_latest:iso(reference+120000),
  acceleration_signal:false,...overrides};}
let cases=0;
function check(name,fn) {fn();cases++;}
check("membership and absence are precautions, never a missed-call assertion",()=>{
  const t=createTracker();let r=t.update(detail(sample(["100","200","201"])),opts());
  assert.equal(r.reason,"number_displayed");assert.equal(r.requested_source_interval_seconds,30);
  r=t.update(detail(sample(["201","202"],base+60000)),opts({now:base+60000}));
  assert.equal(r.reason,"previously_displayed_now_absent");assert.equal(r.first_seen_at,iso(base));
  for(const key of ["notification_sent","background_delivery_available","actual_call_verified","no_show_verified",
    "eta_available","calibrated_risk","network_performed","private_input_uploaded","source_interval_applied"])assert.equal(r[key],false);
  assert(Object.isFrozen(r));
});
check("exact labels, independent queue receipt and valid empty arrays",()=>{
  const t=createTracker();assert.equal(t.update(detail(sample(["0200"])),opts()).reason,"model_unavailable");
  assert.equal(t.update(detail(sample(["200"],base+60000,{error_codes:{storecount:"request_failed"}})),opts({now:base+60000})).reason,"number_displayed");
  assert.equal(createTracker().update(detail(sample([])),opts()).state,"tracking");
});
check("stale, failed and future receipts retract current precautions",()=>{
  const t=createTracker();t.update(detail(sample(["200"])),opts());
  assert.equal(t.update(detail(sample(["200"])),opts({now:base+90001})).reason,"source_stale");
  assert.equal(t.update(detail(sample(["200"],base+60000,{error_codes:{groupqueues:"request_failed"}})),opts({now:base+60000})).reason,"queue_unavailable");
  assert.equal(t.update(detail(sample(["200"],base+120000)),opts({now:base+60000})).reason,"current_time_invalid");
  assert.equal(t.update(null,opts({now:base+60000})).state,"data_unavailable");
});
check("scope edits and fresh confirmation isolate previous membership",()=>{
  const t=createTracker();t.update(detail(sample(["200"])),opts());
  assert.equal(t.update(detail(sample(["201"],base+60000)),opts({now:base+60000,ticket:"202"})).reason,"model_unavailable");
  assert.equal(t.update(detail(),opts({confirmedScope:false})).state,"idle");
  assert.equal(t.update(detail(),opts({confirmedScope:"yes"})).state,"idle");
  assert.equal(t.update(detail(),opts({queueType:"reservation"})).reason,"model_unavailable");
});
check("plan sign and early arrival are separate from prediction error",()=>{
  const d=detail(),arrival=iso(base+1200000);
  const positive=createTracker().update(d,opts({desiredArrivalAt:arrival,callOffsetMinutes:10}),estimate());
  assert.equal(positive.target_call_at,iso(base+1800000));
  const negative=createTracker().update(d,opts({desiredArrivalAt:arrival,callOffsetMinutes:-10}),estimate());
  assert.equal(negative.target_call_at,iso(base+600000));assert.equal(negative.requested_source_interval_seconds,30);
  const early=createTracker().update(d,opts({desiredArrivalAt:iso(base+3600000)}),estimate());
  assert.equal(early.reason,"possible_before_arrival");assert.equal(early.requested_source_interval_seconds,30);
  assert.equal(createTracker().update(d,opts({desiredArrivalAt:iso(base+900000)}),estimate()).requested_source_interval_seconds,30);
  assert.equal(createTracker().update(d,opts({desiredArrivalAt:iso(base+900001)}),estimate()).requested_source_interval_seconds,60);
});
check("unknown issue, invalid offset, old day and out-of-range clocks cannot throw",()=>{
  for(const change of [{issuedAt:null},{issuedAt:iso(base+1)},{callOffsetMinutes:10},{callOffsetMinutes:1.5},
    {desiredArrivalAt:"invalid"},{desiredArrivalAt:iso(base-3600000)},{now:1e20},{now:NaN},{queueType:"storeQueue"}])
    assert.equal(createTracker().update(detail(),opts(change)).state,"data_unavailable");
  assert.equal(createTracker().update({...detail(),local_date:"2026-10-09"},opts()).state,"data_unavailable");
});
check("regressed or conflicting receipts cannot mutate prior evidence",()=>{
  const t=createTracker();t.update(detail(sample(["200"])),opts());
  assert.equal(t.update(detail(sample(["201"])),opts()).reason,"receipt_regressed_or_conflicted");
  assert.equal(t.update(detail(sample(["201"],base-10000)),opts()).reason,"receipt_regressed_or_conflicted");
  assert.equal(t.update(detail(sample(["201"],base+60000)),opts({now:base+60000})).reason,"previously_displayed_now_absent");
});
check("earlier change persists on repeated reads then re-evaluates on a new sample",()=>{
  const t=createTracker();t.update(detail(),opts(),estimate(base,base+2400000));
  const row=sample(["102"],base+60000),plan=opts({now:base+60000}),e=estimate(base+60000,base+1800000);
  let r=t.update(detail(row),plan,e);assert.equal(r.reason,"reference_moved_earlier");assert.equal(r.earlier_shift_minutes,10);
  r=t.update(detail(row),opts({now:base+70000}),e);assert.equal(r.reason,"reference_moved_earlier");
  r=t.update(detail(sample(["104"],base+120000)),opts({now:base+120000}),estimate(base+120000,base+1800000));
  assert.equal(r.reason,"reference_tracking");
});
check("ordinary async loading preserves comparison without presenting an old warning",()=>{
  const t=createTracker();t.update(detail(),opts(),estimate(base,base+2400000));
  assert.equal(t.update(null,opts({now:base+60000,readPending:true})).state,"loading");
  assert.equal(t.update(detail(sample(["102"],base+60000)),opts({now:base+60000}),estimate(base+60000,base+1800000)).reason,"reference_moved_earlier");
  t.update(null,opts({now:base+120000}));
  assert.equal(t.update(detail(sample(["104"],base+120000)),opts({now:base+120000}),estimate(base+120000,base+1500000)).reason,"reference_tracking");
});
check("acceleration and crossed reference are urgent inputs without probability",()=>{
  assert.equal(createTracker().update(detail(),opts(),estimate(base,base+1800000,{acceleration_signal:true})).reason,"reference_acceleration");
  assert.equal(createTracker().update(detail(),opts(),{available:false,reason:"reference_at_or_beyond_ticket"}).reason,"reference_at_or_beyond_ticket");
});
check("gaps, new run and reversal do not fabricate disappearance",()=>{
  for(const row of [sample(["201"],base+180000),sample(["201"],base+60000,{comparison_state:"run_boundary"}),sample(["90"],base+60000)]) {
    const t=createTracker();t.update(detail(sample(["100","200"])),opts());
    const r=t.update(detail(row),opts({now:Date.parse(row.queue_received_at)}));
    assert.notEqual(r.reason,"previously_displayed_now_absent");
  }
});
check("forecast is bound to this current queue receipt and truthful model state",()=>{
  for(const change of [{queue_field:"reservationQueue"},{sample_at:iso(base-1000)},{eta_available:true},
    {actual_call_verified:true},{calibrated_interval:true},{reference_time:iso(base)},
    {scenario_latest:iso(base+1000)},{scenario_earliest:iso(base-1)},{acceleration_signal:"yes"}])
    assert.equal(createTracker().update(detail(),opts(),estimate(base,base+1800000,change)).reason,"forecast_binding_invalid");
  assert.equal(createTracker().update(detail(),opts(),estimate(base,base+1800000,{scenario_latest:null})).state,"tracking");
});
check("source scope, array bound and private input cannot escape",()=>{
  for(const value of [{...detail(),requested_store_id:"900002"},{...detail(),network_performed_by_read:true},
    {...detail(),points:Array(2049).fill(sample())},detail(sample(["1","2","3","4"])),detail(sample(["1x"]))])
    assert.equal(createTracker().update(value,opts()).state,"data_unavailable");
});

// Execute the real page script with its real model and precaution module.
class Element {constructor(tag){this.tag=tag;this.children=[];this.value="";this.hidden=false;this.textContent="";this.dataset={};this.classList={add(){}};}append(e){this.children.push(e);}replaceChildren(){this.children=[];}setAttribute(){}getBoundingClientRect(){return {width:600};}getContext(){return new Proxy({},{get:()=>()=>{}});}}
const nodes=new Map(),reads=[];
let clock=base;
class Clock extends Date {constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}}
const context={console,Intl,Date:Clock,Math,Number,String,Object,Array,RegExp,JSON,Promise,AbortSignal,Map,Set,setInterval:()=>0,
  window:{devicePixelRatio:1,addEventListener(){}},document:{getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},createElement:tag=>new Element(tag)}};
context.fetch=async path=>{reads.push(path);return {ok:true,json:async()=>({days:{},store_names:{900001:"合成门店"},unavailable_store_ids:[]})};};
vm.createContext(context);
for(const name of [process.argv[4],process.argv[2]])vm.runInContext(fs.readFileSync(name,"utf8"),context);
context.window.SushiWaitReference=context.SushiWaitReference;context.window.SushiWaitReferenceAlerts=context.SushiWaitReferenceAlerts;
vm.runInContext(fs.readFileSync(process.argv[3],"utf8"),context);
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  nodes.get("store").value="900001";nodes.get("queue").value="mixedQueue";nodes.get("ticket-number").value="200";
  nodes.get("ticket-issued").value="11:00";nodes.get("ticket-offset").value="0";nodes.get("ticket-scope").checked=true;
  context.testDetail=detail(sample(["100","200"]));
  vm.runInContext('detail=testDetail;ticketScopeKey=scopeKey();renderPrediction();',context);
  assert(nodes.get("ticket-precaution").textContent.includes("出现在最新展示"));
  assert(nodes.get("prediction").textContent.includes("等待时间参考已暂停"));
  assert(!nodes.get("ticket-precaution").hidden);
  const before=reads.length;nodes.get("ticket-arrival").value="12:00";nodes.get("ticket-arrival").oninput();
  nodes.get("ticket-offset").value="10";nodes.get("ticket-offset").oninput();assert.equal(reads.length,before);
  vm.runInContext('showError()',context);assert(nodes.get("ticket-precaution").textContent.includes("暂时暂停"));
  assert(!nodes.get("ticket-precaution").textContent.includes("出现在最新展示"));
  nodes.get("queue").value="reservationQueue";nodes.get("queue").onchange();assert(nodes.get("ticket-precaution").hidden);
  assert(reads.every(p=>p.startsWith("/api/v1/")));assert(!reads.some(p=>p.includes("ticket")||p.includes("200")||p.includes("12:00")||p.includes("?")));
  cases++;
  console.log(`${cases} precaution scenarios including actual page integration passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
