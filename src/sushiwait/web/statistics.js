"use strict";
const $=id=>document.getElementById(id), dateFormat=new Intl.DateTimeFormat("en-CA",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit"}), timeFormat=new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const referenceRoot=$("sushiwait-reference");
const configuredReferenceSource=referenceRoot?.dataset?.statisticsSource??"crm_remote_v1_1";
const combinedHistory=referenceRoot?.dataset?.statisticsHistory==="combined";
let referenceSource=configuredReferenceSource,referenceApi=referenceSource==="sapi_miniapp_gateway"?"/api/v1/official":"/api/v1",recordSource=null;
let savedAdapterPromise=null,savedClient=null;
function savedAdapter(){return savedAdapterPromise??=(import('/saved-statistics.mjs').then(adapter=>{savedClient=adapter.createSavedStatisticsClient();return adapter;}));}
function setReferenceSource(source){if(!["crm_remote_v1_1","sapi_miniapp_gateway"].includes(source))throw Error("记录来源不匹配");referenceSource=source;referenceApi=source==="sapi_miniapp_gateway"?"/api/v1/official":"/api/v1";}

function today(){const parts=dateFormat.formatToParts(new Date()),v={};parts.forEach(p=>v[p.type]=p.value);return `${v.year}-${v.month}-${v.day}`;}
let index={days:{},store_names:{}},day=today(),detail=null,busy=false,detailLoading=false,detailRequestId=0,ticketScopeKey=null;
// The same mounted views keep private drafts and selection state when navigating.
const engineeringPanels=referenceRoot?.dataset?.engineeringPanels==="true";
const engineeringSections={overview:["观测总览","按日期查看已保存的观测与采集覆盖。"],analysis:["门店分析","查看号码位置、数量、展示变化与原始观测。"],prediction:["预测与记录",""],experience:["用餐记录","记录本人正常用餐的实际事件，保留本机私有草稿。"]};
engineeringSections.tickets=["我的号码",""];
engineeringSections.intelligence=["智慧分析",""];
let engineeringSection="overview",engineeringPollingSeconds=null,predictionWaitOpen=false;
const workspaceSectionScroll=new Map();
const taskWorkspace=engineeringPanels&&referenceRoot?.dataset?.engineeringLayout==="tasks-v2";
if(taskWorkspace){engineeringSections.overview=["门店监测",""];engineeringSections.analysis=["门店监测",""];}
// Saved city directory shared with the calendar; unknown future IDs stay explicitly unclassified.
const workspaceCityById={"1":"广州","2":"广州","3":"广州","4":"广州","5":"广州","6":"广州","7":"广州","8":"广州","9":"佛山","10":"广州","11":"佛山","12":"广州","13":"广州","15":"广州","16":"武汉","17":"东莞","18":"广州","19":"东莞","21":"武汉","23":"东莞","24":"武汉","25":"广州","26":"惠州","27":"广州","28":"广州","29":"广州","30":"广州","31":"珠海","32":"广州","34":"厦门","35":"江门","36":"珠海","37":"广州","38":"广州","39":"佛山","40":"广州","41":"武汉","42":"长沙","43":"珠海","44":"武汉","45":"广州","1001":"深圳","1002":"深圳","1003":"深圳","1004":"深圳","1005":"深圳","1006":"深圳","1007":"深圳","1008":"深圳","1009":"深圳","1010":"深圳","1011":"深圳","1012":"深圳","1013":"深圳","1014":"深圳","1015":"深圳","1016":"深圳","1017":"深圳","1018":"深圳","1019":"深圳","2001":"成都","2003":"成都","2004":"成都","2006":"重庆","2007":"重庆","2008":"成都","2009":"成都","2010":"西安","2011":"成都","3001":"天津","3002":"天津","3003":"天津","3004":"北京","3005":"北京","3006":"北京","3007":"天津","3008":"苏州","3009":"北京","3010":"苏州","3011":"杭州","3012":"北京","3013":"北京","3014":"北京","3015":"北京","3016":"北京","3017":"苏州","3018":"杭州","3019":"北京","3020":"杭州","3021":"杭州","3022":"宁波","3023":"北京","3024":"无锡","3025":"上海","3026":"上海","3027":"苏州","3028":"苏州","3029":"北京","3030":"北京","3031":"北京","3032":"上海","3033":"上海","3034":"上海","3035":"北京","3036":"上海","3037":"济南","3038":"北京","3039":"杭州","3040":"上海","3041":"上海","3042":"北京","3043":"金华","3044":"宁波","3045":"北京","3046":"宁波","3047":"绍兴","3048":"绍兴","3049":"杭州","3050":"北京","3051":"上海","3052":"宁波","3053":"北京","3054":"南京","3055":"南京","3056":"绍兴","3057":"嘉兴","3058":"杭州","3059":"温州","3060":"北京","3061":"金华","3062":"南通","3063":"嘉兴","3064":"青岛","3065":"南京","3066":"上海","3067":"石家庄","3068":"杭州","3069":"青岛","3070":"上海","3071":"合肥","3072":"上海","3073":"上海","3074":"北京","3076":"温州","3078":"天津","3079":"镇江","3080":"济南"};
const workspaceState={city:"all",from:"00:00",to:"23:59",range:"all",page:0,selected:null,chartPositions:[],chartScope:null,lastPointKey:null,readError:false,restoring:false,routeStore:null,overviewPage:0,charts:new Map(),hover:new Map(),seenScope:null,seenKeys:new Set(),newKeys:new Map()};
let workspaceHealth=null,workspaceLastBoundaryCheck=0;
let workspaceSessionAuto=taskWorkspace&&!new URLSearchParams(window.location.search).has('date');
let workspaceInitialSection="overview";
if(taskWorkspace){const route=workspaceRouteSelection();day=route.date;workspaceInitialSection=route.section;workspaceState.routeStore=route.store;workspaceState.city=route.store?(workspaceCityById[route.store]||"城市待核"):route.city;recordSource=route.source;$("queue").value=route.queue;}

function syncEngineeringSelection(){
  if(!engineeringPanels)return;
  $("engineering-date").value=day;
  const metricRows=summaries(day),times=metricRows.map(row=>Date.parse(row.last_observation_at)).filter(Number.isFinite);
  $("metric-observations").textContent=metricRows.length?`${metricRows.reduce((total,row)=>total+(taskWorkspace?row.successful_pairs:row.observations),0)} 组`:"无已保存观测";
  $("metric-last").textContent=times.length?time(Math.max(...times)):"—";
  const current=day===today(),running=workspaceHealth?.state==='running'&&!workspaceHealth.heartbeat_stale;
  $('metric-stores-label').textContent=current&&running?'正在采样门店':'本轮采样门店';
  const active=current&&running?workspaceHealth?.active_store_count:null;
  $("metric-stores").textContent=`${Number.isInteger(active)?active:metricRows.length} 家`;
  if(taskWorkspace)workspaceRenderHealth();
  $("metric-interval").textContent=engineeringPollingSeconds===null?"读取中":engineeringPollingSeconds%60===0?`${engineeringPollingSeconds/60} 分钟`:`${engineeringPollingSeconds} 秒`;
  const store=$("store").value;
  $("analysis-empty").hidden=Boolean(store);
  $("prediction-empty").hidden=true;
  $("prediction-content").hidden=false;
  $("store-wait-card").hidden=!store||(!predictionWaitOpen&&Boolean($("ticket-composer-form")?.dataset?.entryWorkflow));
  $("engineering-selection").textContent=`${day}，${store?(index.store_names[store]||store):taskWorkspace&&workspaceState.city!=="all"?workspaceState.city+"，全部门店":"全部门店"}，${$("queue").selectedOptions?.[0]?.textContent||"展示队列"}`;
}
function selectEngineeringSection(section){
  if(taskWorkspace&&section==='quality')section=$('store').value?'analysis':'overview';
  if(!engineeringPanels||!Object.hasOwn(engineeringSections,section))return;
  const changed=section!==engineeringSection;
  if(taskWorkspace&&changed)workspaceSectionScroll.set(engineeringSection,window.scrollY||0);
  engineeringSection=section;
  $("engineering-breadcrumb").textContent=engineeringSections[section][0];
  $(["overview","analysis","quality"].includes(section)?"nav-group-observations":"nav-group-records").open=true;
  for(const name of Object.keys(engineeringSections)){
    const active=name===section;
    $("view-"+name).hidden=!active;
    if(taskWorkspace)$("view-"+name).classList.toggle("view-entering",active&&changed);
    const nav=$("nav-"+name);if(nav){nav.classList.toggle("nav-current",active);if(active)nav.setAttribute("aria-current","page");else nav.removeAttribute("aria-current");}
  }
  [$("engineering-title").textContent,$("engineering-description").textContent]=engineeringSections[section];
  syncEngineeringSelection();
  if(taskWorkspace){
    const hideScope=["tickets","intelligence"].includes(section)||(section==="prediction"&&!predictionWaitOpen&&Boolean($("ticket-composer-form")?.dataset?.entryWorkflow));
    $("workspace-filters").hidden=hideScope;$("workspace-meta").hidden=hideScope;$("engineering-selection").hidden=hideScope;$("archive-status").hidden=hideScope;
    $("filter-queue").hidden=section==="overview"||section==="experience";
    $("filter-date").hidden=["experience","tickets"].includes(section);$("date-shortcuts").hidden=["experience","tickets"].includes(section);
    $("record-source-label").hidden=section==="overview"||section==="experience"||!(detail&&index.days[day]?.[$("store").value]?.record_sources?.length>1);
    workspaceWriteRoute({push:true});
    if(document.body.classList.contains("nav-open"))workspaceNav(false);
    const monitoring=['overview','analysis'].includes(section);
    $('monitor-date').hidden=hideScope||section==='experience';$('monitor-metrics').hidden=section!=='overview';$('monitor-search').hidden=section!=='overview';
    $('nav-overview').classList.toggle('nav-current',monitoring);if(monitoring)$('nav-overview').setAttribute('aria-current','page');
    $('workspace-meta').hidden=monitoring;$('engineering-title').hidden=section==='analysis';

  }
  if(section==="prediction"&&taskWorkspace&&detail)loadHistory($("store").value,day);
  window.SushiWaitResearch?.notify();
  window.SushiWaitAnalytics?.notify();
  if(section==="analysis")graphs();
  if(taskWorkspace&&changed&&typeof window.requestAnimationFrame==='function')window.requestAnimationFrame(()=>{
    if(engineeringSection===section)window.scrollTo({top:workspaceSectionScroll.get(section)||0,behavior:'instant'});
  });
}
// Task-based engineering workspace. All data reads use the public archive.
function workspaceDateValid(value){return /^20\d{2}-\d{2}-\d{2}$/.test(value)&&Number.isFinite(Date.parse(value+'T00:00:00Z'))&&new Date(value+'T00:00:00Z').toISOString().slice(0,10)===value;}
function workspacePointTime(p){return p.queue_received_at||p.count_received_at||p.request_started_at||p.slot_started_at||null;}
function workspacePointKey(p,i){if(p.observation_id||p.record_sha256)return `${detail?.source||referenceSource}|${p.observation_id||p.record_sha256}`;return `${detail?.source||referenceSource}|${p.queue_received_at||''}|${p.count_received_at||''}|${p.request_started_at||p.slot_started_at||i}`;}
function workspacePointState(p,queue){if(p.scheduled_pause)return 'paused';if(!p.pair_ok)return 'failed';if(!p.queues||!Array.isArray(p.queues[queue]))return 'missing';return p.queues[queue].length?'saved':'empty';}
function workspaceStateLabel(state){return {saved:'已保存',empty:'已保存，无展示号',failed:'失败或部分成功',paused:'营业边界暂停',missing:'已保存，字段未显示'}[state]||'未知';}
function workspaceComparisonLabel(value){return {comparable_display_sets:'可比较展示集合',packet_continuity_unknown:'采集连续性未确认',first_observation:'首个观测',no_previous_observation:'无前序观测',scheduled_pause:'营业边界',run_boundary:'采集批次边界',gap:'间隔边界',insufficient:'观测不足',time_order_or_duplicate:'时间顺序或重复边界',queue_type_changed:'队列变化',number_reset:'号码重置',failed_pair:'失败边界',gap_too_large:'长间隔边界',source_changed:'来源变化'}[value]||value||'未确认';}
function workspaceSourceLabel(value){return value==='sapi_miniapp_gateway'?'官方查询记录':value==='crm_remote_v1_1'?'已保存历史记录':'来源未确认';}
function workspaceRows(){return (detail?.points||[]).map((p,i)=>({p,i,key:workspacePointKey(p,i),at:Date.parse(workspacePointTime(p))}));}
function workspaceScope(){return `${day}|${$('store').value}|${$('queue').value}|${detail?.source||referenceSource}`;}
function workspaceFilteredRows({search=true}={}){
 const start=Date.parse(`${day}T${workspaceState.from}:00+08:00`),end=Date.parse(`${day}T${workspaceState.to}:59.999+08:00`),query=search?$('records-search').value.trim():'';
 return workspaceRows().filter(v=>Number.isFinite(v.at)&&v.at>=start&&v.at<=end&&(!query||(v.p.queues?.[$('queue').value]||[]).some(n=>n.includes(query))));
}
function workspaceWriteRoute({push=false}={}){
 if(!taskWorkspace||workspaceState.restoring)return;
 const params=new URLSearchParams({section:engineeringSection});if(!workspaceSessionAuto)params.set('date',day);
 const routeStore=workspaceState.routeStore??$('store').value;
 if(workspaceState.city!=='all')params.set('city',workspaceState.city);
 if(routeStore)params.set('store',routeStore);
 if($('queue').value!=='mixedQueue')params.set('queue',$('queue').value);
 if(recordSource)params.set('source',recordSource);
 const next=window.location.pathname+'?'+params.toString();
 if(next!==window.location.pathname+window.location.search)window.history[push?'pushState':'replaceState']({},'',next);
}
function workspaceRouteSelection(){
 const p=new URLSearchParams(window.location.search),date=p.get('date'),section=p.get('section'),queue=p.get('queue'),store=p.get('store'),source=p.get('source');
 return {city:Object.values(workspaceCityById).includes(p.get('city'))||p.get('city')==='城市待核'?p.get('city'):'all',date:workspaceDateValid(date||'')?date:today(),section:section==='quality'?(store?'analysis':'overview'):Object.hasOwn(engineeringSections,section||'')?section:'overview',store:store&&/^[1-9]\d{0,9}$/.test(store)?store:'',queue:['mixedQueue','reservationQueue'].includes(queue)?queue:'mixedQueue',source:['sapi_miniapp_gateway','crm_remote_v1_1'].includes(source)?source:null};
}
function workspaceStoreCity(id){return workspaceCityById[id]||'城市待核';}
function workspacePopulatePickers(selected=''){
 const cities=[...new Set(Object.keys(index.store_names).map(workspaceStoreCity))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
 if(selected&&Object.hasOwn(index.store_names,selected))workspaceState.city=workspaceStoreCity(selected);
 if(workspaceState.city!=='all'&&!cities.includes(workspaceState.city))workspaceState.city='all';
 $('city').replaceChildren();
 for(const city of ['all',...cities]){const o=document.createElement('option');o.value=city;o.textContent=city==='all'?'全部城市':city;$('city').append(o);}
 $('city').value=workspaceState.city;$('city').disabled=!cities.length;
 $('store').replaceChildren();const all=document.createElement('option');all.value='';all.textContent=workspaceState.city==='all'?'先选择城市':'全部门店';$('store').append(all);
 for(const [id,name] of Object.entries(index.store_names).filter(([id])=>workspaceStoreCity(id)===workspaceState.city).sort((a,b)=>a[1].localeCompare(b[1],'zh-CN'))){const o=document.createElement('option');o.value=id;o.textContent=name;$('store').append(o);}
 $('store').disabled=workspaceState.city==='all';$('store').value=selected&&workspaceStoreCity(selected)===workspaceState.city?selected:'';
}
function workspaceSetDate(date,{push=true}={}){
 if(!workspaceDateValid(date))return;workspaceSessionAuto=false;if(date===day){workspaceWriteRoute({push});return;}
 const changedMonth=date.slice(0,7)!==$('month').value;day=date;recordSource=null;workspaceState.page=0;workspaceState.overviewPage=0;resetTicketScope();workspaceWriteRoute({push});
 if(changedMonth){$('month').value=date.slice(0,7);detail=null;++detailRequestId;refresh();}
 else showDay().catch(showError);
}
function workspaceSelectStore(id){
 if(engineeringSection==='overview')workspaceState.overviewReturn={city:workspaceState.city,page:workspaceState.overviewPage};
 workspacePopulatePickers(id);recordSource=null;workspaceState.page=0;resetTicketScope();selectEngineeringSection('analysis');workspaceWriteRoute({push:true});showDay().catch(showError);
}
function workspaceBackOverview(){
 workspaceState.routeStore=null;workspaceState.city=workspaceState.overviewReturn?.city??workspaceState.city;workspaceState.overviewPage=workspaceState.overviewReturn?.page??workspaceState.overviewPage;
 workspacePopulatePickers();detail=null;++detailRequestId;selectEngineeringSection('overview');showDay().catch(showError);
}
function workspaceRenderOverview(){
 if(!taskWorkspace)return;
 const all=index.days[day]||{},query=$('overview-search').value.trim().toLocaleLowerCase();
 const configured=index.source_months?.sapi_miniapp_gateway?.configured_store_ids||index.configured_store_ids||Object.keys(index.store_names);
 let ids=(Object.keys(all).length?Object.keys(all):configured).filter(id=>(workspaceState.city==='all'||workspaceStoreCity(id)===workspaceState.city)&&(index.store_names[id]||id).toLocaleLowerCase().includes(query));
 ids.sort((a,b)=>(index.store_names[a]||a).localeCompare(index.store_names[b]||b,'zh-CN'));
 const pages=Math.max(1,Math.ceil(ids.length/18));workspaceState.overviewPage=Math.min(workspaceState.overviewPage,pages-1);
 const visible=ids.slice(workspaceState.overviewPage*18,(workspaceState.overviewPage+1)*18);
 const signature=JSON.stringify([day,visible,visible.map(id=>all[id]),workspaceHealth?.state,workspaceHealth?.unavailable_store_ids,workspaceState.readError]);
 if(workspaceState.overviewSignature!==signature){
  const focused=document.activeElement?.dataset?.storeId;$('rows').replaceChildren();
  for(const id of visible){
   const row=all[id],problem=workspaceHealth?.unavailable_store_ids?.includes(id),button=document.createElement('button');button.type='button';button.className='monitor-store-row';button.dataset.storeId=id;
   const title=document.createElement('span'),name=document.createElement('strong'),city=document.createElement('span');title.className='monitor-store-name';name.textContent=index.store_names[id]||id;city.textContent=workspaceStoreCity(id);title.append(name,city);
   const total=document.createElement('span');total.className='monitor-store-total';total.textContent=row?`${row.observations} 条`:'—';
   const received=document.createElement('time');received.textContent=time(row?.last_observation_at);
   const state=document.createElement('span');state.className='monitor-store-state';state.dataset.state=problem||workspaceState.readError?'error':row?'saved':'none';state.textContent=problem?'采样异常':workspaceState.readError?'读取异常':!row?'无已保存观测':day!==today()?'已归档':workspaceHealth?.state==='running'?'采集中':row.failed_pairs>0?'含失败记录':'已保存';
   const arrow=document.createElement('span');arrow.className='monitor-row-arrow';arrow.textContent='→';arrow.setAttribute('aria-hidden','true');button.append(title,total,received,state,arrow);button.onclick=()=>workspaceSelectStore(id);$('rows').append(button);if(focused===id)button.focus();
  }
  workspaceState.overviewSignature=signature;
 }
 $('overview-count').textContent=`${ids.length} 家`;$('overview-empty').hidden=ids.length>0;
 $('overview-page-note').textContent=`${workspaceState.overviewPage+1} / ${pages}`;$('overview-previous').disabled=workspaceState.overviewPage===0;$('overview-next').disabled=workspaceState.overviewPage>=pages-1;
 $('date-title').textContent=day;$('summary').textContent=Object.keys(all).length?'':'无已保存观测';
}
function workspaceEnsureSeries(preferred=1){
 // Keep at least one position visible, including restored browser form state.
 if([1,2,3].some(i=>$('series-'+i).checked))return false;
 $('series-'+preferred).checked=true;return true;
}
function workspaceDrawChart(id,series,{interactive=false,emptyText='当前范围没有可绘制的有效观测'}={}){
 const canvas=$(id),rect=canvas.getBoundingClientRect(),dpr=Math.min(window.devicePixelRatio||1,3),width=Math.max(180,rect.width),height=interactive?(rect.height||300):220;
 canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);
 const c=canvas.getContext('2d');c.scale(dpr,dpr);c.clearRect(0,0,width,height);
 const pad={left:57,right:20,top:36,bottom:38},w=width-pad.left-pad.right,h=height-pad.top-pad.bottom,valid=series.filter(v=>Number.isFinite(v.at)&&Number.isFinite(v.value));
 const bounds={start:Date.parse(`${day}T${workspaceState.from}:00+08:00`),end:Date.parse(`${day}T${workspaceState.to}:59.999+08:00`)};
 const earliest=Math.min(...valid.map(v=>v.at)),latest=Math.max(...valid.map(v=>v.at));
 // A single point keeps a real time axis; no invented neighbouring observation.
 const minX=valid.length&&workspaceState.range==='all'?Math.max(bounds.start,earliest-60000):bounds.start,maxX=valid.length&&workspaceState.range==='all'?Math.min(bounds.end,latest+60000):bounds.end;
 const low=valid.length?Math.min(...valid.map(v=>v.value)):0,high=valid.length?Math.max(...valid.map(v=>v.value)):1,margin=Math.max((high-low)*.08,1);
 const rough=(high-low+2*margin)/4,power=10**Math.floor(Math.log10(rough)),normalized=rough/power,step=power*(normalized<=1?1:normalized<=2?2:normalized<=5?5:10);
 const minY=Math.max(0,Math.floor((low-margin)/step)*step),maxY=Math.ceil((high+margin)/step)*step;
 const x=v=>pad.left+w*(v-minX)/Math.max(maxX-minX,1),y=v=>pad.top+h-h*(v-minY)/Math.max(maxY-minY,1);
 c.font='11px system-ui';c.textBaseline='middle';c.lineWidth=1;c.textAlign='left';c.fillStyle='#718094';c.fillText(workspaceAxisLabel(id),pad.left,13);
 for(let value=minY;value<=maxY+step/100;value+=step){const yy=y(value);c.strokeStyle='#edf0f4';c.beginPath();c.moveTo(pad.left,yy);c.lineTo(width-pad.right,yy);c.stroke();c.fillStyle='#718094';c.textAlign='right';c.fillText(Number(value.toFixed(2)).toLocaleString('en-US'),pad.left-9,yy);}
 const divisions=width<450?2:4;c.setLineDash([2,5]);
 for(let i=0;i<=divisions;i++){const xx=pad.left+w*i/divisions;c.strokeStyle='#f1f3f6';c.beginPath();c.moveTo(xx,pad.top);c.lineTo(xx,pad.top+h);c.stroke();c.fillStyle='#718094';c.textAlign=i===0?'left':i===divisions?'right':'center';c.fillText(time(minX+(maxX-minX)*i/divisions).slice(0,5),xx,height-14);}
 c.setLineDash([]);
 if(!valid.length){c.textAlign='left';c.fillStyle='#8c96a3';c.fillText(emptyText,pad.left+12,pad.top+25);}
 const colors=['#b4212b','#d09c54','#61878b'],positions=[];
 const joined=new Set();let before=new Map();
 for(const item of valid){const prior=before.get(item.series||0);if(item.connect&&prior&&item.i===prior.i+1&&(id!=='numbers'||item.value>=prior.value)){joined.add(prior);joined.add(item);}before.set(item.series||0,item);}
 let previous=new Map();
 for(const item of valid){const px=x(item.at),py=y(item.value),prior=previous.get(item.series||0);c.strokeStyle=colors[item.series||0];c.fillStyle=colors[item.series||0];
  if(item.connect&&prior&&item.i===prior.i+1&&(id!=='numbers'||item.value>=prior.value)){c.globalAlpha=item.guide?.55:.72;c.lineWidth=1.8;c.setLineDash(item.guide?[4,3]:[]);c.beginPath();c.moveTo(x(prior.at),y(prior.value));c.lineTo(px,py);c.stroke();c.setLineDash([]);c.globalAlpha=1;}
  if(!joined.has(item)||workspaceState.selected===item.key){c.beginPath();c.arc(px,py,workspaceState.selected===item.key?4.8:2.5,0,Math.PI*2);c.fill();}
  if(workspaceState.selected===item.key){c.strokeStyle='#fff';c.lineWidth=2;c.stroke();c.strokeStyle=colors[item.series||0];c.globalAlpha=.2;c.lineWidth=5;c.beginPath();c.arc(px,py,7,0,Math.PI*2);c.stroke();c.globalAlpha=1;}
  positions.push({...item,x:px,y:py});previous.set(item.series||0,item);
 }
 workspaceState.charts.set(id,{positions,width,height,pad,series,interactive,emptyText});
 const hover=workspaceState.hover.get(id),hovered=positions.find(v=>v.key===hover?.key&&v.series===hover?.series);
 if(hovered){c.strokeStyle='#8a97a7';c.lineWidth=1;c.setLineDash([3,4]);c.beginPath();c.moveTo(hovered.x,pad.top);c.lineTo(hovered.x,pad.top+h);c.moveTo(pad.left,hovered.y);c.lineTo(width-pad.right,hovered.y);c.stroke();c.setLineDash([]);c.fillStyle='#b4212b';c.beginPath();c.arc(hovered.x,hovered.y,5,0,Math.PI*2);c.fill();c.strokeStyle='#fff';c.lineWidth=2;c.stroke();}
 if(interactive){workspaceState.chartPositions=positions;workspaceState.chartWidth=width;workspaceState.chartHeight=height;
  const selected=positions.find(v=>v.key===workspaceState.selected);
  if(selected){c.strokeStyle='#b4212b';c.globalAlpha=.35;c.lineWidth=1;c.setLineDash([3,4]);c.beginPath();c.moveTo(selected.x,pad.top);c.lineTo(selected.x,pad.top+h);c.stroke();c.setLineDash([]);c.globalAlpha=1;}
 }
}
function workspaceVisualLink(current,previous){
 // A visual guide between adjacent archived samples is not analytic continuity.
 if(current?.comparison_state!=='gap'||!current.pair_ok||!previous?.pair_ok||current.scheduled_pause||previous.scheduled_pause)return false;
 const link=current.continuity,prior=previous.continuity,elapsed=Date.parse(workspacePointTime(current))-Date.parse(workspacePointTime(previous));
 return !!(link&&prior&&link.writer_id===prior.writer_id&&link.predecessor_id===previous.observation_id&&link.predecessor_sha256===previous.record_sha256&&elapsed>0&&elapsed<=660000);
}
function workspaceGraphSeries(rows){
 const queue=$('queue').value,series=[],counts=[],speed=[];
 for(const {p,i,key,at} of rows){
  const labels=p.queues?.[queue]||[];
  if(p.pair_ok&&!p.scheduled_pause)labels.slice(0,3).forEach((label,position)=>{if($('series-'+(position+1)).checked&&/^\d+$/.test(label))series.push({at,value:Number(label),series:position,i,key,connect:p.comparison_state==='comparable_display_sets'||workspaceVisualLink(p,detail.points[i-1]),guide:p.comparison_state!=='comparable_display_sets'});});
  if(p.pair_ok&&p.count_raw!==null&&Number.isFinite(p.count_raw))counts.push({at:Date.parse(p.count_received_at||workspacePointTime(p)),value:p.count_raw,i,key,series:0,connect:p.comparison_state==='comparable_display_sets'||workspaceVisualLink(p,detail.points[i-1]),guide:p.comparison_state!=='comparable_display_sets'});
  const previous=detail.points[i-1],before=previous?.queues?.[queue]?.[0],current=labels[0],dt=p.interval_seconds;
  if(p.pair_ok&&previous?.pair_ok&&p.comparison_state==='comparable_display_sets'&&dt>0&&/^\d+$/.test(before||'')&&/^\d+$/.test(current||'')&&Number(current)>=Number(before))speed.push({at,value:(Number(current)-Number(before))*60/dt,i,key,series:0,connect:true});
 }
 return {series,counts,speed};
}
function workspaceClearDetail(loading){
 $('snapshot-reservation').replaceChildren();$('snapshot-total').textContent='—';workspaceState.charts.clear();workspaceState.hover.clear();
 $('snapshot-store').textContent=index.store_names[$('store').value]||$('store').value;
 $('snapshot-time').textContent=loading?'正在读取所选门店的归档…':'当天没有已保存的观测';$('snapshot-numbers').replaceChildren();$('snapshot-state').textContent=loading?'读取中':'无观测';
 $('snapshot-note').textContent='';$('chart-text-summary').textContent='';$('selected-record-text').textContent='读取完成后可选择观测。';$('selected-record-open').disabled=true;$('records-export').disabled=true;$('quality-strip').replaceChildren();$('records-page-note').textContent='—';$('chart-tooltip').hidden=true;workspaceState.chartPositions=[];

}
function workspaceQueueNumbers(id,labels){
 const target=$(id);if(target.children.length!==3){target.replaceChildren();for(let i=0;i<3;i++){const value=document.createElement('strong');target.append(value);}}
 for(let i=0;i<3;i++)target.children[i].textContent=labels?.[i]??'—';
}
function workspaceSnapshot(){
 const last=workspaceRows().at(-1),p=last?.p,age=p?Date.now()-Date.parse(p.queue_received_at):NaN;
 const fresh=day===today()&&!workspaceState.readError&&!detailLoading&&p?.pair_ok===true&&!p.scheduled_pause&&age>=-5000&&age<=(Math.max(90,(engineeringPollingSeconds||600)+120))*1000;
 const live=fresh&&!['closed','completed','needs_attention'].includes(workspaceHealth?.state)&&!workspaceHealth?.unavailable_store_ids?.includes($('store').value);
 $('snapshot-store').textContent=index.store_names[$('store').value]||$('store').value;
 $('snapshot-time').textContent=p?time(workspacePointTime(p)):'—';$('snapshot-total').textContent=detail?String(detail.summary?.observations??detail.points?.length??0):'—';
 for(const [id,queue] of [['snapshot-numbers','mixedQueue'],['snapshot-reservation','reservationQueue']])workspaceQueueNumbers(id,live&&Array.isArray(p?.queues?.[queue])&&p.queue_field_presence?.[queue]!=='missing'?p.queues[queue]:null);
 const badge=$('snapshot-state');badge.textContent=workspaceState.readError?'读取异常':day!==today()?'已归档':!p?'等待观测':!p.pair_ok?'采样异常':workspaceHealth?.state==='closed'?'已结束':live?'采集中':'等待更新';
 badge.dataset.tone=workspaceState.readError||p?.pair_ok===false?'error':live?'good':'neutral';$('snapshot-note').textContent='';
}
function workspaceRenderHealth(){
 if(!taskWorkspace)return;
 const labels={running:'采集中',starting:'启动中',closed:'营业外',completed:'已结束',needs_attention:'采样异常'},state=workspaceHealth?.state;
 $('monitor-status').dataset.state=state||'unknown';$('monitor-status-label').textContent=labels[state]||'状态未知';
 const reasons={heartbeat_stale:'采集心跳超时',collection_not_started:'开采时间已到，尚未收到观测',collection_progress_stale:'采样进度未按计划更新',public_sync_stopped:'网页同步或统计汇总异常',cloud_rate_limited:'上游请求限流',cloud_refresh_failed:'授权更新失败',cloud_trial_halted:'共享授权保护已暂停采集'};
 $('monitor-status-reason').textContent=reasons[workspaceHealth?.reason_code]||(workspaceHealth?.next_open_at?`下次开采 ${new Date(workspaceHealth.next_open_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}`:state==='running'?'采集运行中':state==='completed'?'本轮采集已结束':'暂时无法确认采集状态');
}
function workspaceClock(){
 if(!taskWorkspace||document.visibilityState==='hidden')return;$('monitor-clock').textContent=time(Date.now());
 if(workspaceSessionAuto&&workspaceHealth?.display_switch_at&&Date.now()>=Date.parse(workspaceHealth.display_switch_at)&&Date.now()-workspaceLastBoundaryCheck>=15000){workspaceLastBoundaryCheck=Date.now();refresh();}
 for(const [key,expires] of workspaceState.newKeys)if(expires<=Date.now()){workspaceState.rowNodes?.get(key)?.classList.remove('observation-new');workspaceState.newKeys.delete(key);}
 if(detail&&engineeringSection==='analysis')workspaceSnapshot();
}
function workspaceSelectRecord(key,{open=false,scroll=false}={}){
 const item=workspaceRows().find(v=>v.key===key);if(!item)return;
 const returnFocus=document.activeElement;workspaceState.selected=key;
 const sorted=workspaceOrderedRows(),position=sorted.findIndex(v=>v.key===key);if(position>=0)workspaceState.page=Math.floor(position/Number($('records-size').value));workspaceState.pageAnchor=null;
 workspaceRenderGraphs({table:true});
 if(scroll)document.getElementById('points')?.querySelector('[data-selected="true"]')?.scrollIntoView({block:'nearest',behavior:'auto'});
 if(open)workspaceOpenRecord(item,{returnFocus});
}
function workspaceOpenRecord(item,{returnFocus=document.activeElement}={}){
 if(!item)return;workspaceState.dialogReturnFocus=returnFocus;const {p,at}=item,queue=$('queue').value,labels=p.queues?.[queue];
 $('record-dialog-title').textContent=`${index.store_names[$('store').value]||$('store').value}，${time(at)}`;$('record-details').replaceChildren();
 const fields=[['日期',day],['记录来源',workspaceSourceLabel(detail.source)],['请求开始',p.request_started_at?time(p.request_started_at):'未记录'],['队列响应',time(p.queue_received_at)],['数量响应',time(p.count_received_at)],['保存状态',workspaceStateLabel(workspacePointState(p,queue))],['展示队列',$('queue').selectedOptions?.[0]?.textContent||queue],['原号码顺序',Array.isArray(labels)?labels.length?labels.join('、'):'成功响应但没有展示号':'字段未显示'],['原始数量',p.count_raw===null||p.count_raw===undefined?'未显示':`${p.count_raw}（单位未核实）`],['相邻间隔',p.interval_seconds===null||p.interval_seconds===undefined?'未确认':`${p.interval_seconds} 秒`],['比较状态',workspaceComparisonLabel(p.comparison_state)],['失败类别',Object.values(p.error_codes||{}).join('、')||'无已记录失败类别'],['成功语义',p.success_semantics==='one_official_detail_response_not_two_query_pair'?'一次官方门店详情响应，不是两次查询的成对记录':p.pair_ok?'原来源记录的完整观测':'请根据原保存状态核对']];
 for(const [name,value] of fields){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=name;dd.textContent=String(value);$('record-details').append(dt,dd);}
 if(!$('record-dialog').open)$('record-dialog').showModal();
}
function workspaceOrderedRows(){const rows=workspaceFilteredRows();return rows.sort((a,b)=>$('records-sort').value==='asc'?a.at-b.at:b.at-a.at);}
function workspaceRenderTable(){
 const rows=workspaceOrderedRows(),size=Number($('records-size').value)||20,pages=Math.max(1,Math.ceil(rows.length/size)),scope=workspaceScope();workspaceState.page=Math.min(workspaceState.page,pages-1);
 const same=workspaceState.seenScope===scope,keys=new Set(workspaceRows().map(v=>v.key)),now=Date.now();
 if(same){for(const key of keys)if(!workspaceState.seenKeys.has(key))workspaceState.newKeys.set(key,now+2400);}else{workspaceState.newKeys.clear();workspaceState.pageAnchor=null;}
 workspaceState.seenScope=scope;workspaceState.seenKeys=keys;
 let start=workspaceState.page*size;
 if(same&&workspaceState.page>0&&workspaceState.pageAnchor){const anchored=rows.findIndex(v=>v.key===workspaceState.pageAnchor);if(anchored>=0)start=anchored;}
 const visible=rows.slice(start,start+size);workspaceState.pageAnchor=workspaceState.page>0?visible[0]?.key:null;
 const signature=JSON.stringify([scope,workspaceState.page,size,visible.map(v=>[v.key,v.p]),workspaceState.selected]);
 if(workspaceState.tableSignature!==signature){
  const old=workspaceState.rowNodes||new Map(),next=new Map(),elements=[];
  for(const item of visible){
   const p=item.p,labels=p.queues?.[$('queue').value],state=workspacePointState(p,$('queue').value);let tr=old.get(item.key);
   const content=JSON.stringify(p);
   if(!tr||tr.dataset.record!==content){
    tr=document.createElement('tr');for(const value of [time(workspacePointTime(p)),Array.isArray(labels)?labels.join('、')||'无展示号':'字段未显示',p.count_raw===null||p.count_raw===undefined?'—':p.count_raw,workspaceStateLabel(state),'']){const cell=document.createElement('td');cell.textContent=String(value);tr.append(cell);}
    tr.dataset.record=content;tr.dataset.observationKey=item.key;
    const locate=document.createElement('button');locate.type='button';locate.className='table-button';locate.textContent='定位';locate.setAttribute('aria-label',`定位 ${time(item.at)} 的观测`);locate.onclick=()=>workspaceSelectRecord(item.key);
    const inspect=document.createElement('button');inspect.type='button';inspect.className='table-button';inspect.textContent='详情';inspect.setAttribute('aria-label',`查看 ${time(item.at)} 的观测详情`);inspect.onclick=()=>workspaceSelectRecord(item.key,{open:true});tr.children[4].append(locate,inspect);
    tr.addEventListener('animationend',()=>{tr.classList.remove('observation-new');workspaceState.newKeys.delete(item.key);});
   }
   tr.dataset.selected=String(item.key===workspaceState.selected);tr.classList.toggle('selected-row',item.key===workspaceState.selected);
   const expires=workspaceState.newKeys.get(item.key)||0;tr.classList.toggle('observation-new',expires>now);if(expires>now)tr.style.animationDelay=`-${(2400-expires+now)/1000}s`;
   next.set(item.key,tr);elements.push(tr);
  }
  $('points').replaceChildren(...elements);workspaceState.rowNodes=next;workspaceState.tableSignature=signature;
 }
 $('records-empty').hidden=rows.length>0;$('records-previous').disabled=workspaceState.page===0;$('records-next').disabled=start+size>=rows.length;
 $('records-page-note').textContent=`${rows.length} 条观测，第 ${workspaceState.page+1} / ${pages} 页`;$('records-export').disabled=!rows.length;
}
function workspaceRenderGraphs({table=true}={}){
 if(!taskWorkspace||!detail||detail.requested_store_id!==$('store').value||detail.local_date!==day)return;
 workspaceEnsureSeries();
 const scope=workspaceScope();
 if(workspaceState.chartScope!==scope){workspaceState.chartScope=scope;workspaceState.selected=null;workspaceState.page=0;workspaceState.range='all';workspaceState.from='00:00';workspaceState.to='23:59';workspaceState.lastPointKey=null;}
 const rows=workspaceFilteredRows({search:false}),data=workspaceGraphSeries(rows),latest=workspaceRows().at(-1)?.key;
 const emptyNumbers=rows.length?'所选号码位置没有可绘制的展示号':'当天没有已保存的观测';
 workspaceState.lastPointKey=latest;workspaceSnapshot();workspaceDrawChart('numbers',data.series,{interactive:true,emptyText:emptyNumbers});
 workspaceDrawChart('count',data.counts,{emptyText:rows.length?'当前观测没有有效的原始数量':'当天没有已保存的观测'});workspaceDrawChart('speed',data.speed,{emptyText:rows.length?'尚无连续可比观测，无法计算推进速度':'当天没有已保存的观测'});
 $('chart-text-summary').textContent=data.series.length?`当前范围 ${rows.length} 组观测，绘制 ${data.series.length} 个原始展示号点。点选可定位记录；左右方向键选择，Enter查看详情。`:emptyNumbers+'。';
 const selected=workspaceRows().find(v=>v.key===workspaceState.selected);
 $('selected-record-open').disabled=!selected;$('selected-record-text').textContent=selected?`${time(selected.at)}，${(selected.p.queues?.[$('queue').value]||[]).join('、')||workspaceStateLabel(workspacePointState(selected.p,$('queue').value))}，${workspaceStateLabel(workspacePointState(selected.p,$('queue').value))}`:'选择一个采样点查看记录。';
 $('quality-strip').replaceChildren();
 // Bounded DOM for dense historical curves; the table still exposes all projected rows.
 for(const item of rows.slice(-240)){const b=document.createElement('button');b.type='button';b.dataset.state=workspacePointState(item.p,$('queue').value);b.dataset.selected=String(item.key===workspaceState.selected);b.title=`${time(item.at)}，${workspaceStateLabel(b.dataset.state)}`;b.setAttribute('aria-label',b.title);b.onclick=()=>workspaceSelectRecord(item.key);$('quality-strip').append(b);}
 $('graph-note').textContent=`${rows.length>240?'状态条展示最近240条。':''}${detail.graph_truncated?'当前投影已截断；完整记录仍保存在数据库。':''}`;
 if(table)workspaceRenderTable();
}
function workspaceAxisLabel(id){
 if(id==='numbers')return '展示号码（号）';if(id==='speed')return '参考号码 / 分钟';
 const unit=detail?.summary?.count_unit;return '原始数量（'+({tables:'桌',table:'桌',people:'人',persons:'人',groups:'组'}[unit]||'单位待核实')+'）';
}
function workspaceNearest(event,id='numbers'){
 const plot=workspaceState.charts.get(id);if(!plot)return null;
 const rect=$(id).getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
 if(x<plot.pad.left||x>plot.width-20||y<plot.pad.top||y>plot.height-38)return null;
 return plot.positions.reduce((best,v)=>{const dx=Math.abs(x-v.x),dy=Math.abs(y-v.y);return !best||dx<best.dx||dx===best.dx&&dy<best.dy?{...v,dx,dy}:best;},null);
}
function workspaceHover(event,id){
 const plot=workspaceState.charts.get(id);if(!plot)return;
 const near=workspaceNearest(event,id),tip=$(id==='numbers'?'chart-tooltip':'chart-tooltip-'+id);
 if(!near){workspaceState.hover.delete(id);tip.hidden=true;workspaceDrawChart(id,plot.series,{interactive:plot.interactive,emptyText:plot.emptyText});return;}
 workspaceState.hover.set(id,{key:near.key,series:near.series});workspaceDrawChart(id,plot.series,{interactive:plot.interactive,emptyText:plot.emptyText});
 const item=workspaceRows().find(v=>v.key===near.key);tip.hidden=false;tip.textContent=`${day} ${time(near.at)}\n${id==='numbers'?(item?.p.queues?.[$('queue').value]||[]).join('、'):near.value.toLocaleString('zh-CN',{maximumFractionDigits:2})+' '+workspaceAxisLabel(id)}\n${workspaceStateLabel(workspacePointState(item?.p||{},$('queue').value))}`;
 tip.style.left=Math.min(Math.max(near.x+12,8),Math.max(8,plot.width-195))+'px';tip.style.top=Math.max(8,near.y-65)+'px';
}
function workspaceCSV(){
 const rows=workspaceOrderedRows(),quote=v=>'"'+String(v??'').replace(/^[=+@\-\t\r]/,"'$&").replaceAll('"','""')+'"';
 const lines=[['门店编号','门店名称','日期','记录来源','请求开始时间','队列响应时间','队列','第1位原展示号','第2位原展示号','第3位原展示号','原始数量（单位未核实）','保存状态','比较状态'].map(quote).join(',')];
 for(const {p} of rows){const labels=p.queues?.[$('queue').value]||[];lines.push([$('store').value,index.store_names[$('store').value]||'',day,detail.source,p.request_started_at||'',p.queue_received_at||'',$('queue').value,labels[0]??'',labels[1]??'',labels[2]??'',p.count_raw??'',workspaceStateLabel(workspacePointState(p,$('queue').value)),p.comparison_state||''].map(quote).join(','));}
 return '\ufeff'+lines.join('\r\n')+'\r\n';
}
function workspaceExport(){
 const url=URL.createObjectURL(new Blob([workspaceCSV()],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download=`sushiwait-public-${$('store').value}-${day}-${$('queue').value}.csv`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);$('workspace-announcement').textContent='已请求下载当前筛选的公共采样记录，未包含个人输入。';
}
function workspaceNav(open){
 const mobile=window.matchMedia('(max-width: 840px)').matches;
 if(mobile){$('workspace-sidebar').inert=!open;document.body.classList.toggle('nav-open',open);$('nav-scrim').hidden=!open;$('nav-toggle').setAttribute('aria-expanded',String(open));if(open)$('nav-close').focus();else $('nav-toggle').focus();}
 else{document.body.classList.toggle('nav-collapsed',!open);$('nav-toggle').setAttribute('aria-expanded',String(open));}
}
function workspaceInit(){
 if(window.matchMedia('(max-width: 840px)').matches){$('workspace-sidebar').inert=true;$('nav-toggle').setAttribute('aria-expanded','false');}
 $('overview-search').oninput=()=>{workspaceState.overviewPage=0;workspaceRenderOverview();};
 $('overview-previous').onclick=()=>{workspaceState.overviewPage=Math.max(0,workspaceState.overviewPage-1);workspaceRenderOverview();};$('overview-next').onclick=()=>{workspaceState.overviewPage++;workspaceRenderOverview();};
 $('monitor-back').onclick=workspaceBackOverview;$('nav-overview').onclick=workspaceBackOverview;
 $('monitor-status').onclick=()=>{const open=$('monitor-status-reason').hidden;$('monitor-status-reason').hidden=!open;$('monitor-status').setAttribute('aria-expanded',String(open));};
 workspaceClock();setInterval(workspaceClock,1000);
 for(const [id,amount] of [['previous-day',-1],['next-day',1]])$(id).onclick=()=>{const value=new Date(day+'T00:00:00Z');value.setUTCDate(value.getUTCDate()+amount);workspaceSetDate(value.toISOString().slice(0,10));};$('today-day').onclick=()=>workspaceSetDate(today());
 workspaceEnsureSeries();
 for(let i=1;i<=3;i++)$('series-'+i).onchange=()=>{if(workspaceEnsureSeries(i))$('workspace-announcement').textContent='至少保留一条号码曲线';graphs();};
 for(const id of ['records-search','records-sort','records-size'])$(id)[id==='records-search'?'oninput':'onchange']=()=>{workspaceState.page=0;workspaceState.pageAnchor=null;workspaceRenderTable();};
 $('records-previous').onclick=()=>{workspaceState.page=Math.max(0,workspaceState.page-1);workspaceState.pageAnchor=null;workspaceRenderTable();};$('records-next').onclick=()=>{workspaceState.page++;workspaceState.pageAnchor=null;workspaceRenderTable();};$('records-export').onclick=workspaceExport;

 $('analysis-choose').onclick=$('prediction-choose').onclick=()=>{$(workspaceState.city==='all'?'city':'store').focus();};
 $('city').onchange=()=>{workspaceState.overviewPage=0;workspaceState.city=$('city').value;workspaceState.routeStore=null;workspacePopulatePickers();recordSource=null;resetTicketScope();if(engineeringSection==='analysis')selectEngineeringSection('overview');calendar();workspaceWriteRoute({push:true});showDay().catch(showError);};
 $('selected-record-open').onclick=()=>workspaceOpenRecord(workspaceRows().find(v=>v.key===workspaceState.selected));$('record-dialog-close').onclick=()=>$('record-dialog').close();
 $('record-dialog').addEventListener('close',()=>{if(workspaceState.dialogReturnFocus?.isConnected)workspaceState.dialogReturnFocus.focus();else if(engineeringSection==='analysis')$('selected-record-open').focus();else $('overview-search').focus();});
 $('record-dialog').addEventListener('click',event=>{if(event.target===$('record-dialog')){const r=$('record-dialog').getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)$('record-dialog').close();}});
 for(const id of ['numbers','count','speed']){
  $(id).addEventListener('pointermove',event=>workspaceHover(event,id));
  $(id).addEventListener('pointerleave',()=>{const plot=workspaceState.charts.get(id);workspaceState.hover.delete(id);$(id==='numbers'?'chart-tooltip':'chart-tooltip-'+id).hidden=true;if(plot)workspaceDrawChart(id,plot.series,{interactive:plot.interactive,emptyText:plot.emptyText});});
  $(id).addEventListener('click',event=>{const near=workspaceNearest(event,id);if(near)workspaceSelectRecord(near.key);});
 }
 $('numbers').addEventListener('keydown',event=>{const rows=workspaceFilteredRows({search:false});if(!rows.length)return;if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();const current=rows.findIndex(v=>v.key===workspaceState.selected),position=current<0?(event.key==='ArrowLeft'?rows.length-1:0):Math.min(rows.length-1,Math.max(0,current+(event.key==='ArrowRight'?1:-1)));workspaceSelectRecord(rows[position].key);}else if(event.key==='Enter'&&workspaceState.selected){event.preventDefault();workspaceOpenRecord(workspaceRows().find(v=>v.key===workspaceState.selected));}});

 $('nav-toggle').onclick=()=>workspaceNav(window.matchMedia('(max-width: 840px)').matches?!document.body.classList.contains('nav-open'):document.body.classList.contains('nav-collapsed'));
 $('nav-close').onclick=$('nav-scrim').onclick=()=>workspaceNav(false);
 document.addEventListener('keydown',event=>{if(document.body.classList.contains('nav-open')){if(event.key==='Escape'){event.preventDefault();workspaceNav(false);}else if(event.key==='Tab'){const buttons=[...$('workspace-sidebar').querySelectorAll('a,button,summary')].filter(v=>v.getClientRects().length),first=buttons[0],last=buttons.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}}}});
 window.addEventListener('popstate',()=>{workspaceSessionAuto=!new URLSearchParams(window.location.search).has('date');const route=workspaceRouteSelection();workspaceState.restoring=true;const changedMonth=route.date.slice(0,7)!==$('month').value;day=route.date;$('month').value=day.slice(0,7);workspaceState.city=route.city;workspacePopulatePickers(route.store);$('queue').value=route.queue;recordSource=route.source;resetTicketScope();selectEngineeringSection(route.section);workspaceState.restoring=false;if(changedMonth){workspaceState.routeStore=route.store;refresh();}else showDay().catch(showError);});
 // The active child stays reachable when its navigation group is toggled.
 for(const id of ['nav-group-observations','nav-group-records'])$(id).addEventListener('toggle',()=>{const activeGroup=['overview','analysis','quality'].includes(engineeringSection)?'nav-group-observations':'nav-group-records';if(id===activeGroup&&!$(id).open)$(id).open=true;});
 window.addEventListener('resize',()=>{if(!window.matchMedia('(max-width: 840px)').matches){document.body.classList.remove('nav-open');$('nav-scrim').hidden=true;$('workspace-sidebar').inert=false;$('nav-toggle').setAttribute('aria-expanded',String(!document.body.classList.contains('nav-collapsed')));}else if(!document.body.classList.contains('nav-open')){$('workspace-sidebar').inert=true;$('nav-toggle').setAttribute('aria-expanded','false');}});
}
async function workspaceUpdatePolling(){
 try{const value=await get('/api/v1/statistics/settings');if(value.schema_version===1&&Number.isInteger(value.poll_seconds)&&value.poll_seconds>=30&&value.poll_seconds<=3600&&value.poll_seconds*1000!==pollingMs){clearInterval(pollingTimer);pollingMs=value.poll_seconds*1000;engineeringPollingSeconds=value.poll_seconds;pollingTimer=setInterval(refresh,pollingMs);syncEngineeringSelection();}}catch(_){/* Keep the last valid public polling setting. */}
}

const historyCache=new Map();
const referenceAlerts=window.SushiWaitReferenceAlerts?.createTracker();
const precautionMessages={number_displayed:"你的原号码出现在最新展示列表中，请立即核对官方号单并准备到场。",previously_displayed_now_absent:"你的号码之前出现过，当前已不在展示列表；请立即核对官方号单，这里不能确认已叫号或过号。",reference_at_or_beyond_ticket:"首位参考已到达或越过你的号码，请立即核对官方号单。",possible_before_arrival:"较早的参考情景早于你计划到店的时间，请核对号单并考虑提前到场。",reference_acceleration:"最近参考突然加速，请核对号单并留意提前到场。",reference_moved_earlier:"新观测把参考时间提前了至少五分钟，请重新安排到场时间。",source_stale:"数据已超过90秒，风险判断和预测暂时暂停。",ticket_cycle_uncertain:"参考发生倒退或轮次不确定，请核对官方号单，暂不判断是否过号。",invalid_private_plan:"请核对取号时间、可选到店时间及整数偏移；到店时间不能早于取号时间。",reference_tracking:"正在根据新观测更新参考，请继续以官方号单为准。",model_unavailable:"最新展示可读，但预测资料尚不足；请关注官方号单。"};
function renderPrecaution(options,result){
  const element=$("ticket-precaution");
  if(!referenceAlerts){element.hidden=true;return;}
  const state=referenceAlerts.update(detail,options,result);
  element.hidden=state.state==="idle";
  const text=state.state==="loading"?"正在读取新观测，当前风险判断暂停。":precautionMessages[state.reason]||(state.state==="data_unavailable"?"当前资料不可用，风险判断暂时暂停；请核对官方号单。":"请核对官方号单。");
  const next=`${text}${state.sample_at?` 依据响应 ${time(state.sample_at)}。`:""}`;
  // Stable text avoids repeating the same live announcement every age tick.
  if(element.textContent!==next)element.textContent=next;
  element.dataset.state=state.state;
  return state;
}
async function get(path){if(!["crm_remote_v1_1","sapi_miniapp_gateway"].includes(referenceSource))throw Error("数据来源未配置");const r=await fetch(path,{cache:"no-store",signal:AbortSignal.timeout(15000)});if(!r.ok){const e=Error("读取失败");e.httpStatus=r.status;throw e;}return r.json();}
function textRow(values,parent){const tr=document.createElement("tr");values.forEach(v=>{const td=document.createElement("td");td.textContent=v;tr.append(td);});parent.append(tr);return tr;}
function time(v){return v?timeFormat.format(new Date(v)):"—";}
function estimateTime(v){return new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(v));}
function summaries(d){const all=index.days[d]||{},store=$("store").value;return store?(all[store]?[all[store]]:[]):Object.values(all).filter(row=>!taskWorkspace||workspaceState.city==="all"||workspaceStoreCity(row.store_id)===workspaceState.city);}
function calendarPending(){const pending=index.calendar_pending_store_ids||[],store=$("store").value;return store?pending.includes(store):pending.length>0;}
function coverage(items){if(items.some(s=>s.source_summaries&&Object.keys(s.source_summaries).length>1))return null;const expected=items.reduce((n,s)=>n+s.expected_background_slots_so_far,0),seen=items.reduce((n,s)=>n+s.observed_background_slots,0);return expected?Math.min(1,seen/expected):null;}
function calendar(){const month=$("month").value;if(!/^\d{4}-\d{2}$/.test(month))return;const [year,m]=month.split("-").map(Number),n=new Date(Date.UTC(year,m,0)).getUTCDate(),start=(new Date(Date.UTC(year,m-1,1)).getUTCDay()+6)%7;$("calendar").replaceChildren();for(let i=0;i<start;i++)$("calendar").append(document.createElement("span"));for(let i=1;i<=n;i++){const d=`${month}-${String(i).padStart(2,"0")}`,items=summaries(d),c=coverage(items),b=document.createElement("button"),label=document.createElement("small");b.type="button";b.textContent=String(i);label.textContent=items.length?`${items.reduce((s,x)=>s+x.observations,0)} 组观测`:calendarPending()?"统计加载中":"无观测";b.append(label);if(items.length)b.classList.add(c!==null&&c>=.8&&(!$("store").value?items.length===Object.keys(index.store_names).length:true)?"covered":"partial");if(d===day)b.classList.add("selected");b.setAttribute("aria-label",`${d}，${label.textContent}`);b.onclick=()=>{day=d;recordSource=null;resetTicketScope();calendar();showDay().catch(showError);};$("calendar").append(b);}}
function scopeKey(){return `${referenceSource}|${day}|${$("store").value}|${$("queue").value}`;}
function resetTicketScope(){ticketScopeKey=null;$("ticket-scope").checked=false;renderPrediction();}
function showError(){if(taskWorkspace){workspaceState.readError=true;detailLoading=false;++detailRequestId;$("status").textContent="归档读取失败，保留上次已保存资料及原响应时间；风险判断暂停。";$("workspace-read-at").textContent="读取失败，已保留资料";if(detail)workspaceSnapshot();renderPrediction();return;}detail=null;detailLoading=false;++detailRequestId;$("status").textContent="统计服务暂时不可用；没有把读取失败当成零数据。";renderPrediction();}
function priorDates(value,store,date){return Object.keys(value?.days||{}).filter(d=>{const row=value.days[d]?.[store],saved=row?.source_summaries?row.source_summaries[referenceSource]:row;return /^\d{4}-\d{2}-\d{2}$/.test(d)&&d<date&&Date.parse(date)-Date.parse(d)<=31*86400000&&saved?.observations>0;}).sort().reverse();}
function loadHistory(store,date){
  if(date!==today())return;
  const key=`${referenceSource}|${store}|${date}`,known=priorDates(index,store,date),signature=known.join(",");
  const old=historyCache.get(key);
  if(old&&old.signature===signature)return;
  const entry={signature,state:"loading",days:[],failed:0};historyCache.set(key,entry);
  while(historyCache.size>3)historyCache.delete(historyCache.keys().next().value);
  // Only read archived public curves; no ticket number, issue time or travel plan is sent.
  (async()=>{
    let dates=known;
    const weekday=new Date(`${date}T00:00:00Z`).getUTCDay(),sameWeekday=d=>new Date(`${d}T00:00:00Z`).getUTCDay()===weekday;
    if(dates.length<7||dates.filter(sameWeekday).length<2){
      const previous=new Date(`${date.slice(0,7)}-01T00:00:00Z`);previous.setUTCDate(0);
      try{const past=await get(`${referenceApi}/months/${previous.toISOString().slice(0,7)}`);dates=[...new Set([...dates,...priorDates(past,store,date)])].sort().reverse();if(past.calendar_pending_store_ids?.includes(store))entry.failed++;}
      catch(_){entry.failed++;}
    }
    // Retain repeated weekdays for date effects, then fill with recent days; choose before reading outcomes.
    dates=[...dates.filter(sameWeekday).slice(0,4),...dates.filter(d=>!sameWeekday(d))].slice(0,7);
    for(const archivedDate of dates){
      if(historyCache.get(key)!==entry)return;
      try{entry.days.push(await get(`${referenceApi}/stores/${encodeURIComponent(store)}/days/${archivedDate}`));}
      catch(_){entry.failed++;}
    }
    entry.state=entry.failed?"partial":"ready";
    if(historyCache.get(key)===entry&&day===date&&$("store").value===store)renderPrediction();
  })();
}
const predictionReasons={beyond_supported_horizon:"当前趋势推算需超过 6 小时，暂不能给出可靠时间。",ticket_precedes_issue:"号码与取号前的展示冲突，请核对官方票据与采集数据。",insufficient_sparse_trend:"正在积累历史与实时趋势，暂时无法给出时间。",awaiting_arrival_evidence:"显示号码已接近，等待完整的到号判定证据。",median_upper_unknown:"历史记录的较晚端还不确定，等待新观测。",unsupported_queue:"仅支持堂食或预约队列；请切换展示队列。",today_only:"实验预测只用于今天的号码；历史日期可查看统计曲线。",scope_mismatch:"门店或数据语义不匹配，暂不计算。",invalid_projection:"观测格式或数量超出支持范围。",confirm_ticket_scope:"请确认这是今天在所选门店与队列取得的号码。",numeric_ticket_required:"请输入最多七位的纯数字号码，保留原有前导零。",issue_time_required:"请填写今天实际取号的时间，不能使用未来时间。",future_or_invalid_clock:"观测时间或本机时钟异常，暂不计算。",awaiting_post_issue_sample:"等待取号之后的新观测。",ticket_cycle_uncertain:"参考曾倒退或重置，无法确认这个号码属于当前轮次。",reference_at_or_beyond_ticket:"参考已到达或越过这个号码，请核对官方号单；这里不能确认已叫号或过号。",no_current_sample:"最新观测失败、暂停或没有有效号码，暂不计算。",stale_sample:"数据已超过当前采样周期，暂停预测并等待新观测。",sampling_density_unsupported:"近期采样密度超出实验支持范围。",insufficient_positive_trend:"需要至少六个有效观测点、五分钟的连续推进；低频采样暂不足以计算精细等待。",sampling_continuity_unavailable:"这些观测之间的采集连续性尚未确认，暂不连接成等待时间趋势。",forecast_horizon_unsupported:"推进过慢或预测已超出有效时限，暂不计算。"};
let latestReferenceResult=null;
const trackingStorageKey='sushiwait.ticket-tracking.v1';
let publicAdvice=null,publicAdviceReading=false,publicAdviceScope=null;
function privateTrackingScope(){return {source:referenceSource,date:day,store:$('store').value,queue:$('queue').value};}
function restoreTrackingInputs(){
 if($('ticket-number').value)return;
 try{const raw=window.localStorage?.getItem(trackingStorageKey);if(!raw||raw.length>4096)return;const v=JSON.parse(raw),scope=privateTrackingScope();
  if(v.schema_version!==1||v.date!==today()||Object.keys(scope).some(k=>v[k]!==scope[k])||!/^\d{1,7}$/.test(v.number)||Number(v.number)<1||!/^\d{2}:\d{2}$/.test(v.issued)||!['unknown','yes','no'].includes(v.checkin))return;
  $('ticket-number').value=v.number;$('ticket-issued').value=v.issued;$('ticket-checkin').value=v.checkin;$('ticket-scope').checked=true;ticketScopeKey=scopeKey();
 }catch(_){/* Storage may be unavailable; the current page still tracks. */}
}
function saveTrackingInputs(options){
 if(!options.confirmedScope||!/^\d{1,7}$/.test(options.ticket)||Number(options.ticket)<1||!options.issuedAt||Date.parse(options.issuedAt)>options.now||day!==today())return;
 try{window.localStorage?.setItem(trackingStorageKey,JSON.stringify({schema_version:1,...privateTrackingScope(),number:options.ticket,issued:$('ticket-issued').value,checkin:$('ticket-checkin').value}));}catch(_){}
}
function loadPublicAdvice(){
 const key=JSON.stringify([referenceSource,day,$('store').value,detail?.points?.at(-1)?.queue_received_at,Math.floor(Date.now()/30000)]);
 if(publicAdviceReading||key===publicAdviceScope)return;publicAdviceScope=key;publicAdviceReading=true;
 get('/model-advice.json').then(v=>{if(v?.schema_version===1&&v.policy==='public_display_scenarios_v1')publicAdvice=v;else publicAdvice=null;})
  .catch(()=>{publicAdvice=null;}).finally(()=>{publicAdviceReading=false;if(publicAdviceScope===key)renderPrediction();});
}
function selectedAdvice(options){
 if($('prediction-mode').value==='algorithm')return null;
 const models=publicAdvice?.stores?.[options.storeId]?.[options.queueType];if(!models)return null;
 const mode=$('prediction-mode').value;return mode==='auto'?(models.nonthinking?.advice||models.thinking?.advice||null):models[mode]?.advice||null;
}
function renderTrackingPrecaution(options,result,arrived){
 const el=$('ticket-precaution');let message='';
 if(arrived?.available)message=arrived.state==='arrived'?'号码已经进入前两位，请准备就餐。':'展示的三个号码均已越过你的号码，按约定判定已到号。';
 else if(arrived?.state==='upcoming')message='号码已进入第3位，即将到号。';
 else if(result?.acceleration_signal)message='号码推进突然加快，预计时间可能提前。';
 else if(result?.available&&options.desiredArrivalAt&&Date.parse(result.scenario_earliest)<Date.parse(options.desiredArrivalAt))message='较早情景早于计划到店时间，建议提前到场。';
 el.hidden=!message;if(el.textContent!==message)el.textContent=message;el.dataset.state=message?'attention':'tracking';
}
function renderPrediction(){
  restoreTrackingInputs();
  latestReferenceResult=null;
  if(window.SushiWaitResearch?.workflow){window.SushiWaitResearch.notify();return;}
  window.SushiWaitResearch?.notify();
  if(taskWorkspace&&workspaceState.readError){$("prediction").textContent="归档读取失败，参考预测与风险判断已暂停。";$("prediction-note").textContent="保留的快照只供查阅，不能当作新响应。";$("ticket-precaution").hidden=true;return;}
  const issued=$("ticket-issued").value,ms=/^\d{2}:\d{2}$/.test(issued)?Date.parse(`${day}T${issued}:00+08:00`):NaN;
  const arrival=$("ticket-arrival").value,arrivalAt=/^\d{2}:\d{2}$/.test(arrival)?`${day}T${arrival}:00+08:00`:arrival?"invalid":null;
  const offset=$("ticket-offset").value;
  const options={source:referenceSource,storeId:$("store").value,queueType:{mixedQueue:"ordinary",reservationQueue:"reservation"}[$("queue").value],ticket:$("ticket-number").value,issuedAt:Number.isFinite(ms)?new Date(ms).toISOString():null,now:Date.now(),confirmedScope:$("ticket-scope").checked&&ticketScopeKey===scopeKey(),desiredArrivalAt:arrivalAt,callOffsetMinutes:/^-?\d{1,3}$/.test(offset)?Number(offset):NaN,readPending:detailLoading,samplingSeconds:engineeringPollingSeconds,checkedIn:$('ticket-checkin').value==='unknown'?null:$('ticket-checkin').value==='yes'};
  saveTrackingInputs(options);$('ticket-tracking-state').textContent=options.confirmedScope&&options.issuedAt?'自动跟踪中':'尚未开始跟踪';$('prediction-ai').textContent='';
  $("prediction-note").textContent="";
  if(!$("ticket-number").value){loadPublicAdvice();renderPrecaution(options);$("prediction").textContent="输入号码、取号时间并确认门店与队列后计算。";return;}
  if(!detail){renderPrecaution(options);$("prediction").textContent="正在等待所选门店的新观测；旧结果已暂停。";return;}
  if(!window.SushiWaitReference){renderPrecaution(options);$("prediction").textContent="实验计算模块未加载，暂不计算。";return;}
  const entry=historyCache.get(`${referenceSource}|${$("store").value}|${day}`),compute=window.SushiWaitReference.trackedForecast||window.SushiWaitReference.forecastWithHistory||window.SushiWaitReference.forecast;
  const arrived=window.SushiWaitReference.arrival?.(detail,options);
  if(arrived?.available){renderTrackingPrecaution(options,null,arrived);$('ticket-tracking-state').textContent=arrived.state==='arrived'?'已到号':'推定已到号';
   $('prediction').textContent=`${arrived.state==='arrived'?'号码进入前两位':'三位展示号码已全部越过'}，首次检测 ${estimateTime(arrived.detected_at)}。`;
   $('prediction-note').textContent=`本次等待约 ${arrived.wait_lower_minutes.toFixed(1)}—${arrived.wait_upper_minutes.toFixed(1)} 分钟。`;return;}
  loadPublicAdvice();const result=compute(detail,options,entry?.days||[],selectedAdvice(options));latestReferenceResult=result;
  renderTrackingPrecaution(options,result,arrived);
  $('prediction-ai').textContent=result.ai_participated?`DeepSeek Flash ${result.ai_mode==='thinking'?'思考':'非思考'}已参与本轮计算`:$('prediction-mode').value==='algorithm'?'使用历史与实时算法':publicAdvice?.status==='budget_exhausted'?'本轮模型额度已用完，继续使用算法':'Flash 等待可用的新观测结果，当前使用算法';
  const historyNote=!(result.available||result.history)?"":entry?.state==="loading"?"历史曲线正在加载，现有实时参考先显示。":result.history?.state==="available"?`参考 ${result.history.selected_days} 天的 ${result.history.samples} 段历史；${result.history.cohort==="date_type_all_hours"?"相近时段不足，已采用同日期类型的其他时段。":""}${result.history.right_censored+result.history.boundary_censored?"未观察到目标到达的片段也保留在范围中。":""}`:entry?.failed?"部分历史读取失败，未补成零等待；可手动刷新重试。":"历史样本尚不足，暂以连续实时趋势为依据。";
  if(!result.available){$("prediction").textContent=result.reason==="truncated_current_projection"?"当天投影有截断，暂不计算历史融合。":predictionReasons[result.reason]||"实验资料不足，暂不计算。";$("prediction-note").textContent=historyNote;return;}
  const stage=result.mode==="history_initial"?"历史初估，":result.mode==="history_live_mix"?"历史＋实时修正，":"";
  $("prediction").textContent=`${stage}预计 ${estimateTime(result.reference_time)} 到号，范围 ${estimateTime(result.scenario_earliest)}—${result.scenario_latest?estimateTime(result.scenario_latest):"较晚端尚不确定"}。`;
  $("prediction-note").textContent=`最新参考 ${result.reference}；位置差 ${result.position_difference}（不是前方桌数）。依据连续 ${result.continuous_minutes.toFixed(1)} 分钟观测，距最近响应 ${Math.round(result.sample_age_seconds)} 秒。${historyNote}情景范围不是已校准的预测误差。${result.acceleration_signal?"最近一段突然加速，已加入更早到达的情景，请及时核对官方号单。":""}${result.latest_interval_stalled?"最新一段参考未推进，历史速度可能滞后。":""}`;
}
async function showDay({quiet=false}={}){
  const requestId=++detailRequestId,selectedDay=day,store=$("store").value,items=summaries(day);
  $("date-title").textContent=`${day}，${store?(index.store_names[store]||store):taskWorkspace&&workspaceState.city!=="all"?workspaceState.city+"，全部门店":"全部门店"}`;
  if(taskWorkspace)workspaceRenderOverview();
  else {$("rows").replaceChildren();
  items.forEach(s=>{const c=s.observed_slot_fraction_so_far,tr=textRow([index.store_names[s.store_id]||s.store_id,s.successful_pairs,`${s.failed_pairs} / ${s.scheduled_pause_slots}`,c===null?"—":`${(c*100).toFixed(1)}%`,time(s.last_observation_at)],$("rows"));tr.onclick=()=>{$("store").value=s.store_id;recordSource=null;resetTicketScope();calendar();selectEngineeringSection("analysis");showDay().catch(showError);};});
  $("summary").textContent=items.length?`有观测的门店 ${items.length} 家，完整观测 ${items.reduce((n,s)=>n+s.successful_pairs,0)} 组。实际叫号总量和人流量仍未核实。`:calendarPending()?"后台正在校验本月归档；当前未加载的数据不记为零。":"当天没有已保存的观测；不能据此判断门店没有客流。";
  }
  $("detail").hidden=!store;syncEngineeringSelection();
  if(taskWorkspace)workspaceWriteRoute();
  let adapter=null,selectedSource=referenceSource;
  if(combinedHistory&&store){
    adapter=await savedAdapter();
    if(requestId!==detailRequestId)return;
    selectedSource=adapter.selectSavedSource(index,store,selectedDay,recordSource);
    const choices=adapter.savedSourceOptions(index,store,selectedDay),picker=$("record-source");
    picker.replaceChildren();
    for(const source of choices){const option=document.createElement("option");option.value=source;option.textContent=source==="sapi_miniapp_gateway"?"当前采集记录":"已保存记录";picker.append(option);}
    picker.value=selectedSource;$("record-source-label").hidden=choices.length<2;
    $("record-source-note").textContent=choices.length>1?"这一天保存了两组记录，曲线分别查看；实验模型只使用同一来源的历史。":"";
  }else if($("record-source-label")){$("record-source-label").hidden=true;$("record-source-note").textContent="";}
  const retain=quiet&&detail?.requested_store_id===store&&detail.local_date===selectedDay&&detail.source===selectedSource;
  if(!retain){detail=null;detailLoading=Boolean(store);if(taskWorkspace){workspaceState.readError=false;workspaceState.page=0;workspaceClearDetail(Boolean(store));}renderPrediction();$("points").replaceChildren();["numbers","count","speed"].forEach(id=>plot(id,[]));$("graph-note").textContent="正在读取当天观测…";}
  if(!store)return;
  if(referenceSource!==selectedSource){setReferenceSource(selectedSource);historyCache.clear();resetTicketScope();}
  let value;
  try{value=combinedHistory?await savedClient.readDay(store,selectedDay,index.configured_store_ids,{index,source:recordSource}):await get(`${referenceApi}/stores/${encodeURIComponent(store)}/days/${selectedDay}`);}
  catch(error){if(requestId!==detailRequestId||day!==selectedDay||$("store").value!==store)return;throw error;}
  if(requestId!==detailRequestId||day!==selectedDay||$("store").value!==store)return;
  if(value.source!==selectedSource||value.requested_store_id!==store||value.local_date!==selectedDay)throw Error("门店来源不匹配");
  detail=value;detailLoading=false;if(taskWorkspace)workspaceState.readError=false;graphs();renderPrediction();if(!taskWorkspace||engineeringSection==="prediction")loadHistory(store,selectedDay);
}
function plot(id,points){const canvas=$(id),rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;canvas.width=Math.max(300,Math.round(rect.width*dpr));canvas.height=230*dpr;const c=canvas.getContext("2d");c.scale(dpr,dpr);const w=canvas.width/dpr,h=230;c.clearRect(0,0,w,h);c.strokeStyle="#d6dce1";c.beginPath();c.moveTo(60,12);c.lineTo(60,h-32);c.lineTo(w-12,h-32);c.stroke();c.fillStyle="#58636c";c.font="12px system-ui";if(!points.length){c.fillText("没有可绘制的有效观测",75,40);return;}const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys),dx=Math.max(1,maxX-minX),dy=Math.max(1,maxY-minY);for(let i=0;i<4;i++){const y=minY+dy*i/3;c.fillText(Number(y.toFixed(2)).toString(),4,h-34-(h-50)*i/3);}c.fillText(time(minX),60,h-9);c.fillText(time(maxX),Math.max(60,w-80),h-9);points.forEach(([x,y,series])=>{c.fillStyle=["#b4212b","#bb7441","#638b4f"][series||0];c.beginPath();c.arc(60+(w-80)*(x-minX)/dx,h-34-(h-50)*(y-minY)/dy,2.4,0,Math.PI*2);c.fill();});}
function graphs(){if(taskWorkspace){workspaceRenderGraphs();return;}if(!detail)return;const queue=$("queue").value,points=detail.points||[],numbers=[],counts=[],speed=[];points.forEach(p=>{(p.queues?.[queue]||[]).slice(0,1).forEach(label=>{if(/^\d+$/.test(label))numbers.push([Date.parse(p.queue_received_at),Number(label),0]);});if(p.count_raw!==null)counts.push([Date.parse(p.count_received_at),p.count_raw,0]);if(p.comparison_state==="comparable_display_sets"&&p.interval_seconds>0&&p.removed_labels)speed.push([Date.parse(p.queue_received_at),p.removed_labels[queue]*60/p.interval_seconds,0]);});plot("numbers",numbers);plot("count",counts);plot("speed",speed);$("points").replaceChildren();points.slice(-60).reverse().forEach(p=>textRow([time(p.queue_received_at),(p.queues?.[queue]||[]).join("、")||"未显示",p.count_raw===null?"未知":p.count_raw,p.scheduled_pause?"营业边界暂停":p.pair_ok?"已保存":"部分成功或失败"],$("points")));$("graph-note").textContent=`图表返回 ${points.length} 组。${detail.graph_truncated?"图表投影有截断，完整原始观测仍保存在数据库。":"当前图表投影未截断。"}`;}
function archiveReadStatus(value){
  if(!Array.isArray(value?.configured_store_ids))return '归档加载中';
  const pending=value.calendar_pending_store_ids||[];
  if(pending.length)return `正在加载本月归档：已检查 ${value.calendar_verified_store_count} 家，剩余 ${pending.length} 家；已完成的统计先显示，未加载的数据不补零。`;
  if(value.source_months){
    const sources=[["sapi_miniapp_gateway","官方来源"],["crm_remote_v1_1","早期来源"]];
    const parts=sources.map(([source,label])=>{const month=value.source_months[source],missing=month.unavailable_store_ids.length;return `${label} ${month.configured_store_ids.length} 家${missing?`（${missing} 家读取不完整）`:""}`;});
    return `本月归档：${parts.join("；")}。缺失资料不补零；归档读取状态不代表实时采集状态。`;
  }
  return value.unavailable_store_ids.length?`本月归档读取不完整：${value.unavailable_store_ids.length} 家，缺失资料未补零。`:`已读取 ${Object.keys(value.store_names).length} 家门店的已保存副本。`;
}
async function updateCollectionHealth(){
  const element=$('collection-health');if(!element)return;
  try{const value=await get('/api/v1/collection/health');
    const labels={running:'采集中',starting:'采集启动中',closed:'营业外暂停',completed:'今日采集结束',needs_attention:'采集待恢复'};
    if(value.schema_version!==1||!labels[value.state])throw Error('health unavailable');
    workspaceHealth=value;
    if(taskWorkspace&&workspaceSessionAuto&&workspaceDateValid(value.display_date||'')&&day!==value.display_date){day=value.display_date;$('month').value=day.slice(0,7);detail=null;++detailRequestId;workspaceState.overviewPage=0;recordSource=null;workspaceWriteRoute();}
    element.dataset.state=value.state;element.textContent=labels[value.state];if(taskWorkspace)syncEngineeringSelection();
    element.title=value.heartbeat_stale?'采集状态已超过两分钟未更新':value.next_open_at?`下次开采 ${new Date(value.next_open_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})}`:value.last_progress_at?`最近采样 ${time(value.last_progress_at)}`:'当前服务器采样状态';
  }catch(_){workspaceHealth=null;if(taskWorkspace)workspaceRenderHealth();element.dataset.state='unknown';element.textContent='采样状态未知';element.title='暂时无法读取服务器采样状态';}
}
async function refresh(){
  if(busy)return;
  busy=true;
  if(taskWorkspace)await updateCollectionHealth();$("refresh").disabled=true;if(engineeringPanels)$("header-refresh").disabled=true;
  const month=$("month").value,selected=$("store").value;
  try{
    let value;
    if(combinedHistory){await savedAdapter();value=await savedClient.readMonth(month);}
    else if(referenceSource==="sapi_miniapp_gateway")value=await get(`${referenceApi}/months/${month}`);
    else if(month===today().slice(0,7))value=await get("/api/v1/days");
    else{try{value=await get(`${referenceApi}/months/${month}`);}catch(e){if(e.httpStatus!==404)throw e;value=await get("/api/v1/days");}}
    if(month!==$("month").value)return;
    if(!combinedHistory&&referenceSource==="sapi_miniapp_gateway"&&(value.source!==referenceSource||value.api_profile!=="miniapp_gateway"||value.month!==month))throw Error("月份来源不匹配");
    const currentSelected=taskWorkspace?(workspaceState.routeStore??$("store").value):$("store").value;
    index=value;if(taskWorkspace){workspaceState.readError=false;workspacePopulatePickers(currentSelected);}else{$("store").replaceChildren();const all=document.createElement("option");all.value="";all.textContent="全部门店";$("store").append(all);
    Object.entries(index.store_names).forEach(([id,name])=>{const o=document.createElement("option");o.value=id;o.textContent=name;$("store").append(o);});
    $("store").value=currentSelected;}if(taskWorkspace)workspaceState.routeStore=null;calendar();await showDay({quiet:true});
    if(month!==$("month").value)return;
    $("status").textContent=archiveReadStatus(index);
    if(taskWorkspace){$("workspace-read-at").textContent=`读取于 ${time(Date.now())}`;workspaceRenderOverview();workspaceUpdatePolling();}
  }catch(_){if(month===$("month").value)showError();}
  finally{busy=false;$("refresh").disabled=false;if(engineeringPanels)$("header-refresh").disabled=false;if(month!==$("month").value)refresh();}
}
$("month").value=day.slice(0,7);$("month").onchange=()=>{day=$("month").value+"-01";recordSource=null;resetTicketScope();detail=null;++detailRequestId;calendar();refresh();};$("store").onchange=()=>{recordSource=null;resetTicketScope();if(taskWorkspace){workspaceState.routeStore=null;if(['overview','analysis'].includes(engineeringSection))selectEngineeringSection($('store').value?'analysis':'overview');workspaceWriteRoute({push:true});}calendar();showDay().catch(showError);};$("queue").onchange=()=>{resetTicketScope();syncEngineeringSelection();if(taskWorkspace)workspaceWriteRoute({push:true});graphs();};$("ticket-number").oninput=renderPrediction;$("ticket-issued").oninput=renderPrediction;$("ticket-arrival").oninput=renderPrediction;$("ticket-offset").oninput=renderPrediction;$("ticket-scope").onchange=()=>{ticketScopeKey=$("ticket-scope").checked?scopeKey():null;renderPrediction();};$("refresh").onclick=()=>{if(engineeringSection==="intelligence"&&window.SushiWaitAnalytics){window.SushiWaitAnalytics.refresh();return;}historyCache.clear();refresh();};window.addEventListener("resize",graphs);refresh();
if(engineeringPanels){
  if($("prediction-wait-toggle"))$("prediction-wait-toggle").onclick=()=>{predictionWaitOpen=!predictionWaitOpen;$("prediction-wait-toggle").setAttribute("aria-expanded",String(predictionWaitOpen));selectEngineeringSection("prediction");};
  $("header-refresh").onclick=()=>$("refresh").onclick();
  for(const section of Object.keys(engineeringSections)){const nav=$("nav-"+section);if(nav)nav.onclick=()=>selectEngineeringSection(section);}
  $("engineering-date").onchange=()=>{
    const selected=$("engineering-date").value;
    if(!/^\d{4}-\d{2}-\d{2}$/.test(selected)||!Number.isFinite(Date.parse(selected+"T00:00:00Z"))||new Date(selected+"T00:00:00Z").toISOString().slice(0,10)!==selected){syncEngineeringSelection();return;}
    const changedMonth=selected.slice(0,7)!==$("month").value;
    if(taskWorkspace){workspaceSetDate(selected);return;}
    day=selected;recordSource=null;resetTicketScope();
    if(changedMonth){$("month").value=selected.slice(0,7);detail=null;++detailRequestId;refresh();}
    else{calendar();showDay().catch(showError);}
  };
  selectEngineeringSection(taskWorkspace?workspaceInitialSection:"overview");
  if(taskWorkspace){workspaceInit();workspaceWriteRoute();}
}
const initialPolling=Number(referenceRoot?.dataset?.statisticsPollSeconds??30);
let pollingMs=Number.isInteger(initialPolling)&&initialPolling>=30&&initialPolling<=3600?initialPolling*1000:30000;
engineeringPollingSeconds=pollingMs/1000;syncEngineeringSelection();
let pollingTimer=setInterval(refresh,pollingMs);
if(combinedHistory){
  $("record-source").onchange=()=>{recordSource=$("record-source").value;historyCache.clear();resetTicketScope();if(taskWorkspace)workspaceWriteRoute({push:true});showDay().catch(showError);};
  get('/api/v1/statistics/settings').then(value=>{if(value.schema_version===1&&Number.isInteger(value.poll_seconds)&&value.poll_seconds>=30&&value.poll_seconds<=3600&&value.poll_seconds*1000!==pollingMs){clearInterval(pollingTimer);pollingMs=value.poll_seconds*1000;engineeringPollingSeconds=value.poll_seconds;syncEngineeringSelection();pollingTimer=setInterval(refresh,pollingMs);}}).catch(()=>{});
}
$('ticket-checkin').onchange=$('prediction-mode').onchange=renderPrediction;
$('ticket-clear').onclick=()=>{try{window.localStorage?.removeItem(trackingStorageKey);}catch(_){}$('ticket-number').value='';$('ticket-issued').value='';$('ticket-scope').checked=false;ticketScopeKey=null;$('ticket-tracking-state').textContent='尚未开始跟踪';renderPrediction();};
setInterval(renderPrediction,15000);

window.SushiWaitResearch?.attach({getState:()=>({section:engineeringSection,source:referenceSource,day,storeId:$("store").value,storeName:index.store_names[$("store").value]||$("store").value,storeNames:index.store_names,queueType:{mixedQueue:"ordinary",reservationQueue:"reservation"}[$("queue").value],mode:$("prediction-mode").value,number:$("ticket-number").value,issued:$("ticket-issued").value,checkedIn:$("ticket-checkin").value,confirmed:$("ticket-scope").checked&&ticketScopeKey===scopeKey(),detail,samplingSeconds:engineeringPollingSeconds,result:latestReferenceResult,advice:selectedAdvice({storeId:$("store").value,queueType:{mixedQueue:"ordinary",reservationQueue:"reservation"}[$("queue").value]})}),today,render:renderPrediction,navigate:selectEngineeringSection,history:()=>historyCache.get(`${referenceSource}|${$("store").value}|${day}`)?.days||[],index:()=>index,cityById:workspaceCityById});

window.SushiWaitAnalytics?.attach({today,section:()=>engineeringSection,cityById:workspaceCityById,navigateStore:(store,date,source)=>{day=date;recordSource=source;workspaceState.city=workspaceCityById[store]||"城市待核";workspacePopulatePickers(store);$("month").value=date.slice(0,7);workspaceState.routeStore=store;selectEngineeringSection("analysis");refresh();}});
