'use strict';
const assert=require('node:assert/strict'),model=require('../src/sushiwait/web/reference-model.js');
const single='one_official_detail_response_not_two_query_pair';
function detail(date='2026-10-14',values=[100,110,120,130,140,150,160,170],step=10){
 const start=Date.parse(date+'T03:00:00Z');const points=values.map((v,i)=>({request_started_at:new Date(start+i*step*60000).toISOString(),queue_received_at:new Date(start+i*step*60000).toISOString(),
  observation_id:String(i),pair_ok:true,scheduled_pause:false,error_codes:{},success_semantics:single,
  queue_field_presence:{mixedQueue:'present',reservationQueue:'present'},queues:{mixedQueue:v===null?[]:[String(v),String(v+2),String(v+4)],reservationQueue:[]},
  continuity:{writer_id:'w',predecessor_writer_id:'w',predecessor_id:String(i-1)}}));
 return {source:'sapi_miniapp_gateway',api_profile:'miniapp_gateway',daily_schema_version:1,summary:{success_semantics:single,date_type:'ordinary_workday'},requested_store_id:'3014',local_date:date,
  generated_at:new Date(start+(values.length-1)*step*60000+1000).toISOString(),graph_truncated:false,network_performed_by_read:false,points};
}
function options(d,ticket='210'){return {source:d.source,storeId:'3014',queueType:'ordinary',ticket,issuedAt:d.local_date+'T02:55:00Z',now:Date.parse(d.generated_at),confirmedScope:true,samplingSeconds:600};}
assert.equal(model.displayState('150',['148','150','153']),'display_top_two');
assert.equal(model.displayState('150',['148','149','150']),'display_third');
assert.equal(model.displayState('150',['149','160','170']),'not_reached');
assert.equal(model.displayState('150',['151','160','170']),'display_all_three_passed');
let d=detail(),o=options(d,'125'),r=model.arrival(d,o);
assert.equal(r.state,'inferred_arrived');assert.equal(r.wait_lower_minutes,25);assert.equal(r.wait_upper_minutes,35);
r=model.arrival(d,options(d,'132'));assert.equal(r.state,'arrived');assert.equal(r.wait_upper_minutes,35);
d=detail('2026-10-14',[100,110,null,140]);r=model.arrival(d,options(d,'130'));assert.equal(r.wait_lower_minutes,0);
d=detail('2026-10-14',[100,1,200]);r=model.arrival(d,options(d,'150'));assert.equal(r.available,false);assert.equal(r.state,'number_cycle_uncertain');
d=detail();o=options(d);r=model.trackedForecast(d,o,[]);assert.equal(r.available,true,'ten-minute samples should produce a sparse baseline');
assert.equal(r.ai_participated,false);assert.ok(Date.parse(r.reference_time)>o.now);
const advice={schema_version:1,policy:'public_display_scenarios_v1',source:d.source,store_id:'3014',queue_type:'ordinary',local_date:d.local_date,
 sample_at:d.points.at(-1).queue_received_at,generated_at:d.generated_at,expires_at:new Date(o.now+300000).toISOString(),model:'deepseek-flash',thinking_mode:'thinking',weights_ppm:{history:0,steady:0,fast:1000000,slow:0}};
const ai=model.trackedForecast(d,o,[],advice);assert.equal(ai.ai_participated,true);assert.notDeepEqual(ai.weights,r.weights);
assert.equal(model.trackedForecast(d,o,[],{...advice,sample_at:advice.sample_at.replace('.000Z','.000000Z')}).ai_participated,true,'microsecond archive timestamps must match browser milliseconds');
assert.equal(model.trackedForecast(d,o,[],{...advice,sample_at:d.points.at(-2).queue_received_at}).ai_participated,false,'superseded model cannot participate');
assert.equal(model.trackedForecast(d,{...o,now:o.now+700000},[],advice).reason,'stale_sample');
assert.equal(model.trackedForecast(d,options(d,'132'),[],advice).reason,'arrival_complete');
assert.equal(model.arrival(d,{...o,queueType:'reservation'}).available,false);
assert.equal(model.arrival(d,{...o,storeId:'2009'}).reason,'scope_mismatch');
const bad=JSON.parse(JSON.stringify(d));bad.points[4].pair_ok=false;
assert.equal(model.trackedForecast(bad,o,[]).provisional,true,'short valid segment remains a broad provisional estimate');
const history=[detail('2026-10-12'),detail('2026-10-13')];
const sparse=detail('2026-10-14',[100]);o=options(sparse,'130');
assert.equal(model.trackedForecast(sparse,o,history).mode,'history_initial');
const before=JSON.stringify(history);model.trackedForecast(d,options(d),history);assert.equal(JSON.stringify(history),before);
console.log('arrival positions, passing, sparse history/live fusion, AI expiry and sampling boundaries passed');

// A store-wide wait requires actual issue-to-arrival labels; display numbers alone cannot create one.
d=detail();o=options(d);const base={sample_count:6,distinct_days:2,median_lower:20,median_upper:30,early_lower:10,late_upper:40,issue_rate_median:2};
assert.equal(model.storeWait(d,o,null).reason,'waiting_for_test_cohort');
const wait=model.storeWait(d,o,base);assert.equal(wait.available,true);assert.equal(wait.position_known,false);assert.equal(wait.actual_call_verified,false);assert(wait.wait_minutes>0);
assert.equal(model.storeWait(d,o,{...base,sample_count:4}).reason,'waiting_for_test_cohort');
assert.equal(model.storeWait(d,{...o,storeId:'3004'},base).reason,'scope_mismatch');
assert.equal(model.storeWait(d,{...o,now:o.now+700000},base).reason,'no_current_sample');

// A partial backfill is not a coherent reset of the displayed queue.
d=detail('2026-10-14',[100,110,108,120,130,140,139,150,160],5);o=options(d,'200');
assert.equal(model.arrival(d,o).state,'waiting');assert(model.arrival(d,o).quality_flags.includes('display_position_backfill'));
assert.equal(model.trackedForecast(d,o).available,true);
assert.equal(model.arrival(d,options(d,'115')).state,'inferred_arrived');

// Supplied issue time must not turn a number passed earlier into a new arrival.
d=detail('2026-10-14',[100,110,120,130]);o={...options(d,'105'),issuedAt:d.points[2].queue_received_at};
r=model.arrival(d,o);assert.equal(r.available,false);assert.equal(r.state,'input_conflict');
assert.equal(model.trackedForecast(d,o).reason,'ticket_precedes_issue');
const direct=JSON.parse(JSON.stringify(d));direct.points.at(-1).queues.mixedQueue=['105','135','140'];
assert.equal(model.arrival(direct,o).state,'arrived','an actual later appearance remains direct evidence');
d=detail('2026-10-14',[100,104],3);r=model.trackedForecast(d,options(d,'120'));
assert.equal(r.available,true);assert.equal(r.provisional,true);assert(Date.parse(r.scenario_latest)-Date.parse(r.scenario_earliest)>360000);

// Long waits are estimates, not missing data. Keep sparse/flat/unsafe cases distinct.
d=detail('2026-10-14',[100,110,120,130,140,150,160],10);o=options(d,'340');
r=model.trackedForecast(d,o);
assert(r.available,'a three-hour positive trend must not be discarded by the old two-hour cutoff');
assert.equal(r.provisional,true);assert.equal(r.forecast_horizon_minutes,360);
assert(Math.abs((Date.parse(r.reference_time)-Date.parse(d.points.at(-1).queue_received_at))/60000-180)<1);
assert(Date.parse(r.scenario_latest)-Date.parse(r.scenario_earliest)>=100*60000);
const longAdvice={...advice,local_date:d.local_date,sample_at:d.points.at(-1).queue_received_at,generated_at:d.generated_at,expires_at:new Date(o.now+600000).toISOString()};
assert(model.trackedForecast(d,o,[],longAdvice).ai_participated,'thinking advice participates in long-wait estimates');
assert.equal(model.trackedForecast(d,options(d,'2000')).reason,'beyond_supported_horizon');
assert.equal(model.trackedForecast(detail('2026-10-14',[100,100,100,100]),options(detail('2026-10-14',[100,100,100,100]),'200')).reason,'insufficient_sparse_trend');
// Censored histories cannot veto a usable recent trend, and are not presented as completed waits.
const censored=detail('2026-10-13',[100,101,102,103,104,105,106],10);
r=model.trackedForecast(d,{...options(d,'200'),issuedAt:d.points.at(-1).queue_received_at},[censored]);
assert(r.available);assert(r.history_censored_fallback);assert.equal(r.weights.history,0);assert.equal(r.mode,'recent_only');
const late=JSON.parse(JSON.stringify(d));for(const point of late.points)for(const k of ['request_started_at','queue_received_at'])point[k]=new Date(Date.parse(point[k])+9*3600000).toISOString();late.generated_at=new Date(Date.parse(late.generated_at)+9*3600000).toISOString();
r=model.trackedForecast(late,{...options(late,'340'),issuedAt:late.points[0].queue_received_at});assert(r.available);assert(r.beyond_business_hours,'late extrapolations must show a closing-time caveat');

// Taking a ticket between two healthy polls does not create a false outage.
d=detail('2026-10-14',[140,160],5);o={...options(d,'150'),issuedAt:d.local_date+'T03:02:00Z'};r=model.arrival(d,o);
assert.equal(r.interval_basis,'issue_to_first_sample');
const firstPoll=model.ticketTiming({result_available:r.available,state:r.state,detected_at:r.detected_at,arrival_interval:{basis:r.interval_basis,lower_at:r.lower_at,upper_at:r.upper_at},wait_minutes:{lower:r.wait_lower_minutes,upper:r.wait_upper_minutes}},{issued_at:o.issuedAt,entry_mode:'retrospective'},o.now);
assert.equal(firstPoll.status,'inferred');assert.equal(firstPoll.wait_minutes.upper,3);
