/* Private caller observations. No network, storage, automatic event inference or training. */
(function(root){
  "use strict";
  const PURPOSE="sushiwait_experience_revisions_v1",TYPES=["issued","checked_in","called","seated","no_show","cancelled","observation_ended"],TERMINALS=["seated","no_show","cancelled","observation_ended"];
  const SCOPE=["episode_id","store_id","api_profile","data_origin","queue_type","party_size","table_type"];
  const FIELDS=["schema_version",...SCOPE,"revision","supersedes_revision","recorded_at","events"];
  const EVENT_FIELDS=["event_id","event_type","event_time_lower","event_time_upper","observed_at","evidence_kind","verification_status"];
  const fail=code=>{throw Error(code);},copy=x=>JSON.parse(JSON.stringify(x));
  function exact(value,keys){return value&&typeof value==="object"&&!Array.isArray(value)&&Object.keys(value).length===keys.length&&keys.every(k=>Object.hasOwn(value,k));}
  function stamp(value){if(typeof value!=="string"||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(value))return NaN;const n=Date.parse(value);return Number.isFinite(n)&&new Date(n).toISOString().slice(0,19)===value.slice(0,19)?n:NaN;}
  function id(value){return typeof value==="string"&&/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);}
  function uuid(){if(!root.crypto?.getRandomValues)fail("secure_random_unavailable");const bytes=root.crypto.getRandomValues(new Uint8Array(16));bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const h=Array.from(bytes,b=>b.toString(16).padStart(2,"0")).join("");return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;}
  function validateEpisode(value,now){
    if(!Number.isFinite(now)||!exact(value,FIELDS)||value.schema_version!==1||!id(value.episode_id)||
      !Number.isInteger(value.revision)||value.revision<1||value.revision>7||value.supersedes_revision!==(value.revision===1?null:value.revision-1)||
      typeof value.store_id!=="string"||!(/^[1-9][0-9]{0,9}$/.test(value.store_id))||Number(value.store_id)>2147483647||
      value.api_profile!=="miniapp_gateway"||!["self_reported","synthetic"].includes(value.data_origin)||
      !["ordinary","reservation","unknown"].includes(value.queue_type)||!["booth","counter","either","unknown"].includes(value.table_type)||
      value.party_size!==null&&(!Number.isInteger(value.party_size)||value.party_size<1||value.party_size>2147483647)||
      !Array.isArray(value.events)||value.events.length<1||value.events.length>7)fail("invalid_experience");
    const recorded=stamp(value.recorded_at),kinds=new Set(),ids=new Set();let earliest=-Infinity,terminal=false;
    if(!Number.isFinite(recorded)||recorded>now)fail("invalid_event_time");
    value.events.forEach((event,i)=>{
      if(!exact(event,EVENT_FIELDS)||!id(event.event_id)||ids.has(event.event_id)||!TYPES.includes(event.event_type)||kinds.has(event.event_type)||
        event.verification_status!=="unverified"||event.evidence_kind!==(value.data_origin==="synthetic"?"synthetic":"self_observation")||
        i===0&&event.event_type!=="issued"||terminal)fail("invalid_experience");
      const lo=stamp(event.event_time_lower),hi=stamp(event.event_time_upper),observed=stamp(event.observed_at);earliest=Math.max(earliest,lo);
      if(!Number.isFinite(lo)||!Number.isFinite(hi)||!Number.isFinite(observed)||earliest>hi||lo>hi||hi>observed||observed>recorded)fail("invalid_event_time");
      kinds.add(event.event_type);ids.add(event.event_id);terminal=TERMINALS.includes(event.event_type);
    });return copy(value);
  }
  function validateBundle(value,now){
    if(!exact(value,["schema_version","purpose","exported_at","revisions"])||value.schema_version!==1||value.purpose!==PURPOSE||
      !Array.isArray(value.revisions)||!value.revisions.length||value.revisions.length>7)fail("invalid_bundle");
    const exported=stamp(value.exported_at);if(!Number.isFinite(exported)||exported>now)fail("invalid_event_time");
    let previous=null;
    value.revisions.forEach((record,i)=>{
      const current=validateEpisode(record,now);
      if(current.revision!==i+1||stamp(current.recorded_at)>exported||previous&&(
        SCOPE.some(k=>current[k]!==previous[k])||stamp(current.recorded_at)<stamp(previous.recorded_at)||
        current.events.length!==previous.events.length+1||current.events.slice(0,-1).some((e,j)=>EVENT_FIELDS.some(k=>e[k]!==previous.events[j][k]))))fail("invalid_revision_chain");
      if(!previous&&current.events.length!==1)fail("invalid_revision_chain");previous=current;
    });return copy(value);
  }
  function event(kind,lower,upper,now,origin){return {event_id:uuid(),event_type:kind,event_time_lower:lower,event_time_upper:upper,observed_at:new Date(now).toISOString(),evidence_kind:origin==="synthetic"?"synthetic":"self_observation",verification_status:"unverified"};}
  function begin(options){
    const {storeId,queueType,partySize=null,tableType="unknown",dataOrigin="self_reported",lower,upper,now}=options;
    const episode={schema_version:1,episode_id:uuid(),store_id:storeId,api_profile:"miniapp_gateway",data_origin:dataOrigin,queue_type:queueType,party_size:partySize,table_type:tableType,revision:1,supersedes_revision:null,recorded_at:new Date(now).toISOString(),events:[event("issued",lower,upper,now,dataOrigin)]};
    return validateBundle({schema_version:1,purpose:PURPOSE,exported_at:new Date(now).toISOString(),revisions:[episode]},now);
  }
  function append(bundle,{kind,lower,upper,now}){
    const result=validateBundle(bundle,now),previous=result.revisions.at(-1);if(kind==="issued"||result.revisions.length>=7)fail("invalid_experience");
    const next=copy(previous);next.revision++;next.supersedes_revision=previous.revision;next.recorded_at=new Date(now).toISOString();next.events.push(event(kind,lower,upper,now,next.data_origin));
    result.revisions.push(next);result.exported_at=new Date(now).toISOString();return validateBundle(result,now);
  }
  function exportBundle(bundle,now){const value=validateBundle(bundle,now);value.exported_at=new Date(now).toISOString();const text=JSON.stringify(value,null,2);if(new TextEncoder().encode(text).length>16384)fail("bundle_too_large");return text;}
  function localInterval(lower,upper,now){
    function parse(value,end){
      if(typeof value!=="string"||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value))fail("event_range_required");
      const full=value.length===16?value+":00":value,n=Date.parse(full+"+08:00");
      if(!Number.isFinite(n)||new Date(n+28800000).toISOString().slice(0,19)!==full||n>now)fail("invalid_event_time");
      return Math.min(now,n+(end?(value.length===16?59999:999):0));
    }
    const lo=parse(lower,false),hi=parse(upper,true);if(lo>hi)fail("invalid_event_time");return {lower:new Date(lo).toISOString(),upper:new Date(hi).toISOString()};
  }
  const api={PURPOSE,begin,append,validateBundle,exportBundle,localInterval};
  if(typeof module==="object"&&module.exports)module.exports=api;else root.SushiWaitExperience=api;
})(typeof globalThis!=="undefined"?globalThis:this);
