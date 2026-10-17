"use strict";
// Submitted tickets stay in the browser unless their owner explicitly chooses server recording.
(function(root){
 const stores=new Set(['3014','3004','2009','3015','2001']),$=id=>document.getElementById(id),storageKey='sushiwait.submitted-tickets.v1';
 let config=null,items=[],selected=null,waitData=null,waitAt=0,waitBusy=false,waitAdvice=null,listBusy=false,lastList=0,memoryOwner=null,toastTimer,recordRevision=0;
 const deletedIds=new Set();
 const waitClient=random(16);
 const cards=new Map(),leases=new Map(),histories=new Map(),tableRows=new Map();
 const tableState={page:0,size:10,city:'all',store:'',from:'',to:'',kind:'all',query:'',sort:'desc'};
 function random(bytes){return [...crypto.getRandomValues(new Uint8Array(bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');}
 function secure(){return location.protocol==='https:'||['127.0.0.1','localhost'].includes(location.hostname);}
 function owner(){if(memoryOwner)return memoryOwner;try{const v=localStorage.getItem('sushiwait.research-owner.v1');if(/^[0-9a-f]{64}$/.test(v||''))return memoryOwner=v;}catch(_){}memoryOwner=random(32);try{localStorage.setItem('sushiwait.research-owner.v1',memoryOwner);}catch(_){}return memoryOwner;}
 async function post(path,value){if(path==='/api/v1/research/tickets'&&!secure())throw Error('https_required');const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({schema_version:1,...value}),cache:'no-store',signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error('save_failed');return r.json();}
 async function get(path){const r=await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(10000)});if(!r.ok)throw Error('read_failed');return r.json();}
 function makeTicket(state,date,time,number,entry){
  const at=Date.parse(`${date}T${time.length===5?time+':00':time}+08:00`);
  if(!/^[1-9]\d{0,9}$/.test(state.storeId)||!['ordinary','reservation'].includes(state.queueType)||!/^\d{1,7}$/.test(number)||Number(number)<1||!/^\d{4}-\d{2}-\d{2}$/.test(date)||!/^\d{2}:\d{2}(?::\d{2})?$/.test(time)||!Number.isFinite(at)||at>Date.now()||new Date(at+28800000).toISOString().slice(0,10)!==date)throw Error('invalid_ticket');
  return {store_id:state.storeId,queue_type:state.queueType,number,issued_at:new Date(at).toISOString(),checked_in:state.checkedIn==='unknown'?null:state.checkedIn==='yes',entry_mode:entry,prediction_reason:null};
 }
 function persist(){try{localStorage.setItem(storageKey,JSON.stringify({schema_version:1,deleted_ids:[...deletedIds].slice(-5000),items:items.slice(0,100).map(i=>({id:i.id,ticket:i.ticket,source:i.source,storeName:i.storeName,mode:i.mode,kind:i.kind,follow:i.follow,record:i.record,saved:i.saved,createdAt:i.createdAt,result:i.result,arrived:i.arrived,lastAt:i.lastAt}))}));}catch(_){} }
 function restore(){try{const v=JSON.parse(localStorage.getItem(storageKey)||'{}');if(v.schema_version!==1||!Array.isArray(v.items))return;for(const id of v.deleted_ids||[])if(/^[0-9a-f]{32}$/.test(id))deletedIds.add(id);for(const i of v.items.slice(0,100)){if(deletedIds.has(i.id)||!/^[0-9a-f]{32}$/.test(i.id)||!['prediction','record'].includes(i.kind)||!['auto','algorithm','nonthinking','thinking'].includes(i.mode)||!['sapi_miniapp_gateway','crm_remote_v1_1'].includes(i.source))continue;const t=i.ticket;const d=new Date(Date.parse(t.issued_at)+28800000).toISOString();makeTicket({storeId:t.store_id,queueType:t.queue_type,checkedIn:t.checked_in===null?'unknown':t.checked_in?'yes':'no'},d.slice(0,10),d.slice(11,16),t.number,t.entry_mode);items.push({...i,nextAt:0,result:i.result&&typeof i.result.available==='boolean'?i.result:null,arrived:i.arrived?.policy===root.SushiWaitReference?.ARRIVAL_POLICY?i.arrived:null,needsRecheck:i.kind==='prediction'});}}catch(_){} }
 function node(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
 function clock(at){return Number.isFinite(Date.parse(at))?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(at)):'—';}
 function issuedLabel(at){return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(at));}
 const reasons={ticket_precedes_issue:'取号前的展示已超过此号，正在核对数据',insufficient_sparse_trend:'正在积累有效趋势',no_current_sample:'等待有效叫号数据',stale_sample:'等待下一次采集更新',awaiting_post_issue_sample:'等待取号后的观测',ticket_cycle_uncertain:'号码轮次暂时无法确认',awaiting_arrival_evidence:'已接近，等待到号证据',today_only:'这张号码已不在今天的预测范围',archive_unavailable:'没有对应日期的观测',unsupported_queue:'请选择堂食或预约队列',arrival_complete:'已到号'};
 function card(item){if(cards.has(item.id))return cards.get(item.id);const el=node('article','card ticket-result-card'),head=node('div','ticket-result-heading'),title=node('div'),number=node('strong','ticket-result-number',item.ticket.number),store=node('h3','',item.storeName),kind=node('span','ticket-kind',item.kind==='record'?'记录':'预测');title.append(store,number);head.append(title,kind);const meta=node('p','ticket-meta',`${issuedLabel(item.ticket.issued_at)} 取号　${item.ticket.queue_type==='ordinary'?'堂食':'预约'}`),body=node('div','ticket-estimate'),primary=node('strong','ticket-remaining'),range=node('p','ticket-range'),status=node('p','ticket-result-status'),tools=node('div','ticket-result-tools');body.append(primary,range);const evidence=node('details','ticket-evidence'),evidenceTitle=node('summary','','计算依据'),evidenceText=node('p','');evidence.append(evidenceTitle,evidenceText);const result={el,primary,range,status,kind,tools,evidence,evidenceText};
  if(item.kind==='prediction'){const refresh=node('button','button-quiet','刷新预测');refresh.type='button';refresh.onclick=()=>refreshItem(item,true);result.refresh=refresh;const follow=node('button','button-quiet');follow.type='button';follow.onclick=()=>{item.follow=!item.follow;item.nextAt=0;persist();updateCard(item);notify();};result.follow=follow;tools.append(refresh,follow);}
  tools.append(deleteButton(item));
  if(item.record&&!item.saved){const retry=node('button','button-quiet','重试录入');retry.type='button';retry.onclick=async()=>{retry.disabled=true;await saveItem(item);retry.disabled=false;updateCard(item);};result.retry=retry;tools.append(retry);}
  el.append(head,meta,body,status,evidence,tools);cards.set(item.id,result);return result;
 }
 function updateCard(item){const c=card(item),arrived=item.arrived?.available;
  if(item.kind==='record'){c.primary.textContent=item.saved?'记录已保存':'尚未保存';c.range.textContent='';}
  else if(item.result?.reason==='ticket_precedes_issue'){c.primary.textContent='数据待核对';c.range.textContent='号码与采集趋势冲突，暂不判定到号';}
  else if(arrived){c.primary.textContent=item.arrived.state==='arrived'?'已到号':'推定已到号';c.range.textContent=`${clock(item.arrived.lower_at)}—${clock(item.arrived.upper_at)} 展示到号区间`; }
  else if(item.result?.available&&Date.now()-Date.parse(item.result.sample_at)>((config.getState().samplingSeconds||600)+60)*1000){c.primary.textContent='等待刷新';c.range.textContent=`上次预计 ${clock(item.result.reference_time)}`;}
  else if(item.result?.available&&Date.parse(item.result.reference_time)<=Date.now()){c.primary.textContent='请刷新核对';c.range.textContent='预计时刻已过，尚无新的到号证据';}
  else if(item.result?.available){const r=item.result,remaining=Math.max(0,Math.round((Date.parse(r.reference_time)-Date.now())/60000));c.primary.textContent=remaining>0?`约 ${remaining} 分钟`:'即将到号';c.range.textContent=`预计 ${clock(r.reference_time)}，范围 ${clock(r.scenario_earliest)}—${r.scenario_latest?clock(r.scenario_latest):'待定'}`;}
  else {c.primary.textContent=item.busy?'正在计算':'暂无法估计';c.range.textContent=item.readError?'观测读取失败，保留上次结果':reasons[item.result?.reason]||'等待更多有效观测';}
  const labels={auto:'算法与 Flash',algorithm:'历史与实时算法',thinking:'Flash 思考',nonthinking:'Flash 非思考'};const saved=item.record?(item.saved?'已录入系统':'尚未录入系统'):'仅保存于当前浏览器';
  const modelText=item.result?.ai_participated?labels[item.result.ai_mode]+'已参与':arrived?'展示规则判定':item.mode==='algorithm'?labels.algorithm:`${labels[item.mode]}未参与：${modelStateText(item.modelState)}`;
  c.status.textContent=item.kind==='record'?saved:`${modelText}　${saved}${item.lastAt?'　'+clock(new Date(item.lastAt).toISOString())+' 更新':''}`;
  c.evidence.hidden=item.kind==='record';const a=item.result?.arrival||item.arrived,r=item.result;
  c.evidenceText.textContent=a?.reason==='ticket_precedes_issue'?`${clock(a.prior_sample_at)} 已显示 ${a.prior_display_numbers.join('、')}，早于填写的取号时间；当前不能据此估算这张票。`:arrived?`${item.arrived.display_numbers.join('、')}。${item.arrived.state==='arrived'?'号码出现在前两位':'三个展示号码全部超过本人号'}，属于展示到号参考。`:r?.available?`${clock(r.sample_at)} 首位 ${r.reference}，相差 ${r.position_difference} 个号码。近 ${Math.round(r.continuous_minutes)} 分钟 ${r.sample_count} 个观测，历史 ${r.history?.selected_days||0} 天。${r.provisional?'初步估计，样本较少。':''}${r.ai_participated?'模型按当前趋势调整情景权重。':''}`:reasons[r?.reason]||'正在读取有效观测';
  if(c.refresh){c.refresh.disabled=Boolean(item.busy);c.refresh.textContent=item.busy?'正在更新':'刷新预测';c.follow.textContent=item.follow?'停止跟踪':'持续跟踪';c.follow.setAttribute('aria-pressed',String(Boolean(item.follow)));c.follow.disabled=Boolean(arrived)||new Date(Date.parse(item.ticket.issued_at)+28800000).toISOString().slice(0,10)!==config.today();}
  if(c.retry)c.retry.hidden=Boolean(item.saved);c.el.classList.toggle('ticket-arrived',Boolean(arrived));updateTableRow(item);
 }
 function ticketDate(item){return new Date(Date.parse(item.ticket.issued_at)+28800000).toISOString().slice(0,10);}
 function filteredItems(){return items.filter(i=>(tableState.city==='all'||(config.cityById?.[i.ticket.store_id]||'城市待核')===tableState.city)&&(!tableState.store||i.ticket.store_id===tableState.store)&&(!tableState.from||ticketDate(i)>=tableState.from)&&(!tableState.to||ticketDate(i)<=tableState.to)&&(tableState.kind==='all'||i.kind===tableState.kind)&&(!tableState.query||i.ticket.number.includes(tableState.query))).sort((a,b)=>(Date.parse(a.ticket.issued_at)-Date.parse(b.ticket.issued_at)||a.id.localeCompare(b.id))*(tableState.sort==='asc'?1:-1));}
 function updateTableRow(item){const row=tableRows.get(item.id);if(!row)return;const c=card(item),result=item.serverResult;
  row.status.textContent=item.kind==='record'&&result?.result_available?(result.state==='arrived'?'已展示到号':'推定已到号'):c.primary.textContent;
  row.sub.textContent=item.kind==='prediction'?c.range.textContent:item.saved?'已录入系统':'等待保存';
  row.follow.textContent=item.kind==='record'?'—':item.follow?'自动跟踪中':'手动刷新';
  if(row.refresh){row.refresh.disabled=Boolean(item.busy);row.refresh.textContent=item.busy?'更新中':'刷新预测';}
 }
 function ticketPickers(){if(!$('tickets-city'))return;const cities=[...new Set(items.map(i=>config.cityById?.[i.ticket.store_id]||'城市待核'))].sort((a,b)=>a.localeCompare(b,'zh-CN'));if(tableState.city!=='all'&&!cities.includes(tableState.city))tableState.city='all';
  const options=(id,rows,value)=>{const el=$(id);el.replaceChildren();for(const [v,label]of rows){const o=node('option','',label);o.value=v;el.append(o);}el.value=value;};
  options('tickets-city',[['all','全部城市'],...cities.map(c=>[c,c])],tableState.city);
  const names=new Map(items.filter(i=>tableState.city==='all'||(config.cityById?.[i.ticket.store_id]||'城市待核')===tableState.city).map(i=>[i.ticket.store_id,i.storeName]));
  if(tableState.store&&!names.has(tableState.store))tableState.store='';
  options('tickets-store',[['','全部门店'],...[...names].sort((a,b)=>a[1].localeCompare(b[1],'zh-CN'))],tableState.store);
 }
 function renderItems(){const target=$('my-tickets');target.replaceChildren();tableRows.clear();ticketPickers();
  const filtered=filteredItems(),pages=Math.max(1,Math.ceil(filtered.length/tableState.size));tableState.page=Math.min(tableState.page,pages-1);
  const table=node('table','ticket-database'),thead=node('thead'),header=node('tr');for(const name of ['号码 / 门店','取号时间','类型 / 模型','当前状态','跟踪','操作'])header.append(node('th','',name));thead.append(header);table.append(thead);const tbody=node('tbody');
  for(const item of filtered.slice(tableState.page*tableState.size,(tableState.page+1)*tableState.size)){
   updateCard(item);const row=node('tr'),identity=node('td'),number=node('button','ticket-row-number',item.ticket.number);number.type='button';number.onclick=()=>openDetail(item);identity.append(number,node('span','ticket-row-store',item.storeName));
   const date=node('td','ticket-row-date',issuedLabel(item.ticket.issued_at)),kind=node('td'),status=node('td'),primary=node('strong','ticket-row-state'),sub=node('span','ticket-row-sub'),follow=node('td','ticket-row-follow'),actions=node('td','ticket-row-actions');
   kind.append(node('span','',item.kind==='record'?'记录':'预测'),node('span','ticket-row-sub',item.kind==='record'?(item.ticket.queue_type==='ordinary'?'堂食':'预约'):({auto:'自动融合',algorithm:'算法',thinking:'Flash 思考',nonthinking:'Flash 非思考'})[item.mode]));status.append(primary,sub);
   let refresh;if(item.kind==='prediction'){refresh=node('button','button-quiet','刷新预测');refresh.type='button';refresh.onclick=()=>refreshItem(item,true);actions.append(refresh);}
   const more=node('button','button-quiet','详情');more.type='button';more.setAttribute('aria-label',item.ticket.number+' 号详情');more.onclick=()=>openDetail(item);actions.append(more,deleteButton(item));row.append(identity,date,kind,status,follow,actions);tbody.append(row);tableRows.set(item.id,{status:primary,sub,follow,refresh});updateTableRow(item);
  }
  table.append(tbody);target.append(table);
  if(!filtered.length){const empty=node('div','ticket-table-empty',items.length?'没有符合筛选的号码':'还没有号码记录');target.append(empty);}
  $('my-ticket-count').textContent=`共 ${items.length} 条，当前筛选 ${filtered.length} 条`;
  if($('tickets-page-note'))$('tickets-page-note').textContent=`第 ${tableState.page+1} / ${pages} 页`;
  if($('tickets-prev'))$('tickets-prev').disabled=tableState.page===0;
  if($('tickets-next'))$('tickets-next').disabled=tableState.page>=pages-1;
 }
 function openDetail(item){selected=item.id;updateCard(item);const dialog=$('ticket-detail-dialog');$('ticket-detail-body').replaceChildren(card(item).el);if(dialog.showModal)dialog.showModal();else dialog.hidden=false;}
 function tableSetup(){if(!$('tickets-city'))return;
  for(const [id,key]of [['tickets-city','city'],['tickets-store','store'],['tickets-from','from'],['tickets-to','to'],['tickets-kind','kind'],['tickets-sort','sort']])$(id).onchange=()=>{tableState[key]=$(id).value;if(key==='city')tableState.store='';if(key==='from'||key==='to')$('tickets-period').value='custom';tableState.page=0;renderItems();};
  $('tickets-search').oninput=()=>{tableState.query=$('tickets-search').value.trim();tableState.page=0;renderItems();};
  $('tickets-size').onchange=()=>{tableState.size=Number($('tickets-size').value)||10;tableState.page=0;renderItems();};
  $('tickets-prev').onclick=()=>{tableState.page=Math.max(0,tableState.page-1);renderItems();};$('tickets-next').onclick=()=>{tableState.page++;renderItems();};
  $('tickets-period').onchange=()=>{const value=$('tickets-period').value;if(value==='custom')return;tableState.to=value==='all'?'':config.today();tableState.from=value==='all'?'':new Date(Date.parse(config.today()+'T00:00:00Z')-(Number(value)-1)*86400000).toISOString().slice(0,10);$('tickets-from').value=tableState.from;$('tickets-to').value=tableState.to;tableState.page=0;renderItems();};
  $('ticket-detail-close').onclick=()=>{$('ticket-detail-dialog').close?.();$('ticket-detail-dialog').hidden=false;};
  $('tickets-export').onclick=()=>{const lines=[['门店','号码','取号时间','队列','类型','模型','保存状态'],...filteredItems().map(i=>[i.storeName,i.ticket.number,issuedLabel(i.ticket.issued_at),i.ticket.queue_type,i.kind,i.mode,i.saved?'已保存':'本机'])];const safe=v=>'"'+String(v).replace(/^[=+@-]/,"'").replaceAll('"','""')+'"';const blob=new Blob(['\ufeff'+lines.map(r=>r.map(safe).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download='sushiwait-my-tickets.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
 }
 function showSuccess(message='提交成功',undo=null){const toast=$('ticket-success');clearTimeout(toastTimer);$('ticket-success-label').textContent=message;const button=$('ticket-delete-undo');button.hidden=!undo;button.disabled=false;button.onclick=undo;toast.hidden=false;toast.classList.remove('success-enter');void toast.offsetWidth;toast.classList.add('success-enter');toastTimer=setTimeout(()=>{toast.hidden=true;},undo?10000:2200);}
 function deleteButton(item){const button=node('button','button-quiet ticket-delete','删除');button.type='button';button.setAttribute('aria-label','删除 '+item.ticket.number+' 号记录');button.disabled=Boolean(item.deleting);button.onclick=()=>deleteItem(item,button);return button;}
 function forgetItem(item){release(item.id);item.follow=false;item.modelUntil=0;const at=items.indexOf(item);if(at>=0)items.splice(at,1);cards.delete(item.id);tableRows.delete(item.id);if(selected===item.id){selected=null;$('ticket-detail-dialog').close?.();}}
 async function deleteItem(item,button){if(item.deleting||deletedIds.has(item.id))return;if(!root.confirm(`删除${item.storeName}的 ${item.ticket.number} 号记录？\n删除后将停止跟踪，并从记录和分析中移除。`))return;
  item.deleting=true;button.disabled=true;button.textContent='正在删除';++recordRevision;lastList=0;
  try{if(item.saving)await item.saving;const result=item.record?await post('/api/v1/research/tickets',{action:'delete',record_id:item.id,owner_secret:owner()}):null;
   ++recordRevision;deletedIds.add(item.id);forgetItem(item);persist();renderItems();$('my-ticket-status').textContent='';root.SushiWaitAnalytics?.localChanged?.();
   showSuccess('记录已删除',async()=>{const undo=$('ticket-delete-undo');undo.disabled=true;clearTimeout(toastTimer);++recordRevision;try{if(result?.found){const restored=await post('/api/v1/research/tickets',{action:'restore',record_id:item.id,owner_secret:owner()});if(!restored.found)throw Error('restore_failed');}++recordRevision;deletedIds.delete(item.id);item.deleting=false;if(!items.some(i=>i.id===item.id))items.unshift(item);lastList=0;persist();renderItems();root.SushiWaitAnalytics?.localChanged?.();showSuccess('记录已恢复');}catch(_){undo.disabled=false;$('my-ticket-status').textContent='恢复失败，请稍后重试';}});
  }catch(_){$('my-ticket-status').textContent='删除失败，记录已保留，请重试';button.disabled=false;button.textContent='删除';}
  finally{item.deleting=false;lastList=0;}
 }
 async function saveItem(item){if(deletedIds.has(item.id))return false;if(item.saving)return item.saving;item.saving=(async()=>{try{const result=await post('/api/v1/research/tickets',{action:'submit',record_id:item.id,owner_secret:owner(),ticket:item.ticket});if(result.deleted||deletedIds.has(item.id)){item.saveError='record_deleted';return false;}if(result.saved!==true)throw Error('save_failed');item.saved=true;persist();lastList=0;return true;}catch(e){item.saveError=e.message;return false;}})();try{return await item.saving;}finally{item.saving=null;}}
 function sameTicket(a,b){return ['store_id','queue_type','number'].every(k=>a[k]===b[k])&&Date.parse(a.issued_at)===Date.parse(b.issued_at);}
 async function submitForm(kind,event){event.preventDefault();const button=$(kind==='prediction'?'ticket-submit':'research-submit'),status=$(kind==='prediction'?'ticket-record-status':'research-status');if(button.disabled)return;const s=config.getState();let ticket;
  try{ticket=makeTicket(kind==='record'?{...s,checkedIn:$('research-checkin').value}:s,kind==='record'?$('research-date').value:config.today(),$(kind==='record'?'research-time':'ticket-issued').value,$(kind==='record'?'research-number':'ticket-number').value,kind==='record'?'retrospective':'live');if(kind==='prediction'&&s.day!==config.today())throw Error('today_only');}catch(_){status.textContent='请选择门店，填写有效号码和取号时间';return;}
  const record=kind==='record'||$('ticket-record-system').checked;if(record&&!secure()){status.textContent='请从 HTTPS 加密地址录入系统';return;}
  if(items.filter(i=>!i.saved).length>=100&&!items.some(i=>i.kind===kind&&i.source===s.source&&sameTicket(i.ticket,ticket))){status.textContent='当前浏览器已有100张号码';return;}
  button.disabled=true;status.textContent='';let item=items.find(i=>i.kind===kind&&i.source===s.source&&sameTicket(i.ticket,ticket));if(!item){item={id:random(16),ticket,source:s.source,storeName:s.storeName,mode:s.mode,kind,follow:kind==='prediction'&&$('ticket-follow').checked,record,saved:false,createdAt:new Date().toISOString(),nextAt:0,result:null,arrived:null};items.unshift(item);}else{item.mode=s.mode;item.follow=kind==='prediction'&&$('ticket-follow').checked;item.record=item.record||record;}
  selected=item.id;persist();renderItems();if(kind==='prediction')await refreshItem(item,true);
  if(item.record&&!item.saved){item.ticket.prediction_reason=item.kind==='prediction'&&!item.result?.available?item.result?.reason||'not_computed_yet':null;await saveItem(item);}
  updateCard(item);button.disabled=false;
  if(item.record&&!item.saved){status.textContent=item.saveError==='https_required'?'请使用 HTTPS 加密入口':'记录尚未保存，可在我的号码重试';$('my-ticket-status').textContent=status.textContent;}
  else {$('my-ticket-status').textContent='';showSuccess();}
  config.navigate('tickets');notify();
 }
 function modelState(value,item){if(!value)return 'unavailable';if(['finished','budget_exhausted','waiting_for_configuration','waiting_for_open'].includes(value.status))return value.status;const modes=value.stores?.[item.ticket.store_id]?.[item.ticket.queue_type];return modes?.[item.mode==='auto'?'thinking':item.mode]?.state||'waiting_for_viewer';}
 function modelStateText(state){return ({finished:'服务已结束',waiting_for_configuration:'尚未启用',waiting_for_open:'等待营业',budget_exhausted:'今日预算已用完',deepseek_budget_exhausted:'今日预算已用完',deepseek_budget_expired:'当日预算已到期',input_conflict:'先核对号码与数据',waiting_for_viewer:'等待计算',waiting_for_observation:'等待新观测',ready:'等待与当前观测匹配',cached:'等待与当前观测匹配',fallback:'结果未通过校验',previous_attempt_unknown:'上次调用未完成',deepseek_timeout:'调用超时',deepseek_http_failed:'服务暂不可用'})[state]||'正在读取模型状态';}
 function adviceFor(advice,item){if(item.mode==='algorithm')return null;const models=advice?.stores?.[item.ticket.store_id]?.[item.ticket.queue_type];return item.mode==='auto'?(models?.thinking?.advice||models?.nonthinking?.advice||null):models?.[item.mode]?.advice||null;}
 async function historyFor(item,date){const key=`${item.source}|${item.ticket.store_id}|${date}`;if(histories.has(key))return histories.get(key);const s=config.getState();if(s.storeId===item.ticket.store_id&&s.day===date&&s.source===item.source&&config.history().length){histories.set(key,config.history());return config.history();}const dates=Object.keys(config.index().days||{}).filter(d=>d<date&&config.index().days[d]?.[item.ticket.store_id]).sort().reverse().slice(0,6);const result=await Promise.allSettled(dates.map(d=>get(`${item.source==='sapi_miniapp_gateway'?'/api/v1/official':'/api/v1'}/stores/${item.ticket.store_id}/days/${d}`)));const days=result.filter(v=>v.status==='fulfilled').map(v=>v.value);histories.set(key,days);return days;}
 async function refreshItem(item,manual=false){if(item.busy||item.kind!=='prediction'||item.deleting||deletedIds.has(item.id))return;item.busy=true;item.readError=false;if(manual){item.modelUntil=Date.now()+150000;selected=item.id;}updateCard(item);
  try{const date=new Date(Date.parse(item.ticket.issued_at)+28800000).toISOString().slice(0,10),s=config.getState();const detail=await get(`${item.source==='sapi_miniapp_gateway'?'/api/v1/official':'/api/v1'}/stores/${item.ticket.store_id}/days/${date}`);const days=await historyFor(item,date);let advice=null;if(item.mode!=='algorithm')try{advice=await get('/model-advice.json');}catch(_){}
   if(item.deleting||deletedIds.has(item.id)||!items.includes(item))return;
   const options={source:item.source,storeId:item.ticket.store_id,queueType:item.ticket.queue_type,ticket:item.ticket.number,issuedAt:item.ticket.issued_at,checkedIn:item.ticket.checked_in,confirmedScope:true,now:Date.now(),samplingSeconds:s.samplingSeconds||600};
   const arrived=root.SushiWaitReference?.arrival(detail,options);item.arrived=arrived||null;item.needsRecheck=false;item.modelState=arrived?.state==='input_conflict'?'input_conflict':modelState(advice,item);
   item.result=root.SushiWaitReference?.trackedForecast(detail,options,days,adviceFor(advice,item))||{available:false,reason:'insufficient_sparse_trend'};if(item.arrived?.available){item.follow=false;item.modelUntil=0;}
   if(item.result.ai_participated||['finished','budget_exhausted','input_conflict','deepseek_budget_exhausted'].includes(item.modelState))item.modelUntil=0;item.lastAt=Date.now();item.nextAt=Date.now()+(s.samplingSeconds||600)*1000;persist();
  }catch(_){item.readError=true;item.result={available:false,reason:'archive_unavailable'};item.nextAt=Date.now()+30000;}finally{item.busy=false;if(items.includes(item))updateCard(item);sendPresence();}
 }
 function release(id){const old=leases.get(id);if(!old)return;leases.delete(id);post('/api/v1/prediction/presence',{...old.value,visible:false}).catch(()=>{});}
 function sendPresence(){if(!config)return;const visible=document.visibilityState==='visible'&&['prediction','tickets'].includes(config.getState().section);const active=new Set();for(const item of items){const date=new Date(Date.parse(item.ticket.issued_at)+28800000).toISOString().slice(0,10);if(!visible||item.kind!=='prediction'||item.arrived?.available||item.modelState==='input_conflict'||!(item.follow||item.modelUntil>Date.now())||date!==config.today()||!stores.has(item.ticket.store_id)||item.source!=='sapi_miniapp_gateway'||item.mode==='algorithm')continue;active.add(item.id);const value={client_id:item.id,store_id:item.ticket.store_id,queue_type:item.ticket.queue_type,mode:item.mode,visible:true},old=leases.get(item.id);if(old&&JSON.stringify(old.value)===JSON.stringify(value)&&Date.now()-old.at<45000)continue;leases.set(item.id,{value,at:Date.now()});post('/api/v1/prediction/presence',value).catch(()=>{leases.delete(item.id);});}const s=config.getState(),cohort=waitData?.stores?.[s.storeId]?.[s.queueType];if(document.visibilityState==='visible'&&s.section==='prediction'&&!$('store-wait-card')?.hidden&&cohort?.sample_count>=5&&stores.has(s.storeId)&&s.source==='sapi_miniapp_gateway'&&s.day===config.today()&&s.mode!=='algorithm'){active.add(waitClient);const value={client_id:waitClient,store_id:s.storeId,queue_type:s.queueType,mode:s.mode,visible:true},old=leases.get(waitClient);if(!old||Date.now()-old.at>=45000){leases.set(waitClient,{value,at:Date.now()});post('/api/v1/prediction/presence',value).catch(()=>leases.delete(waitClient));}}for(const id of leases.keys())if(!active.has(id))release(id);}
 function renderWait(){if(!config)return;const s=config.getState(),r=s.detail&&root.SushiWaitReference?.storeWait(s.detail,{source:s.source,storeId:s.storeId,queueType:s.queueType,now:Date.now(),samplingSeconds:s.samplingSeconds||600},waitData?.stores?.[s.storeId]?.[s.queueType],adviceFor(waitAdvice,{ticket:{store_id:s.storeId,queue_type:s.queueType},mode:s.mode}));$('store-wait-result').textContent=r?.available?`约 ${Math.round(r.wait_minutes)} 分钟，范围 ${Math.floor(r.early_minutes)}—${Math.ceil(r.late_minutes)} 分钟`:'正在积累取号等待样本';$('store-wait-ai').textContent=r?.available?(r.ai_participated?'Flash 与当天趋势已参与调整':'历史等待与当天趋势参考'):'';}
 async function loadWait(){if(!config||config.getState().section!=='prediction'||$('store-wait-card')?.hidden||document.visibilityState!=='visible'||waitBusy||Date.now()-waitAt<30000)return;waitBusy=true;waitAt=Date.now();try{waitData=await get('/wait-reference.json');waitAdvice=await get('/model-advice.json');}catch(_){}finally{waitBusy=false;renderWait();}}
 async function records(force=false){
  if(!secure()||listBusy||document.visibilityState!=='visible'||!['tickets','intelligence'].includes(config.getState().section)||!force&&Date.now()-lastList<60000)return;
  listBusy=true;lastList=Date.now();const revision=recordRevision;
  try{
   const all=[],removed=new Set();
   for(let page=1;page<=10;page++){const value=await post('/api/v1/research/tickets',{action:'list',owner_secret:owner(),page,page_size:500});all.push(...value.records);for(const id of value.deleted_record_ids||[])removed.add(id);sampleReadState=value.has_more&&page===10?'partial':'ready';if(!value.has_more)break;}
   if(revision!==recordRevision){lastList=0;return;}
   $('my-ticket-status').textContent='';
   for(const id of removed){deletedIds.add(id);const item=items.find(i=>i.id===id);if(item)forgetItem(item);}
   for(const record of all){if(deletedIds.has(record.record_id))continue;let item=items.find(i=>i.id===record.record_id);if(!item){const t=record.reported;item={id:record.record_id,ticket:t,source:'sapi_miniapp_gateway',storeName:config.getState().storeNames?.[t.store_id]||t.store_id,mode:'algorithm',kind:'record',record:true,saved:true,follow:false,createdAt:record.received_at};items.push(item);}item.saved=true;item.serverResult=record.result;}
   persist();renderItems();root.SushiWaitAnalytics?.localChanged?.();
  }catch(_){sampleReadState='failed';$('my-ticket-status').textContent='服务器记录暂时无法读取';root.SushiWaitAnalytics?.localChanged?.();}finally{listBusy=false;}
 }
 function notify(){if(!config)return;root.SushiWaitScreenshots?.directoryChanged();sendPresence();renderWait();loadWait();if(config.getState().section==='intelligence')records();if(config.getState().section!=='tickets'||document.visibilityState!=='visible')return;records();for(const item of items){if(tableRows.has(item.id)||cards.has(item.id))updateCard(item);if(item.kind==='prediction'&&!item.arrived?.available&&(item.needsRecheck||(item.follow&&Date.now()>=item.nextAt)||(item.modelUntil>Date.now()&&Date.now()-(item.lastAt||0)>=30000)))refreshItem(item);}}
 function tab(record){$('ticket-predict-form').hidden=record;$('research-form').hidden=!record;for(const name of ['predict','record']){$('entry-tab-'+name).setAttribute('aria-selected',String(record===(name==='record')));$('entry-tab-'+name).tabIndex=record===(name==='record')?0:-1;}}
 async function importScreenshot(fields,settings){
  const at=new Date(Date.parse(fields.issued_at)+28800000).toISOString(),s=config.getState();
  const ticket=makeTicket({storeId:fields.store_id,queueType:fields.queue_type,checkedIn:fields.checked_in===null?'unknown':fields.checked_in?'yes':'no'},at.slice(0,10),at.slice(11,19),fields.number,settings.kind==='record'?'retrospective':'live');
  ticket.input_method=settings.inputMethod|| (settings.reviewed?'screenshot_reviewed':'screenshot_ocr');if(settings.imageSha)ticket.image_sha256=settings.imageSha;
  if(settings.kind==='prediction'&&at.slice(0,10)!==config.today())throw Error('today_only');
  if(!['auto','algorithm','nonthinking','thinking'].includes(settings.mode))throw Error('invalid_mode');
  if((settings.kind==='record'||settings.record)&&!secure())throw Error('https_required');
  const source='sapi_miniapp_gateway';let item=items.find(i=>i.kind===settings.kind&&i.source===source&&sameTicket(i.ticket,ticket));
  if(!item){if(items.filter(i=>!i.saved).length>=100)throw Error('ticket_limit');item={id:random(16),ticket,source,storeName:fields.store_name||s.storeNames?.[ticket.store_id]||ticket.store_id,mode:settings.mode,kind:settings.kind,follow:settings.kind==='prediction'&&settings.follow,record:settings.kind==='record'||settings.record,saved:false,createdAt:new Date().toISOString(),nextAt:0,result:null,arrived:null};items.unshift(item);}else{item.record=item.record||settings.record||settings.kind==='record';item.mode=settings.mode;item.follow=settings.kind==='prediction'&&settings.follow;}
  persist();renderItems();if(item.kind==='prediction')await refreshItem(item,true);
  if(item.record&&!item.saved){item.ticket.prediction_reason=item.kind==='prediction'&&!item.result?.available?item.result?.reason||'not_computed_yet':null;await saveItem(item);}
  updateCard(item);if(item.record&&!item.saved)throw Error(item.saveError||'save_failed');return {saved:item.saved,record:item.record,id:item.id};
 }
 let sampleReadState='loading';
 function recordSummary(from,to,queue){
  const result={},seen=new Set();
  for(const i of items){const t=i.ticket,key=[t.store_id,t.queue_type,Number(t.number),Date.parse(t.issued_at)].join('|');
   if(!i.saved||ticketDate(i)<from||ticketDate(i)>to||queue&&t.queue_type!==queue||seen.has(key))continue;seen.add(key);
   const r=result[t.store_id]||={records:0,completed:0,direct:0,inferred:0,conflicts:0,pending:0,unchecked:0,unknownCheckin:0,cells:{},days:[]};
   const d=new Date(Date.parse(t.issued_at)+28800000),m=d.getUTCHours()*60+d.getUTCMinutes(),kind=[0,6].includes(d.getUTCDay())?'weekend':'weekday',period=m>=690&&m<810?'lunch':m>=1080&&m<1200?'dinner':'other',label=i.serverResult;
   const valid=label?.result_available&&['arrived','inferred_arrived'].includes(label.state)&&!label.quality_flags?.includes('number_reversal');
   const cell=r.cells[kind+'_'+period]||={records:0,completed:0,days:[]};cell.records++;r.records++;
   if(!r.days.includes(ticketDate(i)))r.days.push(ticketDate(i));
   if(valid){cell.completed++;r.completed++;if(!cell.days.includes(ticketDate(i)))cell.days.push(ticketDate(i));r[label.state==='arrived'?'direct':'inferred']++;}
   else if(['input_conflict','number_cycle_uncertain'].includes(label?.state))r.conflicts++;else r.pending++;
   if(t.checked_in===false)r.unchecked++;if(t.checked_in===null)r.unknownCheckin++;
  }return result;
 }
 function screenshotSettings(){const s=config.getState();return {kind:root.SushiWaitScreenshots?.kind?.()||($('research-form').hidden?'prediction':'record'),today:config.today(),storeId:s.storeId,mode:$('prediction-mode').value||s.mode,record:$('ticket-record-system').checked,follow:$('ticket-follow').checked,storeNames:s.storeNames||{},cityById:config.cityById||{}};}
 function attach(value){config=value;restore();$('research-date').value=config.today();$('research-date').max=config.today();$('ticket-predict-form').onsubmit=e=>submitForm('prediction',e);$('research-form').onsubmit=e=>submitForm('record',e);if($('ticket-composer-form')?.dataset?.entryWorkflow!=='drafts-v2'){$('entry-tab-predict').onclick=()=>tab(false);$('entry-tab-record').onclick=()=>tab(true);for(const name of ['predict','record'])$('entry-tab-'+name).onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const record=e.key==='End'||e.key==='ArrowRight';tab(record);$('entry-tab-'+(record?'record':'predict')).focus();}};}$('open-my-tickets').onclick=()=>config.navigate('tickets');$('new-ticket').onclick=()=>config.navigate('prediction');$('research-refresh').onclick=()=>records(true);document.addEventListener('visibilitychange',notify);window.addEventListener('pagehide',()=>{for(const [id,lease]of leases){if(navigator.sendBeacon)navigator.sendBeacon('/api/v1/prediction/presence',new Blob([JSON.stringify({schema_version:1,...lease.value,visible:false})],{type:'application/json'}));leases.delete(id);}});tableSetup();renderItems();setInterval(notify,15000);notify();}
 root.SushiWaitResearch={attach,notify,makeTicket,submitEntry:importScreenshot,importScreenshot,screenshotSettings,showSuccess,openTickets:()=>config.navigate('tickets'),workflow:true,filterRecords:filteredItems,recordSummary,refreshRecords:()=>records(true),sampleStatus:()=>sampleReadState};
})(window);
