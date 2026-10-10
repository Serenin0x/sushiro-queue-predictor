import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createDisplayCache,directory,calendarForScope,dayKind,moveMonth,queuePresentation,recentRecords,plots,nearestPoint,timeText,shanghaiDate} from '../src/sushiwait/web/calendar-data.mjs';
import {createStatisticsClient,createStatisticsController} from '../src/sushiwait/web/statistics-client.mjs';
const fixture=JSON.parse(await readFile(new URL('../docs/design/integration/statistics.synthetic.json',import.meta.url),'utf8'));
const copy=v=>JSON.parse(JSON.stringify(v));let passed=0;
async function check(name,fn){await fn();console.log(`ok ${++passed} - ${name}`);}
await check('city directory follows API IDs and names, unknown city stays unknown',()=>{
  const index={...fixture.month_index,configured_store_ids:['3004','3014','900001'],store_names:{3004:'API店名',3014:'另一家',900001:'目录外城市'}};
  assert.deepEqual(directory(index),[{id:'3004',name:'API店名',city:'北京'},{id:'3014',name:'另一家',city:'北京'},{id:'900001',name:'目录外城市',city:'城市待核'}]);
});
await check('city aggregation is weighted; valid zero differs from missing',()=>{
  const index=copy(fixture.month_index),row=index.days['2026-10-06']['900001'];
  index.configured_store_ids=['3004','3014','2009'];index.store_names={3004:'北京一店',3014:'北京二店',2009:'成都店'};
  index.days['2026-10-06']={3004:{...row,store_id:'3004',observations:0,observed_background_slots:1,expected_background_slots_so_far:10},3014:{...row,store_id:'3014',observations:2,observed_background_slots:9,expected_background_slots_so_far:90},2009:{...row,store_id:'2009'}};
  const cells=calendarForScope(index,'2026-10','北京');assert.equal(cells[5].observations,2);assert.equal(cells[5].coverage,.1);assert.equal(cells[5].configuredStoreCount,2);assert.equal(cells[4].observations,null);
  assert.equal(calendarForScope(index,'2026-10','北京','3004')[5].observations,0);
  index.unavailable_store_ids=['3014'];index.calendar_pending_store_ids=['3014'];index.calendar_index_state='preparing';index.calendar_verified_store_count=2;
  const pending=calendarForScope(index,'2026-10','北京')[5];assert.equal(pending.state,'pending');assert.equal(pending.coverageIsPartial,true);
});
await check('year boundaries, leap month and explicitly known holiday year',()=>{
  assert.equal(moveMonth('2026-01',-1),'2025-12');assert.equal(moveMonth('2026-12',1),'2027-01');
  assert.equal(dayKind('2026-10-10').short,'班');assert.equal(dayKind('2026-10-01').short,'休');assert.equal(dayKind('2027-10-01').short,'');assert.match(dayKind('2027-10-01').name,/待核/);
  const index={...fixture.month_index,month:'2028-02',days:{}};assert.equal(calendarForScope(index,'2028-02').length,29);
});
await check('original labels, repeats and leading zeroes survive through UI data',()=>{
  const detail=copy(fixture.day_detail);detail.points.at(-1).queues.mixedQueue=['0017','0017','A-3'];
  const now=Date.parse(detail.points.at(-1).queue_received_at)+5000;
  assert.equal(queuePresentation(detail,'mixedQueue',now).display,'0017 · 0017 · A-3');
  assert.equal(recentRecords(detail)[0].dine,'0017 · 0017 · A-3');assert.equal(recentRecords(detail)[0].at,detail.points.at(-1).queue_received_at);
});
await check('failed final queue never becomes fresh, count failure preserves queues',()=>{
  const detail=copy(fixture.day_detail),point=detail.points.at(-1),now=Date.parse(point.queue_received_at)+5000;
  point.count_raw=null;point.count_received_at=null;point.pair_ok=false;
  assert.match(queuePresentation(detail,'mixedQueue',now).display,/0016/);assert.equal(recentRecords(detail)[0].state,'数量缺失');
  point.queues=null;point.queue_received_at=null;assert.equal(queuePresentation(detail,'mixedQueue',now).display,'暂无可用号码');assert.equal(recentRecords(detail)[0].dine,'读取失败');
});
await check('valid empty lists, stale responses and historical day remain distinct',()=>{
  const detail=copy(fixture.day_detail);detail.local_date=shanghaiDate();const now=Date.now();detail.points.at(-1).queue_received_at=new Date(now-5000).toISOString();
  detail.points.at(-1).queues.mixedQueue=[];assert.equal(queuePresentation(detail,'mixedQueue',now).display,'本次未显示号码');
  detail.points.at(-1).queues.mixedQueue=['99'];assert.equal(queuePresentation(detail,'mixedQueue',now+100000).display,'暂无可用号码');
  detail.local_date='2026-10-06';assert.equal(queuePresentation(detail,'mixedQueue',now+100000).state,'historical');
});
await check('single quantity stream and nearest real observation, no interpolation',()=>{
  const chart=plots(fixture.day_detail);assert.deepEqual(chart.quantity,chart.dine.rawCount);assert.equal(chart.quantity.length,3);
  const points=chart.dine.reference.flat();const near=nearestPoint(points,points[0].at+10000);assert.equal(near.label,'0012');assert.equal(near.at,points[0].at);assert.equal(timeText(points[0].at),'20:00:00');
});
await check('all scope reads month only; one selected store reads only one detail',async()=>{
  const calls=[];const client=createStatisticsClient({fetchImpl:async path=>{calls.push(path);return {ok:true,text:async()=>JSON.stringify(path.includes('/months/')?fixture.month_index:fixture.day_detail)};}});
  const controller=createStatisticsController({client});await controller.select({month:'2026-10',date:'2026-10-06',storeId:null});assert.equal(calls.length,1);
  await controller.select({month:'2026-10',date:'2026-10-06',storeId:'900001'});assert.deepEqual(calls.slice(1),['/api/v1/months/2026-10','/api/v1/stores/900001/days/2026-10-06']);controller.stop();
});
await check('same-scope loading retains saved table data without declaring it fresh',()=>{
  const cache=createDisplayCache(),selection={month:'2026-10',date:'2026-10-06',storeId:'900001'};
  cache.remember({phase:'ready',selection,index:fixture.month_index,detail:fixture.day_detail,detailState:'ready'});
  const next={phase:'loading',selection,index:null,detail:null,detailState:'loading'},view=cache.view(next,{month:selection.month,date:selection.date,store:selection.storeId});
  assert.equal(view.index,fixture.month_index);assert.equal(view.detail,fixture.day_detail);assert.equal(view.retainedDetail,true);assert.equal(view.detailState,'loading');
  assert.equal(next.detail,null);assert.equal(cache.view(next,{month:'2026-09',date:'2026-09-06',store:'900001'}).detail,null);
  assert.equal(cache.view(next,{month:selection.month,date:'2026-10-07',store:'900001'}).detail,null);
  assert.equal(cache.view(next,{month:selection.month,date:selection.date,store:'3004'}).detail,null);
});
await check('failed refresh discards display cache and never revives earlier success',()=>{
  const cache=createDisplayCache(),selection={month:'2026-10',date:'2026-10-06',storeId:'900001'},scope={month:'2026-10',date:'2026-10-06',store:'900001'};
  const ready={phase:'ready',selection,index:fixture.month_index,detail:fixture.day_detail,detailState:'ready'};cache.remember(ready);
  const partial={phase:'partial',selection,index:fixture.month_index,detail:null,detailState:'error'};cache.remember(partial);
  assert.equal(cache.view(partial,scope).detail,null);assert.equal(cache.view({...partial,phase:'loading',detailState:'loading'},scope).detail,null);
  cache.remember(ready);const failed={phase:'error',selection,index:null,detail:null,detailState:'error'};cache.remember(failed);
  const retry=cache.view({...failed,phase:'loading',detailState:'loading'},scope);assert.equal(retry.index,null);assert.equal(retry.detail,null);
});
await check('display cache is bounded and removed directory IDs cannot reappear',()=>{
  const cache=createDisplayCache(2);
  for(const date of ['2026-10-04','2026-10-05','2026-10-06'])cache.remember({phase:'ready',index:fixture.month_index,detailState:'ready',detail:{...fixture.day_detail,local_date:date}});
  const loading={phase:'loading',detail:null,detailState:'loading'};
  assert.equal(cache.view(loading,{month:'2026-10',date:'2026-10-04',store:'900001'}).detail,null);
  assert.equal(cache.view(loading,{month:'2026-10',date:'2026-10-05',store:'900001'}).detail.local_date,'2026-10-05');
  cache.remember({phase:'ready',index:{...fixture.month_index,configured_store_ids:[]},detailState:'unselected'});
  assert.equal(cache.view(loading,{month:'2026-10',date:'2026-10-06',store:'900001'}).detail,null);
});
assert.equal(await readFile(new URL('../src/sushiwait/web/statistics-client.mjs',import.meta.url),'utf8'),await readFile(new URL('../docs/design/integration/statistics-client.mjs',import.meta.url),'utf8'));
console.log(`${passed} calendar integration cases passed`);
