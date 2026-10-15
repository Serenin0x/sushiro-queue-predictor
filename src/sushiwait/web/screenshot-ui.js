"use strict";
// OCR only fills editable drafts; explicit submission owns all side effects.
(function(){
 const $=id=>document.getElementById(id),drafts={prediction:[],record:[]};
 let current='prediction',recognizing=false,submitting=false,sequence=0,directorySignature='';
 const MAX_ROWS=20,MAX_SIZE=5*1024*1024,settings=()=>window.SushiWaitResearch.screenshotSettings();
 function element(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
 function option(select,value,text){const n=element('option','',text);n.value=value;select.append(n);}
 function field(row,key,title,type='text',values=null){
  const label=element('label','composer-field field-'+key),caption=element('span','',title),n=element(values?'select':'input');
  n.id=`draft-${row.id}-${key}`;n.name=key;
  if(values)for(const [value,text]of values)option(n,value,text);else{n.type=type;n.autocomplete='off';}
  if(type==='time')n.step='60';
  label.append(caption,n);row.fields[key]=n;
  n.addEventListener(values?'change':'input',()=>{row.dirty.add(key);n.removeAttribute('aria-invalid');row.status.textContent='';if(['date','time'].includes(key))row.preciseAt=null;if(key==='city'){populateStores(row);row.dirty.add('store');}updateControls();});return label;
 }
 function populateStores(row,selected=''){
  const cfg=settings(),f=row.fields;f.store.replaceChildren();option(f.store,'','选择门店');
  for(const [id,name]of Object.entries(cfg.storeNames).filter(([id])=>(cfg.cityById[id]||'城市待核')===f.city.value).sort((a,b)=>a[1].localeCompare(b[1],'zh-CN')))option(f.store,id,name);
  if(selected&&!Object.hasOwn(cfg.storeNames,selected)&&row.suggestedName)option(f.store,selected,row.suggestedName);
  f.store.value=selected;f.store.disabled=!f.city.value;
 }
 function populateCities(row,selectedStore=''){
  const cfg=settings(),f=row.fields,oldCity=f.city.value,cities=[...new Set(Object.keys(cfg.storeNames).map(id=>cfg.cityById[id]||'城市待核'))].sort((a,b)=>a.localeCompare(b,'zh-CN'));
  f.city.replaceChildren();option(f.city,'','选择城市');for(const city of cities)option(f.city,city,city);
  const city=selectedStore?(cfg.cityById[selectedStore]||'城市待核'):oldCity;if(city&&!cities.includes(city))option(f.city,city,city);
  f.city.value=city||'';populateStores(row,selectedStore);
 }
 function isBlank(row){return !row.file&&!row.fields.number.value&&!row.fields.time.value;}
 function makeRow(kind){
  const row={id:++sequence,kind,fields:{},dirty:new Set(),preciseAt:null,file:null,imageUrl:null,imageSha:null,suggestedName:null};
  row.el=element('section','composer-row');row.el.dataset.draftId=String(row.id);
  const heading=element('div','composer-row-heading');row.ordinal=element('strong','composer-ordinal');row.status=element('span','composer-row-status');row.status.setAttribute('role','status');
  row.remove=element('button','composer-remove','移除');row.remove.type='button';row.remove.onclick=()=>{if(submitting)return;removeRow(row);if(!drafts[kind].length)makeRow(kind);render();};heading.append(row.ordinal,row.status,row.remove);
  row.preview=element('img','composer-thumbnail');row.preview.hidden=true;row.preview.alt='取号截图';
  const grid=element('div','composer-fields');grid.append(field(row,'city','城市','text',[]),field(row,'store','门店','text',[]),field(row,'number','号码'),field(row,'date','取号日期','date'),field(row,'time','取号时间','time'),field(row,'queue','队列','text',[['ordinary','堂食'],['reservation','预约']]),field(row,'checkin','签到状态','text',[['unknown','未知'],['no','未签到'],['yes','已签到']]));
  row.fields.number.inputMode='numeric';row.fields.number.maxLength=7;row.fields.number.placeholder='填写号码';row.fields.date.value=settings().today;row.fields.date.max=settings().today;
  row.el.append(heading,row.preview,grid);drafts[kind].push(row);populateCities(row);return row;
 }
 function removeRow(row){const list=drafts[row.kind],index=list.indexOf(row);if(index>=0)list.splice(index,1);if(row.imageUrl)URL.revokeObjectURL(row.imageUrl);row.el.remove();}
 function updateControls(){
  const count=drafts[current].filter(r=>!isBlank(r)).length;
  $('composer-add').disabled=submitting||drafts[current].length>=MAX_ROWS;$('screenshot-select').disabled=submitting||recognizing;$('composer-submit').disabled=submitting||recognizing;
  $('composer-submit-label').textContent=submitting?'正在提交':current==='prediction'?count>1?`提交 ${count} 个预测`:'提交预测':count>1?`保存 ${count} 条记录`:'保存记录';
  for(const row of drafts[current])row.remove.disabled=submitting;
 }
 function render(){
  if(!drafts[current].length)makeRow(current);
  $('composer-rows').replaceChildren(...drafts[current].map((row,i)=>{row.ordinal.textContent=drafts[current].length===1?'号码信息':`号码 ${i+1}`;return row.el;}));
  $('composer-prediction-options').hidden=current==='record';$('composer-instruction').textContent=current==='prediction'?'填写号码，或拖入截图自动填入':'可批量上传截图，也可逐条填写';
  $('ticket-composer-panel').setAttribute('aria-labelledby',current==='prediction'?'entry-tab-predict':'entry-tab-record');
  for(const [id,kind]of [['entry-tab-predict','prediction'],['entry-tab-record','record']]){$(id).setAttribute('aria-selected',String(current===kind));$(id).tabIndex=current===kind?0:-1;}updateControls();
 }
 function switchTab(kind){if(submitting)return;current=kind;$('composer-status').textContent='';render();}
 function directoryChanged(){
  if(!$('ticket-composer-form')||!window.SushiWaitResearch)return;
  const today=settings().today;for(const list of Object.values(drafts))for(const row of list){row.fields.date.max=today;if(isBlank(row)&&!row.dirty.has('date'))row.fields.date.value=today;}
  const signature=JSON.stringify(settings().storeNames);if(signature===directorySignature)return;directorySignature=signature;
  for(const list of Object.values(drafts))for(const row of list)populateCities(row,row.fields.store.value);
 }
 function markError(row,key,text){row.status.textContent=text;row.fields[key].setAttribute('aria-invalid','true');return null;}
 function payload(row){
  const f=row.fields,cfg=settings();if(!f.store.value)return markError(row,'store','请选择门店');
  if(!/^\d{1,7}$/.test(f.number.value)||Number(f.number.value)<1)return markError(row,'number','请填写有效号码');
  if(!/^\d{4}-\d{2}-\d{2}$/.test(f.date.value))return markError(row,'date','请填写取号日期');
  if(!/^\d{2}:\d{2}$/.test(f.time.value))return markError(row,'time','请填写取号时间');
  const at=Date.parse(`${f.date.value}T${f.time.value}:00+08:00`);
  if(!Number.isFinite(at)||new Date(at+28800000).toISOString().slice(0,16)!==`${f.date.value}T${f.time.value}`||at>Date.now())return markError(row,'time','请检查日期和时间，不能晚于现在');
  if(row.kind==='prediction'&&f.date.value!==cfg.today)return markError(row,'date','历史号码请切换到录入记录');
  return {store_id:f.store.value,store_name:cfg.storeNames[f.store.value]||row.suggestedName||f.store.value,number:f.number.value,issued_at:row.preciseAt||new Date(at).toISOString(),queue_type:f.queue.value,checked_in:f.checkin.value==='unknown'?null:f.checkin.value==='yes'};
 }
 function fill(row,response){
  const f=response?.fields||{},set=(key,value)=>{if(value!==undefined&&value!==null&&!row.dirty.has(key))row.fields[key].value=value;};
  row.suggestedName=response.store_name;row.imageSha=/^[0-9a-f]{64}$/.test(response.image_sha256||'')?response.image_sha256:null;
  if(!row.dirty.has('city')&&!row.dirty.has('store')){
   if(!f.store_id)row.fields.city.value='';
   populateCities(row,f.store_id||'');
  }
  set('number',f.number);set('queue',f.queue_type);set('checkin',f.checked_in===null?'unknown':f.checked_in===undefined?undefined:f.checked_in?'yes':'no');
  if(Number.isFinite(Date.parse(f.issued_at))){const local=new Date(Date.parse(f.issued_at)+28800000).toISOString();set('date',local.slice(0,10));set('time',local.slice(11,16));if(!row.dirty.has('date')&&!row.dirty.has('time'))row.preciseAt=new Date(f.issued_at).toISOString();}
  row.status.textContent=!f.store_id?'门店未确认，请选择城市和门店':response.auto_accept?'已填入，可修改':'请核对识别结果';
 }
 async function upload(files){
  if(recognizing||submitting)return;const selected=[...files];if(!selected.length)return;
  const kind=current,list=drafts[kind],blank=list.filter(isBlank);
  if(selected.length>MAX_ROWS||list.length-blank.length+selected.length>MAX_ROWS){$('composer-status').textContent='每批最多20条，现有填写已保留';return;}
  if(location.protocol!=='https:'&&!['127.0.0.1','localhost'].includes(location.hostname)){$('composer-status').textContent='请使用 HTTPS 加密入口';return;}
  recognizing=true;$('composer-status').textContent='';
  const queue=selected.map(file=>{const row=blank.shift()||makeRow(kind);row.dirty.clear();row.file=file;row.status.textContent='等待识别';if(['image/png','image/jpeg'].includes(file.type)&&file.size<=MAX_SIZE){row.imageUrl=URL.createObjectURL(file);row.preview.src=row.imageUrl;row.preview.hidden=false;}return row;});render();
  for(const row of queue){
   if(!drafts[kind].includes(row))continue;
   if(!['image/png','image/jpeg'].includes(row.file.type)||row.file.size>MAX_SIZE){row.status.textContent='请选择5MB内的PNG或JPEG，也可直接填写';continue;}
   row.status.textContent='正在识别';
   try{const r=await fetch('/api/v1/research/screenshots/recognize',{method:'POST',headers:{'Content-Type':row.file.type},body:row.file,cache:'no-store',signal:AbortSignal.timeout(40000)});if(!r.ok)throw Error('ocr_failed');const value=await r.json();if(drafts[kind].includes(row))fill(row,value);}
   catch(_){if(drafts[kind].includes(row))row.status.textContent='识别失败，可直接填写';}
  }
  recognizing=false;$('screenshot-files').value='';updateControls();
 }
 async function submit(event){
  event?.preventDefault();if(submitting||recognizing)return;const kind=current,candidates=drafts[kind].filter(r=>!isBlank(r));
  if(!candidates.length){$('composer-status').textContent='先填写号码，或上传截图';drafts[kind][0].fields.number.focus();return;}
  const prepared=candidates.map(row=>({row,fields:payload(row)})),invalid=prepared.find(p=>!p.fields);
  if(invalid){$('composer-status').textContent='请补全标出的信息';Object.values(invalid.row.fields).find(n=>n.getAttribute('aria-invalid')==='true')?.focus();return;}
  const opts={kind,mode:$('prediction-mode').value,record:kind==='record'||$('ticket-record-system').checked,follow:kind==='prediction'&&$('ticket-follow').checked};
  submitting=true;updateControls();$('composer-status').textContent='';
  const controls=[$('prediction-mode'),$('ticket-record-system'),$('ticket-follow'),...prepared.flatMap(p=>Object.values(p.row.fields))];controls.forEach(n=>n.disabled=true);let saved=0;
  for(const {row,fields}of prepared){row.status.textContent='正在提交';try{await window.SushiWaitResearch.submitEntry(fields,{...opts,inputMethod:row.file?'screenshot_reviewed':'manual',reviewed:Boolean(row.file),imageSha:row.imageSha});saved++;removeRow(row);}catch(e){row.status.textContent=e.message==='https_required'?'请使用 HTTPS 加密入口':e.message==='ticket_limit'?'当前浏览器已达到号码上限':'保存失败，信息已保留，可重试';}}
  controls.forEach(n=>n.disabled=false);submitting=false;render();$('composer-status').textContent=saved?`已${kind==='record'?'保存':'提交'} ${saved} 条${saved<prepared.length?`，${prepared.length-saved} 条需重试`:''}`:'提交未完成，信息已保留';
  if(saved)window.SushiWaitResearch.showSuccess();if(kind==='prediction'&&saved===prepared.length)window.SushiWaitResearch.openTickets();
 }
 function attach(){
  if(!$('ticket-composer-form'))return;$('ticket-composer-form').onsubmit=submit;
  $('composer-add').onclick=()=>{if(submitting||drafts[current].length>=MAX_ROWS)return;const row=makeRow(current);render();row.fields.city.focus();};
  $('screenshot-select').onclick=()=>{$('screenshot-files').value='';$('screenshot-files').click();};$('screenshot-files').onchange=()=>upload($('screenshot-files').files);
  $('entry-tab-predict').onclick=()=>switchTab('prediction');$('entry-tab-record').onclick=()=>switchTab('record');
  for(const id of ['entry-tab-predict','entry-tab-record'])$(id).onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();switchTab(['ArrowRight','End'].includes(e.key)?'record':'prediction');$(current==='record'?'entry-tab-record':'entry-tab-predict').focus();};
  const drop=$('ticket-composer-form');for(const name of ['dragenter','dragover'])drop.addEventListener(name,e=>{e.preventDefault();if(!submitting)drop.classList.add('composer-dragging');});drop.addEventListener('dragleave',()=>drop.classList.remove('composer-dragging'));drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('composer-dragging');upload(e.dataTransfer.files);});
  window.addEventListener('pagehide',()=>{for(const list of Object.values(drafts))for(const row of list)if(row.imageUrl)URL.revokeObjectURL(row.imageUrl);});render();directoryChanged();
 }
 document.addEventListener('DOMContentLoaded',attach);window.SushiWaitScreenshots={upload,submit,kind:()=>current,directoryChanged};
})();
