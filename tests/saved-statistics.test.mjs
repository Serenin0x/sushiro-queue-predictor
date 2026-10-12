import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createSavedStatisticsClient,mergeSavedMonths,savedSourceOptions,selectSavedSource,savedCalendarCells,SAVED_SOURCE} from '../src/sushiwait/web/saved-statistics.mjs';
import {createStatisticsController} from '../src/sushiwait/web/statistics-client.mjs';

const fixture=JSON.parse(await readFile(new URL('../docs/design/integration/statistics.synthetic.json',import.meta.url),'utf8'));
const copy=value=>JSON.parse(JSON.stringify(value));
const crm=copy(fixture.month_index),official=copy(fixture.month_index);
const success='one_official_detail_response_not_two_query_pair';
official.source='sapi_miniapp_gateway';official.api_profile='miniapp_gateway';
for(const rows of Object.values(official.days))for(const row of Object.values(rows))row.success_semantics=success;
const date='2026-10-06',id='900001';
const day=copy(fixture.day_detail),officialDay=copy(day);
officialDay.source=official.source;officialDay.api_profile=official.api_profile;officialDay.summary.success_semantics=success;
for(const point of officialDay.points)point.success_semantics=success;
let passed=0;
async function check(name,fn){await fn();passed++;console.log('ok '+passed+' - '+name);}

await check('one calendar includes both saved sets without rewriting either',()=>{
  const before=JSON.stringify([crm,official]),index=mergeSavedMonths([crm,official],crm.month);
  assert.equal(index.source,SAVED_SOURCE);
  assert.equal(index.days[date][id].observations,crm.days[date][id].observations*2);
  assert.equal(index.days[date][id].observed_slot_fraction_so_far,null);
  assert.deepEqual(savedSourceOptions(index,id,date),['sapi_miniapp_gateway','crm_remote_v1_1']);
  assert.equal(JSON.stringify([crm,official]),before);
  assert.throws(()=>index.days[date][id].source_summaries.crm_remote_v1_1.observations++,TypeError);
});
await check('historical dates default to saved history; today retains current chain',()=>{
  const index=mergeSavedMonths([crm,official],crm.month);
  assert.equal(selectSavedSource(index,id,date,null,{today:'2026-10-12'}),'crm_remote_v1_1');
  assert.equal(selectSavedSource(index,id,date,null,{today:date}),'sapi_miniapp_gateway');
  assert.equal(selectSavedSource(index,id,date,'sapi_miniapp_gateway'),'sapi_miniapp_gateway');
  assert.throws(()=>selectSavedSource(index,id,date,'untrusted_source'));
});
await check('empty current date still requests current chain rather than old numbers',()=>{
  const empty=copy(official);empty.days={};
  const index=mergeSavedMonths([crm,empty],crm.month);
  assert.equal(selectSavedSource(index,id,date,null,{today:date}),'sapi_miniapp_gateway');
});
await check('mixed record counts are visible but do not claim one complete stream',()=>{
  const index=mergeSavedMonths([crm,official],crm.month),cell=savedCalendarCells(index,crm.month,[id])[5];
  assert.equal(cell.observations,index.days[date][id].observations);
  assert.equal(cell.coverage,null);assert.equal(cell.coverageIsPartial,true);
  assert.equal(cell.heatmapSemantics,'coverage_only_traffic_not_calibrated');
});
await check('only existing read-only saved routes are used, day source stays original',async()=>{
  const calls=[];
  const client=createSavedStatisticsClient({fetchImpl:async(path,options)=>{
    calls.push({path,options});const isOfficial=path.startsWith('/api/v1/official/');
    const value=path.includes('/months/')?(isOfficial?official:crm):(isOfficial?officialDay:day);
    return {ok:true,status:200,text:async()=>JSON.stringify(value)};
  }});
  const index=await client.readMonth(crm.month);
  const detail=await client.readDay(id,date,index.configured_store_ids,{index,source:'sapi_miniapp_gateway'});
  assert.equal(detail.source,'sapi_miniapp_gateway');assert.deepEqual(detail.points,officialDay.points);
  assert.deepEqual(calls.map(c=>c.path),['/api/v1/official/months/2026-10','/api/v1/months/2026-10','/api/v1/official/stores/900001/days/2026-10-06']);
  assert(calls.every(c=>c.options.method==='GET'&&c.options.cache==='no-store'));
});
await check('failed current day read never falls back to a historical day',async()=>{
  let oldDayReads=0;
  const client=createSavedStatisticsClient({clients:{
    sapi_miniapp_gateway:{readMonth:async()=>official,readDay:async()=>{throw Error('current unavailable');}},
    crm_remote_v1_1:{readMonth:async()=>crm,readDay:async()=>{oldDayReads++;return day;}}
  }});
  const index=await client.readMonth(crm.month);
  await assert.rejects(client.readDay(id,date,index.configured_store_ids,{index,source:'sapi_miniapp_gateway'}));
  assert.equal(oldDayReads,0);
});
await check('failed month does not masquerade as the other source',async()=>{
  const client=createSavedStatisticsClient({clients:{
    sapi_miniapp_gateway:{readMonth:async()=>{throw Error('current unavailable');},readDay:async()=>officialDay},
    crm_remote_v1_1:{readMonth:async()=>crm,readDay:async()=>day}
  }});
  await assert.rejects(client.readMonth(crm.month));
});
await check('controller can switch record sets without joining day curves',async()=>{
  const reads=[];
  const client=createSavedStatisticsClient({clients:{
    sapi_miniapp_gateway:{readMonth:async()=>official,readDay:async()=>{reads.push('official');return officialDay;}},
    crm_remote_v1_1:{readMonth:async()=>crm,readDay:async()=>{reads.push('crm');return day;}}
  }});
  const controller=createStatisticsController({client});
  await controller.select({month:crm.month,date,storeId:id,recordSource:'crm_remote_v1_1'});
  assert.equal(controller.snapshot().detail.source,'crm_remote_v1_1');
  await controller.select({month:crm.month,date,storeId:id,recordSource:'sapi_miniapp_gateway'});
  assert.equal(controller.snapshot().detail.source,'sapi_miniapp_gateway');
  assert.deepEqual(controller.snapshot().detail.points,officialDay.points);
  assert.deepEqual(reads,['crm','official']);
});
console.log(passed+' saved statistics cases passed');
