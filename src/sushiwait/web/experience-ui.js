"use strict";
(function(){
  const $=id=>document.getElementById(id);let bundle=null,storeLabel="",downloaded=false,mutation=0;
  const names={issued:"取号",checked_in:"签到",called:"首次实际叫号",seated:"入座",no_show:"本人确认过号",cancelled:"取消",observation_ended:"停止观察"};
  const time=v=>new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",dateStyle:"short",timeStyle:"medium",hour12:false}).format(new Date(v));
  const errors={secure_random_unavailable:"当前浏览器不支持安全生成记录标识，暂时无法创建草稿。",event_range_required:"请同时填写时间下限和上限；未知时间先不记录。",invalid_event_time:"时间范围矛盾、超出当前时间或与已有事件顺序不符，请核对。",invalid_revision_chain:"草稿修订链不完整或发生更改，请核对原下载文件。",bundle_too_large:"草稿文件过大，未下载。"};
  function error(e){$("experience-status").textContent=errors[e.message]||"草稿格式、事件或门店信息不兼容，未保存此次操作。";}
  function render(){
    const episode=bundle?.revisions.at(-1),terminal=episode?.events.some(e=>["seated","no_show","cancelled","observation_ended"].includes(e.event_type));
    $("experience-start").disabled=!!bundle;$("experience-download").disabled=!bundle;
    $("experience-now").disabled=!bundle||terminal;$("experience-add").disabled=!bundle||terminal;
    ["queue","party","table"].forEach(id=>$("experience-"+id).disabled=!!bundle);
    $("experience-scope").textContent=episode?`正在记录：${storeLabel||episode.store_id}，${{ordinary:"堂食",reservation:"预约",unknown:"队列未知"}[episode.queue_type]}；切换统计门店不会更改这份草稿。`:"先选择取号门店，再创建本人的草稿。";
    $("experience-events").replaceChildren();
    for(const e of episode?.events||[]){const li=document.createElement("li");li.textContent=`${names[e.event_type]}：${time(e.event_time_lower)}—${time(e.event_time_upper)}（本人声明，未审核）`;$("experience-events").append(li);}
  }
  function interval(){return window.SushiWaitExperience.localInterval($("experience-lower").value,$("experience-upper").value,Date.now());}
  function append(nowEvent){
    try{
      if(!$("experience-confirm").checked)throw Error("confirm_observation");
      const now=Date.now(),range=nowEvent?{lower:new Date(now).toISOString(),upper:new Date(now).toISOString()}:interval();
      bundle=window.SushiWaitExperience.append(bundle,{kind:$("experience-kind").value,...range,now});downloaded=false;mutation++;$("experience-saved").checked=false;
      $("experience-status").textContent="事件已记入当前页面的私有草稿，尚未上传或审核；请下载保存。";render();
    }catch(e){error(e);}
  }
  errors.confirm_observation="请确认这是本人实际观察的事件，不能根据展示号消失代填。";
  $("experience-start").onclick=()=>{
    try{
      if(!$("experience-confirm").checked)throw Error("confirm_observation");
      const store=$("store").value;if(!store)throw Error("store_required");
      const range=interval();
      bundle=window.SushiWaitExperience.begin({storeId:store,queueType:$("experience-queue").value,
        partySize:$("experience-party").value?Number($("experience-party").value):null,
        tableType:$("experience-table").value,...range,now:Date.now()});
      const select=$("store");storeLabel=select.options?.[select.selectedIndex]?.textContent||store;downloaded=false;mutation++;$("experience-saved").checked=false;
      $("experience-status").textContent="取号范围已记录；后续只记录本人实际发生的事件。";render();
    }catch(e){error(e);}
  };
  errors.store_required="请先在上方选择本次取号门店。";
  $("experience-add").onclick=()=>append(false);$("experience-now").onclick=()=>append(true);
  $("experience-download").onclick=()=>{
    try{
      const body=window.SushiWaitExperience.exportBundle(bundle,Date.now()),url=URL.createObjectURL(new Blob([body],{type:"application/json"})),a=document.createElement("a");
      a.href=url;a.download=`sushiwait-experience-${bundle.revisions[0].episode_id}-r${bundle.revisions.length}.json`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
      downloaded=true;$("experience-status").textContent="已请求下载全部修订，请确认文件保存成功。文件保留本人用餐时间，不要发布到公开统计或GitHub；尚未进入接收库和审核。";
    }catch(e){error(e);}
  };
  $("experience-import").onchange=async()=>{
    const file=$("experience-import").files?.[0];if(!file)return;
    const request=++mutation;
    try{
      if(bundle)throw Error("clear_before_import");if(file.size>16384)throw Error("bundle_too_large");
      const text=await file.text();if(request!==mutation)return;
      const value=window.SushiWaitExperience.validateBundle(JSON.parse(text),Date.now());
      if(value.revisions[0].data_origin!=="self_reported")throw Error("invalid_bundle");
      bundle=value;storeLabel="";downloaded=true;$("experience-saved").checked=false;$("experience-status").textContent="本机草稿已读入当前页面；没有上传，也没有自动审核。";render();
    }catch(e){if(request===mutation)error(e);}finally{if(request===mutation)$("experience-import").value="";}
  };
  errors.clear_before_import="已有草稿，请先下载并清空，再读入其他草稿。";
  $("experience-clear").onclick=()=>{if(bundle&&(!downloaded||!$("experience-saved").checked)){$("experience-status").textContent="请先下载，并确认文件已保存，再清空；关闭页面会丢失未保存的草稿。";return;}mutation++;bundle=null;storeLabel="";$("experience-confirm").checked=false;$("experience-saved").checked=false;$("experience-lower").value="";$("experience-upper").value="";$("experience-party").value="";$("experience-queue").value="unknown";$("experience-table").value="unknown";$("experience-kind").value="checked_in";$("experience-status").textContent="页面草稿已清空；已确认保存的文件仍由你保管。";render();};
  window.addEventListener("beforeunload",e=>{if(bundle&&(!downloaded||!$("experience-saved").checked)){e.preventDefault();e.returnValue="";}});
  if(!window.SushiWaitExperience){$("experience-status").textContent="经历记录模块未加载，暂不记录。";["start","add","now","download","clear"].forEach(id=>$("experience-"+id).disabled=true);return;}
  render();
})();
