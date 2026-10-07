"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const source=fs.readFileSync(process.argv[2],"utf8");
class Element {
  constructor(){this.children=[];this.events={};this.textContent="";this.value="";this.draws=[];this.disabled=false;}
  addEventListener(name,callback){this.events[name]=callback;}
  replaceChildren(){this.children=[];}
  append(...items){this.children.push(...items);}
  getBoundingClientRect(){return {width:480};}
  getContext(){const draws=this.draws;return new Proxy({}, {get:(_,name)=> (...args)=>{draws.push([name,...args]);},set:()=>true});}
}
const base="2020-01-01T12:00:00Z",later="2020-01-01T12:00:30Z";
function point(at,ok=true){const query={attempted:true,ok,error_code:ok?null:"http_error",http_status:ok?200:503,started_at:at,received_at:at};return {pair_ok:ok,reported_count_raw:ok?7:null,queries:{groupqueues:query,storequeuecount:query},display_comparison:{state:ok?"comparable_display_sets":"insufficient",interval_seconds:ok?30:null,removed_labels:ok?{storeQueue:1,reservationQueue:0}:null}};}
async function setup({mismatch=false,future=false}={}) {
  const elements={},calls=[],timers=new Map(),events={};let failed=false,timer=0;
  const element=id=>elements[id]||(elements[id]=new Element());
  const field={state:"last_known_only",response_age_seconds:30,payload:{queues:{storeQueue:["12","13-1"],reservationQueue:[],counterQueue:[],boothQueue:[],mixedQueue:[]}}};
  const view={requested_store_id:mismatch?"900002":"900001",fields:{groupqueues:field,storequeuecount:{state:"last_known_only"}}};
  const history={requested_store_id:"900001",generated_at:later,retained_points:2,max_points:360,evicted_points_this_process:0,comparison_max_gap_seconds:120,points:[point(future?"2020-01-02T12:00:00Z":base),point(later,false)]};
  const status={store_ids:["900001"],service_state:"completed",worker_alive:false,task:{recorded_http_attempts:4}};
  const context=vm.createContext({document:{getElementById:element,createElement:()=>new Element()},
    window:{devicePixelRatio:1,addEventListener:(name,callback)=>{events[name]=callback;}},
    getComputedStyle:()=>({getPropertyValue:()=>"#777"}),AbortSignal,Date,Promise,Error,Number,Object,Math,
    setTimeout:(callback,delay)=>{timers.set(++timer,{callback,delay});return timer;},clearTimeout:id=>timers.delete(id),
    fetch:async(path)=>{calls.push(path);if(failed)throw new Error("private-server-message");
      assert.match(path,/^\/api\/v1\/(status|stores\/900001\/(queue|history))$/);
      return {ok:true,json:async()=>structuredClone(path.endsWith("status")?status:path.endsWith("history")?history:view)};}});
  vm.runInContext(source,context);
  // Finish the initial asynchronous same-origin refresh.
  for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));
  return {context,elements,calls,timers,events,fail:()=>{failed=true;}};
}
(async()=>{
  const app=await setup();
  assert.equal(app.calls.length,3);
  assert.equal(app.timers.size,0,"terminal collection must not schedule active refresh");
  assert.match(app.elements.message.textContent,/仅保留为历史/);
  assert.doesNotMatch(app.elements.task.textContent,/运行/,"completed task detail must not claim it is running");
  assert.match(app.elements["count-note"].textContent,/1 个有效点/);
  assert.match(app.elements.failures.textContent,/1／0/);
  assert.match(app.elements["numbers-state"].textContent,/仅保留上次成功信息/);
  app.elements.pause.events.click();
  assert.match(app.elements.message.textContent,/页面更新已暂停/);
  app.fail();await app.context.refresh();
  assert.match(app.elements.state.textContent,/状态未知/);
  const failedMessage=app.elements.message.textContent;
  app.events.resize();
  assert.equal(app.elements.message.textContent,failedMessage,"resize must not turn a failed read into fresh data");
  assert.doesNotMatch(failedMessage,/private-server-message/);
  const mismatch=await setup({mismatch:true});
  assert.match(mismatch.elements.state.textContent,/状态未知/);
  assert.equal(mismatch.elements.count?.draws.length||0,0,"wrong-store response must not be rendered");
  const future=await setup({future:true});
  assert.match(future.elements["count-note"].textContent,/缺失数据不会替换成 0/);
  assert.ok(future.elements.count.draws.some(row=>row[0]==="fillText"&&row[1]==="暂无可绘制的有效观测"));
})().catch(error=>{process.stderr.write(String(error));process.exitCode=1;});
