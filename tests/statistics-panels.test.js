"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.value="";this.hidden=false;this.textContent="";this.dataset={};this.attributes={};const names=new Set();this.classList={add:n=>names.add(n),toggle(n,on){on?names.add(n):names.delete(n);},contains:n=>names.has(n)};}
  append(e){this.children.push(e);}replaceChildren(){this.children=[];}
  setAttribute(k,v){this.attributes[k]=v;}removeAttribute(k){delete this.attributes[k];}
  getBoundingClientRect(){return {width:600};}getContext(){return new Proxy({},{get:()=>()=>{}});}
}
const nodes=new Map(),reads=[],$=id=>{if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);};
$("sushiwait-reference").dataset.engineeringPanels="true";
const at=Date.parse("2026-10-13T00:00:00+08:00");
class Clock extends Date {constructor(...args){super(...(args.length?args:[at]));}static now(){return at;}}
const row={store_id:"900001",observations:1,successful_pairs:1,failed_pairs:0,scheduled_pause_slots:0,expected_background_slots_so_far:1,observed_background_slots:1,observed_slot_fraction_so_far:1,last_observation_at:"2026-10-09T03:00:00Z"};
const month={days:{"2026-10-09":{"900001":row}},store_names:{900001:"合成门店"},unavailable_store_ids:[]};
const ctx={console,Intl,Date:Clock,Math,Number,String,Object,Array,RegExp,JSON,Promise,Set,Map,AbortSignal,setInterval:()=>0,
window:{devicePixelRatio:1,addEventListener(){}},document:{getElementById:$,createElement:tag=>new Element(tag)}};
ctx.fetch=async path=>{reads.push(path);return {ok:true,json:async()=>path==="/model-advice.json"?{schema_version:1,policy:"public_display_scenarios_v1",stores:{}}:path.includes("/stores/")?{source:"crm_remote_v1_1",requested_store_id:"900001",local_date:path.split("/").at(-1),points:[],graph_truncated:false}:month};};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[2],"utf8"),ctx);
const run=code=>vm.runInContext(code,ctx),settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
 await settle();
 const visible=()=>["overview","analysis","prediction","experience","tickets"].filter(k=>!$("view-"+k).hidden);
 assert.deepEqual(visible(),["overview"]);
 $("nav-analysis").onclick();assert.deepEqual(visible(),["analysis"]);assert.equal($("analysis-empty").hidden,false);
 $("ticket-number").value="4045";$("ticket-issued").value="11:12";$("experience-party").value="2";
 const privateInputs=[$("ticket-number"),$("ticket-issued"),$("experience-party")];
 const initialReads=reads.length;
 for(const k of ["prediction","tickets","experience","overview","analysis"]){$("nav-"+k).onclick();assert.deepEqual(visible(),[k]);assert.equal($("nav-"+k).attributes["aria-current"],"page");}
 assert.equal(reads.length,initialReads);assert.equal($("ticket-number").value,"4045");assert.equal($("experience-party").value,"2");
 assert.deepEqual(privateInputs,[$("ticket-number"),$("ticket-issued"),$("experience-party")]);
 $("engineering-date").value="2026-10-09";$("engineering-date").onchange();await settle();
 $("store").value="900001";$("store").onchange();await settle();
 assert.equal($("analysis-empty").hidden,true);assert.equal($("prediction-content").hidden,false);
 assert(reads.includes("/api/v1/stores/900001/days/2026-10-09"));
 const beforeSwitch=reads.length;$("nav-prediction").onclick();$("nav-analysis").onclick();assert.equal(reads.length,beforeSwitch);
 assert.equal($("engineering-breadcrumb").textContent,"门店分析");
 assert.equal($("nav-group-records").open,true);
 $("header-refresh").onclick();await settle();
 assert.equal($("engineering-date").value,"2026-10-09");assert.equal($("store").value,"900001");
 assert.deepEqual(privateInputs,[$("ticket-number"),$("ticket-issued"),$("experience-party")]);
 assert.equal($("ticket-number").value,"4045");assert.equal($("header-refresh").disabled,false);
 $("engineering-date").value="2026-09-07";$("engineering-date").onchange();await settle();
 assert.equal($("month").value,"2026-09");assert.equal(run("day"),"2026-09-07");assert(reads.includes("/api/v1/months/2026-09"));
 const beforeInvalid=reads.length;$("engineering-date").value="2026-02-31";$("engineering-date").onchange();
 assert.equal(run("day"),"2026-09-07");assert.equal(reads.length,beforeInvalid);
 $("store").value="";$("store").onchange();await settle();
 // Screenshot import must remain reachable before a store is selected.
 assert.equal($("prediction-content").hidden,false);
 assert.equal($("prediction-empty").hidden,true);
 assert.equal($("store-wait-card").hidden,true);
 assert.equal($("ticket-number").value,"4045");
 assert(reads.every(p=>(p.startsWith("/api/v1/")||p==="/model-advice.json")&&!p.includes("4045")&&!p.includes("11:12")));
 console.log("one visible engineering section; shared dates; mounted private inputs; navigation makes no reads passed");
})().catch(e=>{console.error(e);process.exitCode=1;});
