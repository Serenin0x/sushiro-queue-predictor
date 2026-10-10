import {createStatisticsClient, createStatisticsController} from './statistics-client.mjs';
import {createDisplayCache, directory, scopeStores, calendarForScope, dayKind, moveMonth, queuePresentation, recentRecords, plots, nearestPoint, shanghaiDate, timeText} from './calendar-data.mjs';
import {icons} from './calendar-icons.mjs';

const root=document.getElementById('sushiwait-month-calendar');
const el=id=>root.querySelector('#'+id);
const today=shanghaiDate();
const state={month:today.slice(0,7),date:today,city:'all',store:'all',view:'month',series:{dine:true,reservation:true},theme:'system'};
let snapshot={phase:'idle',index:null,detail:null,detailState:'unselected',error:null};
let openPickerKind=null, menuVersion='', pageUpdatedAt=null;
const displayCache=createDisplayCache(), chartCache=new WeakMap(), detailVersions=new WeakMap();
let display=snapshot, pendingRender=false, renderedScope='', calendarMonth='', detailVersion='', chartVersion='', dayLayoutScope='', dayLayoutHeight=0;
const setText=(node,value)=>{if(node.textContent!==value)node.textContent=value;};
const compact=new Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:0});
const make=(tag, className='', text='')=>{const node=document.createElement(tag);node.className=className;node.textContent=text;return node;};
const svgNS='http://www.w3.org/2000/svg';
function icon(name) {
  const svg=document.createElementNS(svgNS,'svg');
  for (const [key,value] of Object.entries({viewBox:'0 0 24 24',width:'16',height:'16',fill:'none',stroke:'currentColor','stroke-width':'2','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'})) svg.setAttribute(key,value);
  svg.innerHTML=icons[name] || ''; // Reviewed static Lucide paths; never server text.
  return svg;
}
root.querySelectorAll('[data-lucide]').forEach(node=>node.replaceWith(icon(node.dataset.lucide)));
try {state.theme=localStorage.getItem('sushiwait-calendar-theme') || 'system';} catch (_) {}
if (!['light','dark','system'].includes(state.theme)) state.theme='system';
function applyTheme() {root.style.colorScheme=state.theme==='system'?'light dark':state.theme;}
applyTheme();

const controller=createStatisticsController({client:createStatisticsClient(),onChange(next) {
  const completedRead=snapshot.phase==='loading' && ['ready','partial'].includes(next.phase);
  snapshot=next;
  displayCache.remember(next);
  if(completedRead)pageUpdatedAt=Date.now();
  if (next.index && state.store!=='all' && !next.index.configured_store_ids.includes(state.store)) {
    state.store='all';state.city='all';queueMicrotask(selectStatistics);
  }
  render();
}});
function selectStatistics() {
  if (document.hidden) {render();return Promise.resolve();}
  return controller.select({month:state.month,date:state.date,storeId:state.view==='day' && state.date<=shanghaiDate() && state.store!=='all'?state.store:null});
}
function closePickers(restoreFocus=false) {
  const old=openPickerKind;openPickerKind=null;
  ['city','store'].forEach(kind=>{el('sw-'+kind+'-menu').hidden=true;el('sw-'+kind).setAttribute('aria-expanded','false');});
  if(restoreFocus && old) el('sw-'+old).focus();
}
function filterPicker(kind) {
  const query=el('sw-'+kind+'-search').value.trim().toLocaleLowerCase();let matches=0;
  el('sw-'+kind+'-options').querySelectorAll('button').forEach(button=>{button.hidden=!button.textContent.toLocaleLowerCase().includes(query);if(!button.hidden)matches++;});
  el('sw-'+kind+'-empty').hidden=matches>0;
}
function openPicker(kind) {
  if(el('sw-'+kind).disabled)return;
  const wasOpen=openPickerKind===kind;closePickers();if(wasOpen)return;
  openPickerKind=kind;el('sw-'+kind+'-search').value='';filterPicker(kind);
  el('sw-'+kind+'-options').scrollTop=0;el('sw-'+kind+'-menu').hidden=false;el('sw-'+kind).setAttribute('aria-expanded','true');
}
function optionButton(kind,value,label,selected,onSelect) {
  const button=make('button','sw-menu-option');button.type='button';button.dataset[kind]=value;
  button.setAttribute('aria-pressed',String(selected));const check=make('span','sw-menu-check');check.append(icon('check'));
  button.append(make('span','',label),check);button.addEventListener('click',onSelect);return button;
}
function renderPickers() {
  const stores=directory(display.index), cities=[...new Set(stores.map(item=>item.city))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
  if(state.city!=='all' && stores.length && !cities.includes(state.city)) {state.city='all';state.store='all';}
  const selected=stores.find(item=>item.id===state.store);
  const cityLabel=state.city==='all'?'全部':state.city, storeLabel=selected?.name || '全部';
  el('sw-city-label').textContent=cityLabel;el('sw-store-label').textContent=storeLabel;
  el('sw-city').setAttribute('aria-label','选择城市，当前'+cityLabel);el('sw-store').setAttribute('aria-label','选择门店，当前'+storeLabel);
  el('sw-city').disabled=!display.index;el('sw-store').disabled=!display.index || state.city==='all';
  const version=JSON.stringify([stores,state.city,state.store]);if(version===menuVersion)return;menuVersion=version;closePickers();
  const cityOptions=el('sw-city-options');cityOptions.replaceChildren();
  ['all',...cities].forEach(city=>cityOptions.append(optionButton('city',city,city==='all'?'全部':city,state.city===city,()=>{
    if(state.city!==city){state.city=city;state.store='all';}render();selectStatistics();
    requestAnimationFrame(()=>{if(city!=='all'){el('sw-store').focus({preventScroll:true});openPicker('store');}else el('sw-city').focus({preventScroll:true});});
  })));
  const storeOptions=el('sw-store-options');storeOptions.replaceChildren();
  [{id:'all',name:'全部'},...stores.filter(item=>item.city===state.city)].forEach(store=>storeOptions.append(optionButton('store',store.id,store.name,state.store===store.id,()=>{state.store=store.id;render();selectStatistics();closePickers();el('sw-store').focus();})));
}
function monthCells() {return display.index?calendarForScope(display.index,state.month,state.city,state.store):null;}
function renderMonth() {
  const year=Number(state.month.slice(0,4)), month=Number(state.month.slice(5));
  el('sw-month-title').textContent=`${year} 年 ${month} 月`;el('sw-previous').disabled=state.month==='2000-01';el('sw-next').disabled=state.month==='2099-12';
  const days=el('sw-days'), cells=monthCells(), today=shanghaiDate();
  days.classList.toggle('sw-multiple',scopeStores(display.index,state.city,state.store).length>1);
  days.setAttribute('aria-label',`${year} 年 ${month} 月日期`);
  if(calendarMonth!==state.month){days.replaceChildren();calendarMonth=state.month;}
  const first=(new Date(state.month+'-01T00:00:00Z').getUTCDay()+6)%7, count=new Date(Date.UTC(year,month,0)).getUTCDate();
  const previousCount=new Date(Date.UTC(year,month-1,0)).getUTCDate();
  for(let slot=0;slot<Math.ceil((first+count)/7)*7;slot++){
    const day=slot-first+1;
    if(day<1 || day>count){if(!days.querySelector(`[data-slot="${slot}"]`)){const outside=make('div','sw-outside sw-numbers');outside.dataset.slot=slot;outside.setAttribute('aria-hidden','true');outside.append(make('span','sw-date',String(day<1?previousCount+day:day-count)));days.append(outside);}continue;}
    const date=state.month+'-'+String(day).padStart(2,'0'),kind=dayKind(date),cell=cells?.[day-1];
    const future=date>today, loading=!cell && snapshot.phase!=='error';
    let label=future?'—':!cell?loading?'加载':'失败':cell.state==='pending'?'加载':cell.state==='unavailable'?(cell.observations===null?'不可用':'部分'):cell.observations===null?'暂无':String(cell.observations);
    const hasCount=!future && cell?.observations!==null && cell?.observations!==undefined;
    if(hasCount)label=String(cell.observations);
    let button=days.querySelector(`[data-date="${date}"]`);
    if(!button){button=make('button','sw-day');button.type='button';button.dataset.date=date;
      const top=make('span','sw-day-top');top.append(make('span','sw-date sw-numbers',String(day)),make('span','sw-kind'+(kind.work?' sw-work':''),kind.short));button.append(top,make('span','sw-count sw-numbers'));days.append(button);
    }
    button.className='sw-day'+(!cell || cell.observations===null?' sw-missing':'')+(future?' sw-future':'');button.dataset.state=future?'future':cell?.state || (loading?'loading':'error');
    // Incomplete coverage remains distinct and is never painted as complete.
    const level=!future && cell?.coverage!==null && cell?.coverage!==undefined ? 2+Math.min(cell.coverage,cell.coverageIsPartial ? .45 : 1)*22 : 0;
    button.style.setProperty('--sw-level',level+'%');button.setAttribute('aria-pressed',String(date===state.date));
    button.title=date+'，'+kind.name+'，'+(future?'未来日期':!cell?label:cell.coverageIsPartial?'部分资料，'+label:hasCount?`${cell.observations}组观测，采集覆盖${cell.coverage===null?'未知':Math.round(cell.coverage*100)+'%'}`:label);
    button.setAttribute('aria-label',button.title+'，查看当日');
    const number=button.querySelector('.sw-count'), countKey=hasCount+'|'+label;
    if(button.dataset.countKey!==countKey){number.replaceChildren();if(hasCount){number.append(make('span','sw-count-full',label),make('span','sw-count-compact',compact.format(cell.observations)),make('small','','组'));}else number.textContent=label;button.dataset.countKey=countKey;}
  }
}
function summaryRow(ids) {
  const rows=ids.map(id=>display.index.days[state.date]?.[id]).filter(Boolean);
  const pending=ids.some(id=>display.index.calendar_pending_store_ids?.includes(id));
  const unavailable=ids.some(id=>display.index.unavailable_store_ids.includes(id));
  return pending?'加载中':unavailable?(rows.length?`${rows.reduce((n,r)=>n+r.observations,0)}组 · 部分`:'暂不可用'):rows.length?rows.reduce((n,r)=>n+r.observations,0)+'组'+(rows.length!==ids.length?' · 部分':''):'暂无观测';
}
function renderAllDay() {
  const container=el('sw-all-content'),stores=scopeStores(display.index,state.city);
  const groups=state.city==='all'?[...new Set(stores.map(item=>item.city))].sort((a,b)=>a.localeCompare(b,'zh-CN')).map(city=>({label:city,ids:stores.filter(item=>item.city===city).map(item=>item.id),city})):stores.map(store=>({label:store.name,ids:[store.id],store}));
  const structure=JSON.stringify([state.city,groups.map(group=>[group.label,group.ids])]);
  if(container.dataset.structure!==structure){
    const table=make('table','sw-all-table'),head=make('thead'),headings=make('tr');[state.city==='all'?'城市':'门店','观测',''].forEach(label=>headings.append(make('th','',label)));head.append(headings);table.append(head);
    const body=make('tbody');
    for(const group of groups){const row=make('tr');row.append(make('td','',group.label),make('td','sw-numbers'));const action=make('td');const button=make('button','sw-ghost');button.type='button';button.setAttribute('aria-label','查看'+group.label);button.append(icon('chevron-right'));button.addEventListener('click',()=>{state.city=group.city || group.store.city;state.store=group.store?.id || 'all';selectStatistics();render();});action.append(button);row.append(action);body.append(row);}
    table.append(body);container.replaceChildren(table);container.dataset.structure=structure;
  }
  groups.forEach((group,i)=>setText(container.querySelector('tbody').children[i].children[1],summaryRow(group.ids)));
}
function renderDay() {
  el('sw-day-title').textContent=state.date.replaceAll('-',' / ');el('sw-day-title').tabIndex=-1;
  const content=el('sw-day-content'),all=el('sw-all-content'),empty=el('sw-day-empty');
  const scope=state.city+'|'+state.store+'|'+state.date;
  if(dayLayoutScope!==scope){dayLayoutScope=scope;dayLayoutHeight=0;detailVersion='';chartVersion='';}
  function showEmpty(message){
    content.hidden=true;all.hidden=true;empty.hidden=false;empty.style.minHeight=dayLayoutHeight+'px';
    if(!empty.firstElementChild)empty.append(make('span','sw-empty-message'));
    setText(empty.firstElementChild,message);
  }
  if(state.date>shanghaiDate()){showEmpty('这一天还未到来');return;}
  if(state.store==='all'){if(!display.index){showEmpty(snapshot.phase==='error'?'读取失败，请重试':'正在读取…');return;}content.hidden=true;empty.hidden=true;all.hidden=false;renderAllDay();dayLayoutHeight=all.getBoundingClientRect().height;return;}
  if(!display.detail){showEmpty(snapshot.phase==='error'||snapshot.detailState==='error'?'读取失败，请重试':snapshot.phase==='idle'?'更新已暂停':'正在读取…');return;}
  if(!display.detail.points.length){showEmpty('这一天暂无已保存观测');return;}
  content.hidden=false;all.hidden=true;empty.hidden=true;
  const updating=snapshot.phase==='loading'||snapshot.phase==='idle';
  const unavailable={state:'unavailable',observedAt:null,labels:null,display:snapshot.phase==='loading'?'更新中…':'更新已暂停'};
  const dine=updating?unavailable:queuePresentation(display.detail,'mixedQueue'),reservation=updating?unavailable:queuePresentation(display.detail,'reservationQueue');
  for(const [id,data] of [['sw-queue-dine',dine],['sw-queue-reservation',reservation]]){
    const container=el(id), labels=data.labels?.length?data.labels:[data.display];
    if(updating && !container.style.minHeight)container.style.minHeight=container.getBoundingClientRect().height+'px';
    if(!updating)container.style.minHeight='';
    if(container.dataset.labels!==JSON.stringify(labels)){container.replaceChildren(...labels.map(label=>make('span','',label)));container.dataset.labels=JSON.stringify(labels);}
  }
  const responseLabel={fresh:'响应',historical:'历史响应',stale:'资料较旧 · 最后响应',unavailable:'最近一次未取得可用号码'}[dine.state];
  setText(el('sw-response'),updating?snapshot.phase==='loading'?'正在更新已保存观测':'更新已暂停':responseLabel+(dine.observedAt?' '+timeText(dine.observedAt)+' UTC+8':''));
  if(!detailVersions.has(display.detail))detailVersions.set(display.detail,JSON.stringify([display.detail.points,display.detail.graph_truncated]));
  const version=detailVersions.get(display.detail),records=el('sw-records');
  if(detailVersion!==version){
    const rows=recentRecords(display.detail);
    rows.forEach((record,i)=>{let row=records.children[i];if(!row){row=make('tr');for(let col=0;col<4;col++)row.append(make('td','sw-numbers'));records.append(row);}[timeText(record.at),record.dine,record.reservation,record.state].forEach((value,col)=>setText(row.children[col],value));});
    while(records.children.length>rows.length)records.lastElementChild.remove();detailVersion=version;
  }
  setText(el('sw-graph-note'),'第一位是展示参考；展示变化不等于实际叫号。'+(display.detail.graph_truncated?'图点已截断，显示范围内资料。':''));
  drawCharts();
  dayLayoutHeight=content.getBoundingClientRect().height;
}
function render() {
  if(pendingRender)return;pendingRender=true;
  requestAnimationFrame(()=>{pendingRender=false;renderNow();});
}
function renderNow() {
  const scope=[state.view,state.month,state.date,state.city,state.store].join('|');
  const preserveScroll=renderedScope===scope, left=window.scrollX, top=window.scrollY;
  display=displayCache.view(snapshot,state);
  renderPickers();el('sw-month-view').hidden=state.view!=='month';el('sw-day-view').hidden=state.view!=='day';
  el('sw-nav-calendar').setAttribute('aria-pressed',String(state.view==='month'));el('sw-nav-day').setAttribute('aria-pressed',String(state.view==='day'));
  if(state.view==='month')renderMonth();else renderDay();
  let message='';
  if(snapshot.phase==='loading')message='正在读取…';
  else if(snapshot.phase==='error')message='读取失败，请重试';
  else if(snapshot.detailState==='error')message='当天数据读取失败，请重试';
  else if(snapshot.phase==='partial')message=snapshot.index?.calendar_pending_store_ids?.length?'部分门店正在加载':'部分门店暂不可用';
  else if(snapshot.phase==='idle')message='更新已暂停';
  else if(state.view==='month' && monthCells()?.some(cell=>cell.observations!==null && cell.coverageIsPartial))message='部分日期的门店观测不完整';
  if(snapshot.index?.origin_halted===true)message='来源查询被拒绝，采集已保护停止。门店号码暂不更新，已保存记录仍可查看。';
  else if(snapshot.index?.collector_process_state==='failed')message='采集异常，门店号码可能停止更新。请核对最后响应时间。';
  el('sw-status').textContent=message;el('sw-status').hidden=!message;
  el('sw-demo').textContent=pageUpdatedAt?'页面读取 '+timeText(pageUpdatedAt)+' UTC+8':'';
  el('sw-demo').title='这是网页读取时间，门店号码的实际更新时间见最后响应。';
  root.setAttribute('aria-busy',String(snapshot.phase==='loading'));
  const busy=snapshot.phase==='loading';
  el('sw-refresh').setAttribute('aria-busy',String(busy));
  if(busy)el('sw-refresh').classList.add('sw-refreshing');else if(matchMedia('(prefers-reduced-motion:reduce)').matches)el('sw-refresh').classList.remove('sw-refreshing');
  for(const id of ['sw-days','sw-all-content','sw-day-content']){const region=el(id);region.classList.toggle('sw-updating',busy);region.setAttribute('aria-busy',String(busy));}
  el('sw-tooltip').hidden=busy||el('sw-tooltip').hidden;
  if(preserveScroll && (window.scrollX!==left||window.scrollY!==top))window.scrollTo({left,top,behavior:'instant'});
  renderedScope=scope;
}
function drawCharts() {
  if(state.view!=='day'||el('sw-day-content').hidden||!display.detail)return;
  const version=[detailVersions.get(display.detail),state.series.dine,state.series.reservation,...['sw-number-plot','sw-speed-plot','sw-count-plot'].map(id=>Math.floor(el(id).getBoundingClientRect().width))].join('|');
  if(version===chartVersion)return;chartVersion=version;
  if(!chartCache.has(display.detail))chartCache.set(display.detail,plots(display.detail));
  const chart=chartCache.get(display.detail);el('sw-tooltip').hidden=true;
  // Independent reference axes keep unrelated queue numbering scales apart.
  drawPlot('sw-number-plot','reference',[{key:'dine',label:'堂食',segments:chart.dine.reference},{key:'reservation',label:'预约',segments:chart.reservation.reference}]);
  drawPlot('sw-speed-plot','turnover',[{key:'dine',label:'堂食',segments:[chart.dine.turnover]},{key:'reservation',label:'预约',segments:[chart.reservation.turnover]}]);
  drawPlot('sw-count-plot','quantity',[{key:'quantity',label:'原始数量',segments:[chart.quantity]}]);
}
function drawPlot(id,kind,allSeries) {
  const svg=el(id),width=Math.max(180,Math.floor(svg.getBoundingClientRect().width)),height=240;
  const margin={top:28,bottom:55,left:kind==='reference'?57:50,right:kind==='reference'?57:12};
  svg.replaceChildren();svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('height',height);
  function add(tag,attrs={},text=''){const node=document.createElementNS(svgNS,tag);for(const [k,v] of Object.entries(attrs))node.setAttribute(k,String(v));node.textContent=text;svg.append(node);return node;}
  const series=allSeries.filter(s=>s.key==='quantity'||state.series[s.key]);
  const points=series.flatMap(s=>s.segments.flat());const allPoints=allSeries.flatMap(s=>s.segments.flat());
  const units=kind==='reference'?'首项展示号':kind==='turnover'?'展示变化 / 分钟':'原始数量 · 单位待核';
  add('title',{},units+'，仅实际观测，缺口断开，UTC+8');
  add('text',{x:margin.left,y:16},units);add('text',{'data-axis':'x',x:width-margin.right,y:height-5,'text-anchor':'end'},'UTC+8');
  if(!allPoints.length){add('text',{x:width/2,y:height/2,'text-anchor':'middle'},'暂无可用观测');return;}
  let minAt=Math.min(...allPoints.map(p=>p.at)),maxAt=Math.max(...allPoints.map(p=>p.at));
  if(minAt===maxAt){minAt-=30000;maxAt+=30000;}
  const left=margin.left,right=width-margin.right,top=margin.top,bottom=height-margin.bottom;
  const x=at=>left+(at-minAt)/(maxAt-minAt)*(right-left);
  const value=p=>kind==='reference'?p.position:p.value;
  function scaleFor(s){const values=s.segments.flat().map(value);let low=Math.min(...values),high=Math.max(...values);if(!values.length){low=0;high=1;}const pad=Math.max((high-low)*.08,kind==='reference'?1:.1);low=Math.max(0,low-pad);high+=pad;return {low,high,y:n=>bottom-(n-low)/(high-low)*(bottom-top)};}
  const common=scaleFor({segments:[points]});const scales=new Map(series.map(s=>[s.key,kind==='reference'?scaleFor(s):common]));
  add('line',{x1:left,x2:right,y1:bottom,y2:bottom,stroke:'var(--sw-line)'});
  add('line',{x1:left,x2:left,y1:top,y2:bottom,stroke:'var(--sw-line)'});
  if(kind==='reference')add('line',{x1:right,x2:right,y1:top,y2:bottom,stroke:'var(--sw-line)'});
  for(let i=0;i<3;i++){const at=minAt+(maxAt-minAt)*i/2;add('text',{x:x(at),y:bottom+20,'text-anchor':i===0?'start':i===2?'end':'middle'},timeText(at).slice(0,5));}
  function axis(scale,rightAxis,color,label){for(let i=0;i<4;i++){const n=scale.low+(scale.high-scale.low)*i/3;add('text',{x:rightAxis?right+5:left-7,y:scale.y(n)+4,'text-anchor':rightAxis?'start':'end',fill:color},kind==='reference'?String(Math.round(n)):Number(n.toFixed(1)).toLocaleString('zh-CN'));}if(label)add('text',{x:rightAxis?right:left,y:height-23,'text-anchor':rightAxis?'end':'start',fill:color},label);}
  if(kind==='reference')series.forEach(s=>axis(scales.get(s.key),s.key==='reservation',s.key==='reservation'?'var(--sw-second)':'var(--sw-red)',s.label));else axis(common,false,'var(--sw-muted)','');
  series.forEach(s=>{
    const scale=scales.get(s.key),color=s.key==='reservation'?'var(--sw-second)':'var(--sw-red)';
    s.segments.forEach(segment=>{
      if(kind==='reference' && segment.length>1){const d=segment.map((p,i)=>(i?'L':'M')+x(p.at).toFixed(2)+' '+scale.y(value(p)).toFixed(2)).join(' ');add('path',{d,fill:'none',stroke:color,'stroke-width':1.8,...(s.key==='reservation'?{'stroke-dasharray':'5 4'}:{}),'data-series':s.key});}
      segment.forEach(p=>{const dot=add('circle',{cx:x(p.at),cy:scale.y(value(p)),r:kind==='reference'?1.8:2.6,fill:color,'data-series':s.key,'aria-hidden':'true'});const title=document.createElementNS(svgNS,'title');title.textContent=`${timeText(p.at)} UTC+8 · ${s.label} ${kind==='reference'?p.label:Number(p.value.toFixed(3))}`;dot.append(title);});
    });
  });
  const guide=add('line',{x1:left,x2:left,y1:top,y2:bottom,stroke:'var(--sw-muted)',visibility:'hidden'});
  const overlay=add('rect',{x:left,y:top,width:right-left,height:bottom-top,fill:'transparent','data-chart-hit':'','aria-hidden':'true'});
  let pinned=false;
  function inspect(event){
    const box=svg.getBoundingClientRect(),pixel=Math.max(left,Math.min(right,(event.clientX-box.left)*width/box.width));const target=minAt+(pixel-left)/(right-left)*(maxAt-minAt);
    const nearest=nearestPoint(points,target);if(!nearest)return;guide.setAttribute('x1',x(nearest.at));guide.setAttribute('x2',x(nearest.at));guide.setAttribute('visibility','visible');
    const tooltip=el('sw-tooltip');tooltip.replaceChildren();tooltip.append(make('div','sw-tooltip-time sw-numbers',timeText(nearest.at)+' UTC+8'));
    series.forEach(s=>{const actual=s.segments.flat().find(p=>p.at===nearest.at);const row=make('div','sw-tooltip-row');row.append(make('span','',s.label),make('span','sw-numbers',!actual?'该时刻无观测':kind==='reference'?actual.label:Number(actual.value.toFixed(3)).toLocaleString('zh-CN')));tooltip.append(row);});
    tooltip.hidden=false;const rootBox=root.getBoundingClientRect();tooltip.style.left=Math.max(8,Math.min(rootBox.width-tooltip.offsetWidth-8,event.clientX-rootBox.left+12))+'px';tooltip.style.top=box.top-rootBox.top+top+'px';
  }
  const hide=()=>{el('sw-tooltip').hidden=true;guide.setAttribute('visibility','hidden');};
  overlay.addEventListener('pointermove',event=>{if(!pinned)inspect(event);});overlay.addEventListener('pointerleave',()=>{if(!pinned)hide();});overlay.addEventListener('click',event=>{pinned=!pinned;if(pinned)inspect(event);else hide();});
}

['city','store'].forEach(kind=>{
  el('sw-'+kind).addEventListener('click',()=>openPicker(kind));el('sw-'+kind+'-search').addEventListener('input',()=>filterPicker(kind));
  el('sw-'+kind).addEventListener('keydown',event=>{if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();if(openPickerKind!==kind)openPicker(kind);const options=[...el('sw-'+kind+'-options').querySelectorAll('button')].filter(button=>!button.hidden);options[event.key==='ArrowUp'?options.length-1:0]?.focus();}});
  el('sw-'+kind+'-menu').addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();closePickers(true);return;}if(!['ArrowDown','ArrowUp'].includes(event.key))return;event.preventDefault();const options=[...el('sw-'+kind+'-options').querySelectorAll('button')].filter(button=>!button.hidden);if(!options.length)return;const i=options.indexOf(document.activeElement),direction=event.key==='ArrowDown'?1:-1;options[i<0?direction>0?0:options.length-1:(i+direction+options.length)%options.length].focus();});
});
el('sw-days').addEventListener('click',event=>{
  const button=event.target.closest('button[data-date]');if(!button)return;
  state.date=button.dataset.date;state.view='day';closePickers();selectStatistics();render();
  requestAnimationFrame(()=>el('sw-day-title').focus({preventScroll:true}));
});
document.addEventListener('pointerdown',event=>{if(!el('sw-pickers').contains(event.target))closePickers();});
el('sw-pickers').addEventListener('focusout',event=>{if(!el('sw-pickers').contains(event.relatedTarget))closePickers();});
el('sw-theme').addEventListener('click',()=>{const dark=state.theme==='dark'||state.theme==='system'&&matchMedia('(prefers-color-scheme:dark)').matches;state.theme=dark?'light':'dark';applyTheme();try{localStorage.setItem('sushiwait-calendar-theme',state.theme);}catch(_){} });
function changeMonth(offset){state.month=moveMonth(state.month,offset);state.date=state.month+'-01';state.view='month';selectStatistics();render();}
el('sw-previous').addEventListener('click',()=>changeMonth(-1));el('sw-next').addEventListener('click',()=>changeMonth(1));
el('sw-this-month').addEventListener('click',()=>{state.month=shanghaiDate().slice(0,7);state.date=shanghaiDate();state.view='month';selectStatistics();render();});
['sw-back','sw-nav-calendar'].forEach(id=>el(id).addEventListener('click',()=>{state.view='month';selectStatistics();render();}));
el('sw-nav-day').addEventListener('click',()=>{state.view='day';selectStatistics();render();});
el('sw-refresh').addEventListener('click',()=>controller.refresh());
el('sw-refresh').addEventListener('animationiteration',()=>{if(snapshot.phase!=='loading')el('sw-refresh').classList.remove('sw-refreshing');});
root.querySelectorAll('.sw-series-control').forEach(button=>button.addEventListener('click',()=>{const key=button.dataset.series;state.series[key]=!state.series[key];button.setAttribute('aria-pressed',String(state.series[key]));el('sw-tooltip').hidden=true;drawCharts();}));
const resize=new ResizeObserver(drawCharts);root.querySelectorAll('.sw-chart-panel').forEach(panel=>resize.observe(panel));
function resume() {
  const selected=controller.snapshot().selection;
  const storeId=state.view==='day' && state.date<=shanghaiDate() && state.store!=='all'?state.store:null;
  if(selected && selected.month===state.month && selected.date===state.date && selected.storeId===storeId)controller.refresh();else selectStatistics();
  controller.start();
}
document.addEventListener('visibilitychange',()=>{if(document.hidden)controller.stop();else resume();});
window.addEventListener('pagehide',()=>{controller.stop();resize.disconnect();});
window.addEventListener('pageshow',event=>{if(event.persisted){root.querySelectorAll('.sw-chart-panel').forEach(panel=>resize.observe(panel));resume();}});
render();if(!document.hidden){selectStatistics();controller.start();}
