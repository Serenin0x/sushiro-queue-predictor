"use strict";
const assert = require("node:assert/strict"), model = require(process.argv[2]);
const base = Date.parse("2026-10-09T03:00:00Z"), iso = ms => new Date(ms).toISOString();
function data(count=31, rate=2) {
  const points = Array.from({length:count}, (_,i) => ({request_started_at:iso(base+i*60000-200),
    queue_received_at:iso(base+i*60000), pair_ok:true, scheduled_pause:false, error_codes:{},
    comparison_state:i?"comparable_display_sets":"insufficient",
    queues:{mixedQueue:[String(100+i*rate),"9999","9000"],reservationQueue:[String(7000+i*rate)]}}));
  return {daily_schema_version:1,source:"crm_remote_v1_1",requested_store_id:"900001",local_date:"2026-10-09",
    generated_at:points.at(-1).queue_received_at,points,network_performed_by_read:false,eta_available:false,
    first_label_is_confirmed_call:false,call_reference_semantics:"user_assumed_first_displayed_label"};
}
function options(d, changes={}) {return {storeId:"900001",queueType:"ordinary",ticket:"200",issuedAt:iso(base),
  now:Date.parse(d.points.at(-1).queue_received_at),confirmedScope:true,...changes};}
let cases=0;
function check(name, fn) {fn();cases++;}
check("steady slope, first label and clock anchor",()=>{
  const d=data(),r=model.forecast(d,options(d));assert(r.available);assert.equal(r.reference,160);
  assert.equal(r.position_difference,40);assert.equal(r.reference_time,iso(base+50*60000));
  assert.equal(r.sample_count,31);assert.equal(r.eta_available,false);assert.equal(r.calibrated_interval,false);
  assert.equal(r.actual_call_verified,false);assert.equal(r.ticket_cycle_verified,false);
});
check("queue binding and ignored aggregate",()=>{
  const d=data();d.points.forEach(p=>p.queues.storeQueue=["8000"]);
  assert.equal(model.forecast(d,options(d)).reference,160);
  assert.equal(model.forecast(d,options(d,{queueType:"reservation",ticket:"7100"})).reference,7060);
  assert.equal(model.forecast(d,options(d,{queueType:"aggregate"})).reason,"unsupported_queue");
});
for(const [change,reason] of [
  [{confirmedScope:false},"confirm_ticket_scope"],[{ticket:"A200"},"numeric_ticket_required"],
  [{ticket:"200.0"},"numeric_ticket_required"],[{ticket:"12345678"},"numeric_ticket_required"],
  [{issuedAt:null},"issue_time_required"],[{issuedAt:"2026-10-08T03:00:00Z"},"issue_time_required"],
  [{issuedAt:iso(base+31*60000)},"issue_time_required"],[{ticket:"160"},"reference_at_or_beyond_ticket"],
  [{ticket:"150"},"reference_at_or_beyond_ticket"],[{ticket:"1000"},"forecast_horizon_unsupported"],
  [{now:base+30*60000+90001},"stale_sample"],[{now:base+30*60000-1},"future_or_invalid_clock"],
  [{now:base+24*60*60000},"today_only"],[{storeId:"900002"},"scope_mismatch"]
]) check(reason,()=>{const d=data();assert.equal(model.forecast(d,options(d,change)).reason,reason);});
check("known issue after last sample waits",()=>{
  const d=data();assert.equal(model.forecast(d,options(d,{now:base+30*60000+5000,issuedAt:iso(base+30*60000+1000)})).reason,"awaiting_post_issue_sample");
});
check("five-minute minimum and plateau",()=>{
  const short=data(5);assert.equal(model.forecast(short,options(short)).reason,"insufficient_positive_trend");
  const flat=data(31,0);assert.equal(model.forecast(flat,options(flat)).reason,"insufficient_positive_trend");
});
check("one last jump survives robust smoothing",()=>{
  const d=data();d.points.at(-1).queues.mixedQueue[0]="258";
  const r=model.forecast(d,options(d,{ticket:"300"}));assert(r.available);assert(r.acceleration_signal);
  assert(r.candidates.some(c=>c.method==="latest_jump_scenario"));
  assert(Date.parse(r.scenario_earliest)<Date.parse(r.reference_time));
});
check("stall is visible without invented zero wait",()=>{
  const d=data();d.points.at(-1).queues.mixedQueue[0]="158";
  const r=model.forecast(d,options(d));assert(r.available);assert(r.latest_interval_stalled);
});
check("an isolated jump after a plateau has no positive robust baseline",()=>{
  const d=data(31,0);d.points.at(-1).queues.mixedQueue=["150"];
  assert.equal(model.forecast(d,options(d)).reason,"insufficient_positive_trend");
});
check("reversal across a failed sample still invalidates old ticket",()=>{
  const d=data();d.points[10].pair_ok=false;d.points[11].queues.mixedQueue=["10"];
  assert.equal(model.forecast(d,options(d)).reason,"ticket_cycle_uncertain");
});
check("generation cannot precede its last sample",()=>{
  const d=data();d.generated_at=iso(base+29*60000);
  assert.equal(model.forecast(d,options(d)).reason,"future_or_invalid_clock");
});
for(const mutate of [p=>p.pair_ok=false,p=>p.scheduled_pause=true,p=>p.error_codes={groupqueues:"network"},
  p=>p.queues.mixedQueue=[],p=>p.queues.mixedQueue=["X160"],p=>p.queue_received_at="2026-02-30T03:00:00Z",
  p=>p.request_started_at=iso(base+40*60000)]) check("latest failure clears result",()=>{
  const d=data();mutate(d.points.at(-1));assert.equal(model.forecast(d,options(d,{now:base+30*60000})).reason,"no_current_sample");
});
for(const state of ["gap","run_boundary","time_order_or_duplicate","insufficient"]) check(state,()=>{
  const d=data();d.points.at(-1).comparison_state=state;
  assert.equal(model.forecast(d,options(d)).reason,"insufficient_positive_trend");
});
check("reversal invalidates older ticket cycle after recovery",()=>{
  const d=data();d.points[10].queues.mixedQueue=["10"];
  assert.equal(model.forecast(d,options(d)).reason,"ticket_cycle_uncertain");
  assert(model.forecast(d,options(d,{issuedAt:iso(base+11*60000)})).available);
});
check("failure does not stitch slope across a gap",()=>{
  const d=data();d.points[26].pair_ok=false;
  assert.equal(model.forecast(d,options(d)).reason,"insufficient_positive_trend");
});
check("sample density and projection size bounded",()=>{
  const d=data(65);d.points.forEach((p,i)=>{p.request_started_at=iso(base+i*10000-200);p.queue_received_at=iso(base+i*10000);});
  d.generated_at=d.points.at(-1).queue_received_at;
  assert.equal(model.forecast(d,options(d,{ticket:"300"})).reason,"sampling_density_unsupported");
  const oversized=data(2049);assert.equal(model.forecast(oversized,options(oversized,{now:base+60000})).reason,"invalid_projection");
});
check("prefix-only rolling replay and interval scoring",()=>{
  const d=data(61),r=model.replay(d,{storeId:"900001",queueType:"ordinary",offset:50,stride:20});
  assert.equal(r.actual_call_verified,false);assert.equal(r.calibration_for_actual_wait,false);
  assert.equal(r.cases[0].state,"unavailable");assert.equal(r.cases[1].state,"reference_crossing_observed");
  assert.equal(r.cases[1].predicted_reference_time,iso(base+45*60000));
  assert.equal(r.cases[1].signed_error_to_interval_minutes,0);assert.equal(r.cases[2].state,"right_censored");
  const changed=structuredClone(d);changed.points.slice(21).forEach((p,i)=>p.queues.mixedQueue=[String(200+i*10)]);
  const r2=model.replay(changed,{storeId:"900001",queueType:"ordinary",offset:50,stride:20});
  assert.equal(r.cases[1].predicted_reference_time,r2.cases[1].predicted_reference_time);
});
check("future boundary produces censorship, not call or skip",()=>{
  const d=data(61);d.points[21].pair_ok=false;
  const r=model.replay(d,{storeId:"900001",queueType:"ordinary",offset:50,stride:20});
  assert.equal(r.cases[1].state,"boundary_censored");assert(!("called" in r.cases[1]));
});
check("invalid replay bounds",()=>assert.equal(model.replay(data(),{queueType:"ordinary",stride:1}).error,"invalid_replay_request"));
function archived(day, count=91, rate=2) {
  const d=data(count,rate),shift=Date.parse(`${day}T03:00:00Z`)-base;
  d.local_date=day;d.graph_truncated=false;d.summary={date_type:"ordinary_workday"};
  d.points.forEach(p=>{for(const key of ["request_started_at","queue_received_at"])p[key]=iso(Date.parse(p[key])+shift);});
  d.generated_at=d.points.at(-1).queue_received_at;return d;
}
function withHistory(d,days,changes={}) {
  d.graph_truncated=false;d.summary={date_type:"ordinary_workday"};
  return model.forecastWithHistory(d,options(d,changes),days);
}
check("history starts before a five-minute current trend exists",()=>{
  const d=data(4),r=withHistory(d,[archived("2026-10-08"),archived("2026-10-07")],{ticket:"136"});
  assert(r.available);assert.equal(r.policy,model.HISTORY_POLICY);assert.equal(r.mode,"history_initial");
  assert.equal(r.live_weight,0);assert.equal(r.history.selected_days,2);assert.equal(r.history.samples,4);
  assert.equal(r.history.cohort,"date_type_season_time");assert.equal(r.history.interval_observed,4);
  assert.equal(r.reference_time,iso(base+(3+14.5)*60000));assert.equal(r.calibrated_interval,false);
});
check("right-censored windows retain unknown late endpoint",()=>{
  const r=withHistory(data(4),[archived("2026-10-08",61),archived("2026-10-07",61)],{ticket:"136"});
  assert(r.available);assert.equal(r.history.right_censored,2);assert.equal(r.scenario_latest,null);
  assert.equal(r.empirical_quantile_bounds_minutes_from_sample["0.9"].upper,null);
});
check("unobserved crossings cannot yield a fabricated finite median",()=>{
  const r=withHistory(data(4),[archived("2026-10-08",91,0),archived("2026-10-07",91,0)]);
  assert.equal(r.available,false);assert.equal(r.history.state,"median_upper_unknown");
});
check("history failure preserves valid recent-only forecast",()=>{
  const d=data(),r=withHistory(d,[archived("2026-10-08")]);
  assert(r.available);assert.equal(r.mode,"recent_only");assert.equal(r.history.state,"insufficient_history");
  assert.equal(r.reference_time,model.forecast(d,options(d)).reference_time);
});
check("new observed span increases live weight, wall-clock aging does not",()=>{
  const days=[archived("2026-10-08"),archived("2026-10-07")],a=data(11),b=data(21);
  const ra=withHistory(a,days,{ticket:"160"}),rb=withHistory(b,days,{ticket:"180"});
  assert.equal(ra.mode,"history_live_mix");assert.equal(ra.live_weight,.425);assert.equal(rb.live_weight,.85);
  assert.equal(withHistory(a,days,{ticket:"160",now:options(a).now+30000}).live_weight,ra.live_weight);
});
check("latest acceleration raises live weight and early scenario",()=>{
  const d=data();d.points.at(-1).queues.mixedQueue=["258"];
  const r=withHistory(d,[archived("2026-10-08",91,10),archived("2026-10-07",91,10)],
    {ticket:"300",issuedAt:d.points.at(-1).queue_received_at});
  assert(r.available);assert(r.acceleration_signal);assert.equal(r.live_weight,.75);
});
check("history selection preserves boundary censorship",()=>{
  const days=[archived("2026-10-08"),archived("2026-10-07")];
  days.forEach(d=>{d.points[61].pair_ok=false;});
  const r=withHistory(data(4),days,{ticket:"136"});assert.equal(r.history.boundary_censored,2);
});
check("historical times are intervals, not interpolated exact events",()=>{
  const r=withHistory(data(4),[archived("2026-10-08"),archived("2026-10-07")],{ticket:"136"});
  assert.deepEqual(r.empirical_quantile_bounds_minutes_from_sample["0.5"],{lower:14,upper:15});
  assert.equal(r.central_semantics,"midpoint_of_empirical_median_bounds");assert.equal(r.actual_call_verified,false);
});
check("one densely observed day cannot outweigh another day",()=>{
  const r=withHistory(data(4),[archived("2026-10-08",91,1),archived("2026-10-07",31,2)],{ticket:"136"});
  assert.equal(r.history.samples,3);assert.deepEqual(r.empirical_quantile_bounds_minutes_from_sample["0.5"],{lower:14,upper:15});
});
for(const mutate of [d=>d.requested_store_id="900002",d=>d.graph_truncated=true,
  d=>d.local_date="2026-10-09",d=>d.local_date="2026-10-10",d=>d.local_date="2026-02-30",
  d=>d.generated_at=iso(base+40*60000),d=>d.generated_at=iso(Date.parse(d.points[1].queue_received_at)),
  d=>d.network_performed_by_read=true,d=>d.first_label_is_confirmed_call=true])
  check("invalid, future or mismatched archive excluded",()=>{
    const past=archived("2026-10-08");mutate(past);
    const r=withHistory(data(4),[past,archived("2026-10-07")]);
    assert.equal(r.available,false);assert.equal(r.history.excluded_days,1);assert.equal(r.history.selected_days,0);
  });
check("duplicates do not masquerade as independent days",()=>{
  const d=archived("2026-10-08"),r=withHistory(data(4),[d,structuredClone(d)]);
  assert.equal(r.available,false);assert.equal(r.history.accepted_days,1);assert.equal(r.history.excluded_days,1);
});
check("holiday archives are not pooled into ordinary workdays",()=>{
  const days=[archived("2026-10-08"),archived("2026-10-07")];days.forEach(d=>d.summary.date_type="holiday");
  assert.equal(withHistory(data(4),days).history.state,"insufficient_history");
});
check("history cannot bypass fresh-sample, cycle and scope guards",()=>{
  const days=[archived("2026-10-08"),archived("2026-10-07")],d=data();
  assert.equal(withHistory(d,days,{now:options(d).now+90001}).reason,"stale_sample");
  assert.equal(withHistory(d,days,{confirmedScope:false}).reason,"confirm_ticket_scope");
  d.points[10].queues.mixedQueue=["10"];
  assert.equal(withHistory(d,days).reason,"ticket_cycle_uncertain");
});
check("history work bounds and caller data stay immutable",()=>{
  const d=data(),days=[archived("2026-10-08"),archived("2026-10-07")],before=JSON.stringify([d,days]);
  d.graph_truncated=false;d.summary={date_type:"ordinary_workday"};const canonical=JSON.stringify([d,days]);
  model.forecastWithHistory(d,options(d),days);assert.equal(JSON.stringify([d,days]),canonical);assert(before);
  assert.equal(withHistory(d,Array(8).fill(days[0])).history.accepted_days,0);
  d.graph_truncated=true;assert.equal(model.forecastWithHistory(d,options(d),days).reason,"truncated_current_projection");
});
console.log(`${cases} reference-model cases passed`);
