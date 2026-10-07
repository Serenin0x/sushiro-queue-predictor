/* Fixed same-origin reads only. No upstream requests, ticket input or fake data. */
"use strict";
const byId = id => document.getElementById(id);
const stateNames = {running:"正在采集",ready:"准备完成",starting:"正在启动",not_started:"尚未启动",completed:"采集已完成",failed:"采集失败",stopped:"采集已停止"};
const viewNames = {recent_response:"最近收到的响应",saved_history:"已保存历史",last_known_only:"仅保留上次成功信息",stale_response:"响应已过时",unavailable:"尚无成功响应",clock_invalid:"响应时间异常"};
const queueNames = {storeQueue:"堂食集合",reservationQueue:"预约集合",counterQueue:"吧台集合",boothQueue:"卡座集合",mixedQueue:"混合集合"};
let timer=null, busy=false, paused=false, selected="", cached=null, lastFailed=false;
function stamp(value) { const n=Date.parse(value); return Number.isFinite(n)?n:null; }
function timeLabel(value) { return new Date(value).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}); }
function pointTime(point,endpoint) { const q=point.queries[endpoint]; return stamp(q.received_at||q.started_at); }
function plot(id,points,color,maxGap) {
  const canvas=byId(id),rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
  const width=Math.max(220,rect.width),height=220;
  canvas.width=width*ratio; canvas.height=height*ratio;
  const ctx=canvas.getContext("2d"); ctx.scale(ratio,ratio);
  const style=getComputedStyle(document.documentElement),text=style.getPropertyValue("--muted").trim();
  const line=style.getPropertyValue("--line").trim();
  const valid=points.filter(p=>p.t!==null&&p.y!==null&&Number.isFinite(p.y));
  const note=byId(id+"-note");
  if(!valid.length) { ctx.fillStyle=text;ctx.font="14px system-ui";ctx.fillText("暂无可绘制的有效观测",24,105);note.textContent="缺失数据不会替换成 0";return; }
  const times=points.filter(p=>p.t!==null).map(p=>p.t);
  const first=Math.min(...times),last=Math.max(...times),span=Math.max(1,last-first);
  const high=Math.max(1,...valid.map(p=>p.y)),left=48,right=width-14,top=14,bottom=178;
  const x=t=>left+(t-first)/span*(right-left), y=v=>bottom-v/high*(bottom-top);
  ctx.font="11px system-ui";ctx.lineWidth=1;
  for(let i=0;i<=4;i++) {const value=high*i/4,yy=y(value);ctx.strokeStyle=line;ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();ctx.fillStyle=text;ctx.fillText(value.toFixed(high<=4?1:0),4,yy+4);}
  ctx.fillStyle=text;ctx.fillText(timeLabel(first),left,202);ctx.textAlign="right";ctx.fillText(timeLabel(last),right,202);ctx.textAlign="left";
  let previous=null;
  for(const p of points) {
    if(p.t===null||p.y===null||!Number.isFinite(p.y)) {previous=null;continue;}
    ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=1.8;
    if(previous&&p.t>previous.t&&(p.t-previous.t)<=maxGap*1000) {ctx.beginPath();ctx.moveTo(x(previous.t),y(previous.y));ctx.lineTo(x(p.t),y(p.y));ctx.stroke();}
    ctx.beginPath();ctx.arc(x(p.t),y(p.y),2.3,0,Math.PI*2);ctx.fill();previous=p;
  }
  note.textContent=`${valid.length} 个有效点 · 最近 ${valid[valid.length-1].y} · ${new Date(first).toLocaleDateString("zh-CN")}—${new Date(last).toLocaleDateString("zh-CN")}`;
}
function render(data) {
  const {status,view,history}=data,points=history.points,fields=view.fields;
  byId("state").textContent=stateNames[status.service_state]||"状态未知";
  const task=status.task||{};
  const total=task.recorded_http_attempts;
  byId("task").textContent=Number.isInteger(total)?`本任务累计 ${total} 次查询；原期限及预算保持`:"等待任务状态";
  byId("samples").textContent=`${history.retained_points} 组`;
  byId("history-note").textContent=`保留最近最多 ${history.max_points} 组；本进程已移出 ${history.evicted_points_this_process} 组，不是完整历史`;
  const q=fields.groupqueues,c=fields.storequeuecount;
  byId("age").textContent=q.response_age_seconds===null?"未知":`${Math.round(q.response_age_seconds)} 秒`;
  const failed=points.filter(p=>!p.pair_ok).length,gaps=points.filter(p=>p.display_comparison.state==="gap").length;
  byId("failures").textContent=`${failed}／${gaps}`;
  byId("numbers-state").textContent=`${viewNames[q.state]||"状态未知"} · 源更新时间未知`;
  byId("numbers").replaceChildren();
  for(const [name,label] of Object.entries(queueNames)) {const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;const values=q.payload?.queues?.[name];dd.textContent=values===undefined?"未获得":values.length?values.join("、"):"该响应展示为空";byId("numbers").append(dt,dd);}
  const now=stamp(history.generated_at);
  const values=(endpoint,pick)=>points.map(p=>{const t=pointTime(p,endpoint);return {t,y:t===null||now===null||t>now?null:pick(p)};});
  plot("count",values("storequeuecount",p=>p.reported_count_raw),"#bc3f49",history.comparison_max_gap_seconds);
  plot("ordinary",values("groupqueues",p=>p.display_comparison.removed_labels?.storeQueue??null),"#bc3f49",history.comparison_max_gap_seconds);
  plot("reservation",values("groupqueues",p=>p.display_comparison.removed_labels?.reservationQueue??null),"#be8039",history.comparison_max_gap_seconds);
  plot("interval",values("groupqueues",p=>p.display_comparison.interval_seconds>0?p.display_comparison.interval_seconds:null),"#397989",history.comparison_max_gap_seconds);
  byId("issues").replaceChildren();
  const issues=points.filter(p=>!p.pair_ok||p.display_comparison.state==="gap");
  for(const p of issues.slice(-30)) {const item=document.createElement("li"),at=pointTime(p,"groupqueues");const errors=Object.entries(p.queries).filter(([,q])=>!q.ok).map(([name,q])=>`${name}：${q.attempted?"查询失败":"未查询"} ${q.error_code||"未知"}`);item.textContent=`${at===null?"时间未知":new Date(at).toLocaleString("zh-CN")} · ${errors.join("；")||`响应间隔 ${p.display_comparison.interval_seconds} 秒`}`;byId("issues").append(item);}
  if(!issues.length) {const item=document.createElement("li");item.textContent="保留窗口内未记录失败或较长缺口；这不证明窗口外或源数据的新鲜度。";byId("issues").append(item);}
  const active=status.service_state==="running"&&status.worker_alive;
  byId("message").textContent=`${paused?"页面更新已暂停；以下是上次读取的信息。 ":""}${active?"最近读取时采集运行中":"采集未在运行，曲线仅保留为历史"} · 读取于 ${new Date(data.loadedAt).toLocaleTimeString("zh-CN")} · 数量响应：${viewNames[c.state]||"状态未知"}`;
}
async function getJSON(path) {const response=await fetch(path,{cache:"no-store",signal:AbortSignal.timeout(8000)});if(!response.ok)throw new Error("local_read_failed");return response.json();}
async function refresh() {
  if(busy)return;busy=true;clearTimeout(timer);byId("refresh").disabled=true;byId("store").disabled=true;
  try {
    const status=await getJSON("/api/v1/status");
    if(!status.store_ids.includes(selected)) {selected=status.store_ids[0]||"";byId("store").replaceChildren();for(const id of status.store_ids){const option=document.createElement("option");option.value=id;option.textContent=`门店 ${id}`;byId("store").append(option);}}
    if(!selected)throw new Error("no_store");byId("store").value=selected;
    const [view,history]=await Promise.all([getJSON(`/api/v1/stores/${encodeURIComponent(selected)}/queue`),getJSON(`/api/v1/stores/${encodeURIComponent(selected)}/history`)]);
    if(view.requested_store_id!==selected||history.requested_store_id!==selected)throw new Error("store_mismatch");
    cached={status,view,history,loadedAt:Date.now()};lastFailed=false;render(cached);
    if(!paused&&status.service_state==="running"&&status.worker_alive)timer=setTimeout(refresh,5000);
  } catch(error) {lastFailed=true;byId("message").textContent="统计服务读取失败；已有曲线保留为历史，当前采集状态未知。";byId("state").textContent="读取失败 · 状态未知";byId("numbers-state").textContent="仅保留页面上次获得的信息";if(!paused)timer=setTimeout(refresh,5000);}
  finally {busy=false;byId("refresh").disabled=false;byId("store").disabled=false;}
}
byId("store").addEventListener("change",()=>{selected=byId("store").value;cached=null;refresh();});
byId("refresh").addEventListener("click",refresh);
byId("pause").addEventListener("click",()=>{paused=!paused;clearTimeout(timer);byId("pause").textContent=paused?"继续页面更新":"暂停页面更新";if(!paused)refresh();else if(cached&&!lastFailed)render(cached);});
window.addEventListener("resize",()=>{if(cached&&!lastFailed)render(cached);});
refresh();
