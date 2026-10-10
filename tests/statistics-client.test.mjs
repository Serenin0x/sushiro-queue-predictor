import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createStatisticsClient, createStatisticsController, validateMonth, validateDay,
  calendarCells, chartSeries, latestQueue, shanghaiDate} from '../docs/design/integration/statistics-client.mjs';

const fixture = JSON.parse(await readFile(new URL('../docs/design/integration/statistics.synthetic.json', import.meta.url), 'utf8'));
assert.equal(fixture.synthetic, true);
const index = fixture.month_index, detail = fixture.day_detail;
const copy = v => JSON.parse(JSON.stringify(v));
const now = Date.parse(detail.points.at(-1).queue_received_at) + 5000;
const response = v => ({ok: true, status: 200, text: async () => JSON.stringify(v)});
const deferred = () => {let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {resolve,reject,promise};};
let passed = 0;
async function check(name, fn) {await fn(); passed++; console.log(`ok ${passed} - ${name}`);}

await check('fixed GETs and immutable scoped responses', async () => {
  const calls = [];
  const client = createStatisticsClient({fetchImpl: async (path, options) => {calls.push({path,options}); return response(path.includes('/months/') ? index : detail);}});
  const month = await client.readMonth('2026-10');
  const day = await client.readDay('900001','2026-10-06',month.configured_store_ids);
  assert.deepEqual(calls.map(x => x.path), ['/api/v1/months/2026-10','/api/v1/stores/900001/days/2026-10-06']);
  assert(calls.every(x => x.options.method === 'GET' && x.options.cache === 'no-store'));
  assert.throws(() => day.points[0].queues.mixedQueue.push('9'), TypeError);
  assert.deepEqual(day.points[0].queues.mixedQueue, ['0012','0011','0014']);
});
await check('invalid dates, queries and stores perform no fetch', async () => {
  let calls=0; const c=createStatisticsClient({fetchImpl: async () => {calls++; return response(index);}});
  for (const month of ['2026-00','2026-13','2026-10?x=1']) await assert.rejects(c.readMonth(month));
  for (const date of ['2026-02-30','2026-10-06?ticket=12']) await assert.rejects(c.readDay('900001',date,['900001']));
  await assert.rejects(c.readDay('3014','2026-10-06',['900001']), e=>e.code==='unknown_store');
  assert.equal(calls,0);
});
await check('scope and contract failures rejected before presentation', async () => {
  assert.throws(()=>validateMonth({...index,month:'2026-09'},'2026-10'));
  assert.throws(()=>validateDay({...detail,requested_store_id:'900002'},'900001','2026-10-06'));
  const bad=copy(detail); bad.points[0].operation_started_at=bad.points[0].request_started_at; delete bad.points[0].request_started_at;
  assert.throws(()=>validateDay(bad,'900001','2026-10-06'));
  assert.throws(()=>validateDay({...detail,returned_graph_points:4},'900001','2026-10-06'));
});
await check('plaintext errors never parsed or exposed as successful data', async () => {
  const c=createStatisticsClient({fetchImpl:async()=>({ok:false,status:503,text:async()=>{throw Error('should not parse');}})});
  await assert.rejects(c.readMonth('2026-10'),e=>e.code==='http_error'&&e.httpStatus===503);
  const invalid=createStatisticsClient({fetchImpl:async()=>({ok:true,status:200,text:async()=>'<html>not data</html>'})});
  await assert.rejects(invalid.readMonth('2026-10'),e=>e.code==='invalid_json');
});
await check('deadline and external cancellation remove stale reads', async () => {
  const aborting=(_path,{signal})=>new Promise((_resolve,reject)=>{if(signal.aborted)reject(Error('aborted'));else signal.addEventListener('abort',()=>reject(Error('aborted')),{once:true});});
  const c=createStatisticsClient({fetchImpl:aborting,timeoutMs:5});
  await assert.rejects(c.readMonth('2026-10'),e=>e.code==='read_timeout');
  const external=new AbortController(); external.abort();
  await assert.rejects(c.readMonth('2026-10',{signal:external.signal}),e=>e.code==='read_cancelled');
});
await check('day byte bound counts UTF8 rather than characters alone', async () => {
  const c=createStatisticsClient({fetchImpl:async()=>({ok:true,status:200,text:async()=> '中'.repeat(800000)})});
  await assert.rejects(c.readDay('900001','2026-10-06',['900001']),e=>e.code==='response_too_large');
});
await check('latest failed observation cannot revive previous numbers', () => {
  const d=copy(detail); d.points.push({...d.points.at(-1),queues:null,queue_received_at:null,pair_ok:false});
  assert.equal(latestQueue(d,'mixedQueue',{now}).state,'unavailable');
  assert.equal(latestQueue(d,'mixedQueue',{now}).labels,null);
  d.points.pop(); d.points.at(-1).queues.mixedQueue=[];
  const empty=latestQueue(d,'mixedQueue',{now}); assert.equal(empty.state,'fresh'); assert.equal(empty.isEmpty,true); assert.deepEqual(empty.labels,[]);
});
await check('timestamps distinguish stale, historical and future responses', () => {
  const at=Date.parse(detail.points.at(-1).queue_received_at);
  assert.equal(latestQueue(detail,'mixedQueue',{now:at+90001}).state,'stale');
  assert.equal(latestQueue(detail,'mixedQueue',{now:at-1}).state,'unavailable');
  assert.equal(latestQueue(detail,'mixedQueue',{now:at-1,today:'2026-10-07'}).state,'unavailable');
  assert.equal(latestQueue(detail,'mixedQueue',{now,today:'2026-10-07'}).state,'historical');
  assert.equal(shanghaiDate(Date.parse('2026-10-05T16:00:00Z')),'2026-10-06');
  assert.equal(latestQueue(detail,'mixedQueue',{now}).isConfirmedCall,false);
});
await check('graphs preserve raw labels and break at gap, reversal and failure', () => {
  const d=copy(detail); d.points[1].comparison_state='gap';
  d.points[2].queues.mixedQueue=['0009','0017','0017'];
  const view=chartSeries(d,'mixedQueue');
  assert.equal(view.reference.length,3); assert.equal(view.reference[0][0].label,'0012');
  assert.equal(view.countUnit,'unknown'); assert.equal(view.actualCalledCount,null); assert.equal(view.noShowRate,null);
  assert.equal(view.turnover.length,1); assert.equal(view.turnoverUnit,'display_labels_per_minute');
  const p=copy(d.points[0]); p.queues=null; d.points.splice(1,0,p);
  assert.equal(chartSeries(d,'mixedQueue').reference.length,3);
});
await check('calendar coverage is weighted and missing days stay unknown', () => {
  const m=copy(index),date='2026-10-06',a=m.days[date]['900001'];
  m.store_names['900002']='第二合成门店';m.configured_store_ids.push('900002');
  Object.assign(a,{expected_background_slots_so_far:10,observed_background_slots:10,observed_slot_fraction_so_far:1});
  m.days[date]['900002']={...a,store_id:'900002',expected_background_slots_so_far:90,observed_background_slots:0,observed_slot_fraction_so_far:0};
  const cells=calendarCells(m,'2026-10');assert.equal(cells.length,31);assert.equal(cells[5].coverage,.1);assert.equal(cells[0].observations,null);
  m.calendar_pending_store_ids=['900002'];m.unavailable_store_ids=['900002'];m.calendar_index_state='preparing';m.calendar_verified_store_count=1;
  const pending=calendarCells(m,'2026-10')[5];assert.equal(pending.state,'pending');assert.equal(pending.coverageIsPartial,true);
  const feb={...index,month:'2028-02',days:{}};assert.equal(calendarCells(feb,'2028-02').length,29);
});
await check('late index success and failure cannot overwrite newer selection', async () => {
  const a=deferred(),b=deferred();let calls=0;let oldSignal;
  const c=createStatisticsController({client:{readMonth:(_m,{signal})=>{calls++;if(calls===1){oldSignal=signal;return a.promise;}return b.promise;},readDay:async()=>detail}});
  const old=c.select({month:'2026-09',date:'2026-09-01'});
  const fresh=c.select({month:'2026-10',date:'2026-10-06'});
  assert(oldSignal.aborted);b.resolve(index);await fresh;a.resolve({...index,month:'2026-09'});await old;
  assert.equal(c.snapshot().index.month,'2026-10');assert.equal(c.snapshot().phase,'ready');
  const fail=deferred();c.stop();
  const failed=createStatisticsController({client:{readMonth:m=>m==='2026-09'?fail.promise:Promise.resolve(index),readDay:async()=>detail}});
  const previous=failed.select({month:'2026-09',date:'2026-09-01'});await failed.select({month:'2026-10',date:'2026-10-06'});fail.reject(Error('old failure'));await previous;
  assert.equal(failed.snapshot().phase,'ready');
});
await check('late day response cannot cross store selection', async () => {
  const m=copy(index);m.configured_store_ids.push('900002');m.store_names['900002']='第二合成门店';
  const a=deferred();
  const c=createStatisticsController({client:{readMonth:async()=>m,readDay:id=>id==='900001'?a.promise:Promise.resolve({...detail,requested_store_id:'900002'})}});
  const old=c.select({month:'2026-10',date:'2026-10-06',storeId:'900001'});
  await Promise.resolve();await Promise.resolve();
  await c.select({month:'2026-10',date:'2026-10-06',storeId:'900002'});a.resolve(detail);await old;
  assert.equal(c.snapshot().detail.requested_store_id,'900002');
});
await check('queue switch uses saved arrays and refreshes do not overlap', async () => {
  assert.throws(()=>createStatisticsController({client:{readMonth(){},readDay(){}},pollMs:2147483648}));
  let reads=0;const wait=deferred();let job;
  const c=createStatisticsController({client:{readMonth:async()=>{reads++;return index;},readDay:()=>wait.promise},setIntervalImpl:(fn,ms)=>{assert.equal(ms,30000);job=fn;return 42;},clearIntervalImpl:id=>assert.equal(id,42)});
  const initial=c.select({month:'2026-10',date:'2026-10-06',storeId:'900001'});
  assert.equal(c.refresh(),initial);c.start();c.start();job();assert.equal(reads,1);
  const queue=c.select({month:'2026-10',date:'2026-10-06',storeId:'900001',queue:'reservationQueue'});
  assert.equal(queue,initial);wait.resolve(detail);await initial;
  assert.equal(c.snapshot().selection.queue,'reservationQueue');assert.equal(reads,1);
  c.stop();assert.equal(c.snapshot().detail,null);assert.equal(c.snapshot().phase,'idle');
});
await check('failed detail retains known calendar and pauses current cards', async () => {
  const c=createStatisticsController({client:{readMonth:async()=>index,readDay:async()=>{const e=Error('failure');e.code='http_error';e.httpStatus=503;throw e;}}});
  await c.select({month:'2026-10',date:'2026-10-06',storeId:'900001'});
  assert.equal(c.snapshot().index,index);assert.equal(c.snapshot().detail,null);assert.equal(c.snapshot().phase,'partial');assert.equal(c.snapshot().detailState,'error');assert.equal(c.snapshot().error.httpStatus,503);
});
await check('unknown selected directory ID never requests a day', async () => {
  let days=0;const c=createStatisticsController({client:{readMonth:async()=>index,readDay:async()=>{days++;return detail;}}});
  await c.select({month:'2026-10',date:'2026-10-06',storeId:'3014'});
  assert.equal(days,0);assert.equal(c.snapshot().error.code,'unknown_store');
});
console.log(`${passed} integration cases passed; synthetic only; external requests 0`);
