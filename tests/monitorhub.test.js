"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Element {
  constructor(){this.children=[];this.events={};this.textContent="";this.value="";this.disabled=false;}
  addEventListener(n,f){this.events[n]=f;} replaceChildren(){this.children=[];} append(...a){this.children.push(...a);}
  getBoundingClientRect(){return {width:480};} getContext(){return new Proxy({}, {get:()=>()=>{},set:()=>true});}
}
const elements={},calls=[],timers=new Map();let tick=0;
const element=id=>elements[id]||(elements[id]=new Element());
const at=new Date().toISOString();
const ctx=vm.createContext({document:{getElementById:element,createElement:()=>new Element()},
  window:{devicePixelRatio:1,addEventListener:()=>{}},getComputedStyle:()=>({getPropertyValue:()=>"#777"}),
  AbortSignal,Date,Promise,Error,Number,Object,Math,
  setTimeout:(f,d)=>{timers.set(++tick,{f,d});return tick;},clearTimeout:n=>timers.delete(n),
  fetch:async path=>{
    calls.push(path);assert.match(path,/^\/api\/v1\/(status|stores\/(900001|900003)\/(status|queue|history))$/);
    const store=path.includes("900003")?"900003":"900001",good=store==="900003";
    const value=path.endsWith("status")?{store_ids:["900001","900003"],store_names:{"900001":"第一店","900003":"第三店"},
      service_state:good?"running":"failed",worker_alive:good,task:{recorded_http_attempts:good?20:7}}
      :path.endsWith("history")?{requested_store_id:store,generated_at:at,retained_points:0,max_points:360,
        evicted_points_this_process:0,comparison_max_gap_seconds:120,points:[]}
      :{requested_store_id:store,fields:{groupqueues:{state:"unavailable"},storequeuecount:{state:"unavailable"}}};
    return {ok:true,json:async()=>value};}});
async function settle(){for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));}
(async()=>{
 vm.runInContext(fs.readFileSync(process.argv[2],"utf8"),ctx);await settle();
 assert.equal(elements.store.children[0].textContent,"第一店");assert.equal(elements.store.children[1].textContent,"第三店");
 assert.match(elements.state.textContent,/采集失败/);assert.equal(timers.size,0);
 elements.store.value="900003";elements.store.events.change();await settle();
 assert.ok(calls.includes("/api/v1/stores/900003/status"));assert.match(elements.state.textContent,/正在采集/);
 assert.match(elements.task.textContent,/当前门店所属批次累计 20/);assert.equal(timers.size,1);
 assert.ok(calls.every(p=>p.startsWith("/api/v1/")));assert.equal(calls.length,6);
})().catch(e=>{process.stderr.write(String(e));process.exitCode=1;});
