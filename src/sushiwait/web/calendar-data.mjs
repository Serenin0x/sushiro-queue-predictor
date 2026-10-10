import {calendarCells, latestQueue, chartSeries, shanghaiDate} from './statistics-client.mjs';
import {cityById, calendar2026} from './calendar-directory.mjs';

export {shanghaiDate};
// Display-only memory: keep the same scope in place while the controller reads.
// It never supplies numbers to latestQueue or revives a failed response as fresh.
export function createDisplayCache(limit=4) {
  if(!Number.isInteger(limit)||limit<1||limit>12)throw new RangeError('invalid_display_cache_limit');
  const months=new Map(), days=new Map();
  const key=(store,date)=>store+'|'+date;
  const put=(map,id,value)=>{map.delete(id);map.set(id,value);while(map.size>limit)map.delete(map.keys().next().value);};
  return {
    remember(snapshot) {
      const selection=snapshot.selection;
      if(snapshot.phase==='error') {
        if(selection){months.delete(selection.month);days.delete(key(selection.storeId,selection.date));}
        return;
      }
      if(snapshot.index)put(months,snapshot.index.month,snapshot.index);
      if(snapshot.detailState==='error' && selection)days.delete(key(selection.storeId,selection.date));
      if(snapshot.detailState==='ready' && snapshot.detail)put(days,key(snapshot.detail.requested_store_id,snapshot.detail.local_date),snapshot.detail);
    },
    view(snapshot,{month,date,store}) {
      const pending=['loading','idle'].includes(snapshot.phase);
      const index=snapshot.index?.month===month?snapshot.index:pending?months.get(month)||null:null;
      const current=snapshot.detailState==='ready'?snapshot.detail:null;
      const detail=current?.requested_store_id===store && current.local_date===date?current:
        pending && index?.configured_store_ids.includes(store)?days.get(key(store,date))||null:null;
      return {...snapshot,index,detail,retainedDetail:!!detail && detail!==current};
    }
  };
}
export const timeText = value => value ? new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(new Date(value)) : '—';
export function directory(index) {
  return (index?.configured_store_ids || []).map(id => ({id,name:index.store_names[id],city:cityById[id] || '城市待核'}));
}
export function scopeStores(index, city = 'all', store = 'all') {
  return directory(index).filter(item => store !== 'all' ? item.id === store : city === 'all' || item.city === city);
}
export function calendarForScope(index, month, city = 'all', store = 'all') {
  if (!index) return null;
  const ids = scopeStores(index, city, store).map(item => item.id);
  if (!ids.length) return null;
  if (store !== 'all') return calendarCells(index, month, store);
  const filtered = {...index, configured_store_ids:ids,
    store_names:Object.fromEntries(ids.map(id => [id,index.store_names[id]])),
    unavailable_store_ids:index.unavailable_store_ids.filter(id => ids.includes(id)),
    days:Object.fromEntries(Object.entries(index.days).map(([date,rows]) => [date,Object.fromEntries(Object.entries(rows).filter(([id]) => ids.includes(id)))]))};
  if (index.calendar_index_state !== undefined) {
    filtered.calendar_pending_store_ids=index.calendar_pending_store_ids.filter(id => ids.includes(id));
    filtered.calendar_index_state=filtered.calendar_pending_store_ids.length ? 'preparing' : 'ready';
    filtered.calendar_verified_store_count=ids.length-filtered.calendar_pending_store_ids.length;
  }
  return calendarCells(filtered,month);
}
export function dayKind(date) {
  const known = Number(date.slice(0,4)) === calendar2026.year;
  if (known && calendar2026.makeup_workdays.some(row => row.date===date)) return {short:'班',name:'调休工作日',work:true};
  if (known && calendar2026.holidays.some(row => date>=row.start && date<=row.end)) return {short:'休',name:'法定节假日',work:false};
  const weekend = [0,6].includes(new Date(date+'T00:00:00Z').getUTCDay());
  return {short:'',name:(weekend?'周末':'工作日')+(known?'':'，节假日安排待核'),work:!weekend};
}
export function moveMonth(month, offset) {
  const date=new Date(Date.UTC(Number(month.slice(0,4)), Number(month.slice(5))-1+offset,1));
  return date.toISOString().slice(0,7);
}
export function queuePresentation(detail, queue, now=Date.now()) {
  const latest=latestQueue(detail,queue,{now});
  // Old numbers stay in the historical table; they cannot look current on top.
  const labels=['fresh','historical'].includes(latest.state) ? latest.labels : null;
  return {...latest,display:labels===null?'暂无可用号码':labels.length?labels.join(' · '):'本次未显示号码'};
}
export function recentRecords(detail) {
  return (detail?.points || []).slice(-60).reverse().map(point => ({
    at:point.queue_received_at,
    dine:point.queues===null?'读取失败':point.queues.mixedQueue.length?point.queues.mixedQueue.join(' · '):'未显示',
    reservation:point.queues===null?'读取失败':point.queues.reservationQueue.length?point.queues.reservationQueue.join(' · '):'未显示',
    state:point.scheduled_pause?'时段暂停':!point.queues?'号码失败':point.count_received_at===null||point.count_raw===null?'数量缺失':point.pair_ok?'已保存':'部分失败'
  }));
}
export function plots(detail) {
  const dine=chartSeries(detail,'mixedQueue'), reservation=chartSeries(detail,'reservationQueue');
  return {dine,reservation,quantity:dine.rawCount};
}
export function nearestPoint(points, at) {
  return points.reduce((closest,p) => !closest || Math.abs(p.at-at)<Math.abs(closest.at-at) ? p : closest,null);
}
