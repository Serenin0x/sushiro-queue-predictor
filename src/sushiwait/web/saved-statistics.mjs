/** One calendar for saved records. Day curves retain their original source. */
import {StatisticsError, createStatisticsClient, validateMonth, calendarCells, shanghaiDate} from './statistics-client.mjs';

export const SAVED_SOURCE = 'saved_statistics_collection';
const SOURCE_ORDER = ['sapi_miniapp_gateway', 'crm_remote_v1_1'];
const freeze = value => {if(value && typeof value === 'object'){Object.values(value).forEach(freeze);Object.freeze(value);}return value;};
const error = code => {throw new StatisticsError(code);};
const latest = values => values.filter(Boolean).sort((a,b)=>Date.parse(a)-Date.parse(b)).at(-1) ?? null;

export function mergeSavedMonths(months, month) {
  if(!Array.isArray(months) || months.length!==2 || new Set(months.map(v=>v?.source)).size!==2)error('saved_month_sources_required');
  const bySource={};
  for(const value of months){if(!SOURCE_ORDER.includes(value?.source))error('invalid_source');validateMonth(value,month,{source:value.source});bySource[value.source]=value;}
  const ids=[...new Set(SOURCE_ORDER.flatMap(source=>bySource[source].configured_store_ids))].sort((a,b)=>Number(a)-Number(b));
  if(ids.length>256)error('invalid_directory');
  const names=Object.fromEntries(ids.map(id=>[id,SOURCE_ORDER.map(source=>bySource[source].store_names[id]).find(Boolean)]));
  const days={};
  for(const source of SOURCE_ORDER)for(const [date,rows] of Object.entries(bySource[source].days)){
    for(const [id,row] of Object.entries(rows)){
      days[date]??={};
      days[date][id]??={store_id:id,local_date:date,source_summaries:{}};
      days[date][id].source_summaries[source]=row;
    }
  }
  for(const rows of Object.values(days))for(const item of Object.values(rows)){
    const values=Object.values(item.source_summaries);
    item.record_sources=SOURCE_ORDER.filter(source=>item.source_summaries[source]);
    for(const key of ['observations','successful_pairs','failed_pairs','scheduled_pause_slots','expected_background_slots_so_far','observed_background_slots']){
      item[key]=values.reduce((total,row)=>total+row[key],0);
      if(!Number.isSafeInteger(item[key]))error('invalid_summary');
    }
    // Two differently sampled streams do not constitute one coverage claim.
    item.observed_slot_fraction_so_far=values.length===1?values[0].observed_slot_fraction_so_far:null;
    item.last_observation_at=latest(values.map(row=>row.last_observation_at));
  }
  const unavailable=[...new Set(SOURCE_ORDER.flatMap(source=>bySource[source].unavailable_store_ids))];
  const pending=[...new Set(SOURCE_ORDER.flatMap(source=>bySource[source].calendar_pending_store_ids??[]))];
  return freeze({daily_schema_version:1,source:SAVED_SOURCE,month,configured_store_ids:ids,store_names:names,
    unavailable_store_ids:unavailable,calendar_pending_store_ids:pending,calendar_index_state:pending.length?'preparing':'ready',
    calendar_verified_store_count:ids.length-pending.length,days,source_months:bySource,
    generated_at:latest(months.map(value=>value.generated_at)),network_performed_by_read:false,eta_available:false,
    heatmap_semantics:'coverage_only_traffic_not_calibrated'});
}

export function savedSourceOptions(index, id, date) {
  if(index?.source!==SAVED_SOURCE || !index.configured_store_ids.includes(id) || !date.startsWith(index.month+'-'))error('saved_scope_mismatch');
  return SOURCE_ORDER.filter(source=>index.source_months[source].days[date]?.[id]?.observations>0);
}

export function selectSavedSource(index, id, date, requested=null, {today=shanghaiDate()}={}) {
  const options=savedSourceOptions(index,id,date);
  if(requested!==null){if(!options.includes(requested))error('saved_source_not_available');return requested;}
  const official=index.source_months.sapi_miniapp_gateway;
  // Today stays on the configured current chain, including empty or failed
  // responses. A failed day read never causes an alternate-source request.
  if(date>=today && official.configured_store_ids.includes(id))return 'sapi_miniapp_gateway';
  if(options.includes('crm_remote_v1_1'))return 'crm_remote_v1_1';
  if(options.length)return options[0];
  return SOURCE_ORDER.find(source=>index.source_months[source].configured_store_ids.includes(id));
}

export function createSavedStatisticsClient({fetchImpl=globalThis.fetch,timeoutMs=45000,clients=null}={}) {
  const readers=clients??Object.fromEntries(SOURCE_ORDER.map(source=>[source,createStatisticsClient({fetchImpl,timeoutMs,source})]));
  if(SOURCE_ORDER.some(source=>typeof readers[source]?.readMonth!=='function'||typeof readers[source]?.readDay!=='function'))error('invalid_client_options');
  return Object.freeze({
    async readMonth(month,{signal}={}){
      // Both inputs are mandatory; a broken current chain is never hidden
      // by returning a successfully read historical index instead.
      const values=await Promise.all(SOURCE_ORDER.map(source=>readers[source].readMonth(month,{signal})));
      return mergeSavedMonths(values,month);
    },
    async readDay(id,date,directory,{signal,index,source=null}={}){
      if(!directory.includes(id) || index?.month!==date.slice(0,7))error('saved_scope_mismatch');
      const selected=selectSavedSource(index,id,date,source);
      const value=await readers[selected].readDay(id,date,index.source_months[selected].configured_store_ids,{signal});
      if(value.source!==selected || value.requested_store_id!==id || value.local_date!==date)error('day_scope_mismatch');
      return value;
    },
    acceptsDetail(index,detail,requested=null){
      return index?.source===SAVED_SOURCE && SOURCE_ORDER.includes(detail?.source)
        && index.source_months[detail.source].configured_store_ids.includes(detail.requested_store_id)
        && detail.local_date.startsWith(index.month+'-') && (requested===null || requested===detail.source);
    }
  });
}

export function savedCalendarCells(index, month, ids) {
  if(index?.source!==SAVED_SOURCE || index.month!==month || ids.some(id=>!index.configured_store_ids.includes(id)))error('saved_scope_mismatch');
  const count=new Date(Date.UTC(Number(month.slice(0,4)),Number(month.slice(5)),0)).getUTCDate();
  const sourceCells=SOURCE_ORDER.map(source=>{
    const value=index.source_months[source],selected=ids.filter(id=>value.configured_store_ids.includes(id));
    if(!selected.length)return null;
    const filtered={...value,configured_store_ids:selected,store_names:Object.fromEntries(selected.map(id=>[id,value.store_names[id]])),
      unavailable_store_ids:value.unavailable_store_ids.filter(id=>selected.includes(id)),
      days:Object.fromEntries(Object.entries(value.days).map(([date,rows])=>[date,Object.fromEntries(Object.entries(rows).filter(([id])=>selected.includes(id)))]))};
    if(value.calendar_index_state!==undefined){filtered.calendar_pending_store_ids=value.calendar_pending_store_ids.filter(id=>selected.includes(id));filtered.calendar_index_state=filtered.calendar_pending_store_ids.length?'preparing':'ready';filtered.calendar_verified_store_count=selected.length-filtered.calendar_pending_store_ids.length;}
    return calendarCells(filtered,month,null,{source});
  });
  return Array.from({length:count},(_,i)=>{
    const date=month+'-'+String(i+1).padStart(2,'0'),rows=ids.map(id=>index.days[date]?.[id]).filter(Boolean);
    const cells=sourceCells.filter(Boolean).map(list=>list[i]),hasRows=cells.filter(cell=>cell.observations!==null);
    const pending=cells.some(cell=>cell.state==='pending'),unavailable=cells.some(cell=>cell.state==='unavailable');
    return freeze({date,weekdayMondayFirst:(new Date(date+'T00:00:00Z').getUTCDay()+6)%7,
      observations:rows.length?rows.reduce((total,row)=>total+row.observations,0):null,
      observedStoreCount:rows.length,configuredStoreCount:ids.length,
      coverage:hasRows.length===1?hasRows[0].coverage:null,
      state:pending?'pending':unavailable?'unavailable':rows.length?'observed':'no_data',
      coverageIsPartial:pending||unavailable||rows.length!==ids.length||hasRows.length>1,
      heatmapSemantics:'coverage_only_traffic_not_calibrated'});
  });
}
