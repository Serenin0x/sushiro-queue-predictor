"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
class Element{constructor(tag){this.tag=tag;this.children=[];this.value="";this.hidden=false;this.textContent="";this.classList={names:new Set(),add:n=>this.classList.names.add(n)};}append(e){this.children.push(e);}replaceChildren(){this.children=[];}setAttribute(){}getBoundingClientRect(){return {width:600};}getContext(){return new Proxy({},{get:()=>()=>{}});}}
const nodes=new Map(),ctx={console,Intl,Date,Math,Number,String,Object,Array,RegExp,JSON,Promise,AbortSignal,setInterval:()=>0,window:{devicePixelRatio:1,addEventListener(){}},document:{getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element(id));return nodes.get(id);},createElement:tag=>new Element(tag)}};
const summary={store_id:"900001",observations:2,successful_pairs:2,failed_pairs:0,scheduled_pause_slots:0,expected_background_slots_so_far:2,observed_background_slots:2,observed_slot_fraction_so_far:1,last_observation_at:"2026-10-09T03:01:00Z"};
const index={days:{"2026-10-09":{"900001":summary}},store_names:{"900001":"第一店","900002":"第二店"},unavailable_store_ids:[]};
let reads=[];ctx.fetch=async path=>{reads.push(path);return {ok:true,json:async()=>index};};vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[2],"utf8"),ctx);
async function settle(){await new Promise(resolve=>setImmediate(resolve));}
(async()=>{await settle();vm.runInContext('day="2026-10-09";$("month").value="2026-10";calendar();',ctx);
const buttons=nodes.get("calendar").children.filter(n=>n.tag==="button");assert.equal(buttons.length,31);assert(buttons[8].classList.names.has("partial"));assert(!buttons[8].classList.names.has("covered"));
nodes.get("store").value="900001";vm.runInContext('calendar();',ctx);assert(nodes.get("calendar").children.filter(n=>n.tag==="button")[8].classList.names.has("covered"));
let resolveA,resolveB;ctx.fetch=path=>new Promise(resolve=>{reads.push(path);if(!resolveA)resolveA=resolve;else resolveB=resolve;});const a=vm.runInContext('showDay()',ctx),b=vm.runInContext('showDay()',ctx);
assert.equal(nodes.get("points").children.length,0);assert.equal(vm.runInContext('detail',ctx),null);
const fresh={points:[],returned_graph_points:0,graph_truncated:false};resolveB({ok:true,json:async()=>fresh});await b;resolveA({ok:true,json:async()=>({points:[{queue_received_at:"2026-10-09T03:00:00Z",queues:{storeQueue:["12"]},count_raw:7,pair_ok:true}],graph_truncated:false})});await a;
assert.equal(vm.runInContext('detail.points.length',ctx),0);assert.equal(nodes.get("points").children.length,0);assert(reads.every(p=>p.startsWith("/api/v1/")));assert(!reads.some(p=>p.includes("sushiro.com")));console.log("calendar gaps, selected-store coverage, stale-detail rejection and local-only reads passed");})().catch(e=>{console.error(e);process.exitCode=1;});
