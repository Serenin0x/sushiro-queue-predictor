'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const nodes=new Map(),events={},imports=[],requests=[],revoked=[];
class Node {
 constructor(){this.children=[];this.value='';this.hidden=false;this.disabled=false;this.textContent='';this.classList={add(){},remove(){}};}
 append(...values){this.children.push(...values);}replaceChildren(...values){this.children=[...values];}
 addEventListener(name,fn){this[name]=fn;}click(){this.onclick?.();}
}
const $=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
let settings={kind:'record',mode:'algorithm',record:false,follow:false,storeNames:{'900001':'合成门店'},cityById:{'900001':'测试城市'}};
const sample={fields:{store_id:'900001',number:'0321',issued_at:'2026-09-08T04:45:19Z',checked_in:false,queue_type:'ordinary'},store_name:'合成门店',image_sha256:'a'.repeat(64),auto_accept:true};
let response=sample,failSave=false;
const context={window:{SushiWaitResearch:{screenshotSettings:()=>({...settings}),importScreenshot:async(fields,opts)=>{if(failSave)throw Error('save');imports.push({fields,opts});return {record:opts.kind==='record'||opts.record};},showSuccess(){},openTickets(){}},addEventListener:(n,f)=>events[n]=f},document:{getElementById:$,createElement:()=>new Node(),addEventListener:(n,f)=>events[n]=f},location:{protocol:'https:',hostname:'example.test'},Date,AbortSignal,URL:{createObjectURL:f=>'blob:'+f.name,revokeObjectURL:v=>revoked.push(v)},fetch:async(path,opts)=>{requests.push({path,opts});return {ok:true,json:async()=>structuredClone(response)};}};
vm.createContext(context);vm.runInContext(fs.readFileSync('src/sushiwait/web/screenshot-ui.js','utf8'),context);events.DOMContentLoaded();
const upload=context.window.SushiWaitScreenshots.upload,file=name=>({name,type:'image/png',size:1200}),rows=()=>$('screenshot-rows').children;
(async()=>{
 await upload([file('one.png'),file('two.png')]);
 assert.equal(imports.length,2);assert(imports.every(i=>i.opts.kind==='record'&&!i.opts.reviewed));assert.equal(imports[0].fields.issued_at,'2026-09-08T04:45:19.000Z');
 assert.equal($('screenshot-show-tickets').hidden,false);assert.equal($('screenshot-select').disabled,false);
 $('screenshot-clear').onclick();assert.equal(rows().length,0);assert.equal(imports.length,2,'clearing previews must not delete submitted records');assert.equal(revoked.length,2);
 response={...sample,auto_accept:false};await upload([file('uncertain.png')]);assert.equal(imports.length,2,'uncertain OCR cannot autosave');
 let button=rows()[0].children[1].children[2];assert.equal(button.hidden,false);await button.onclick();assert.equal(imports.length,3);assert.equal(imports[2].opts.reviewed,true);
 $('screenshot-clear').onclick();settings={...settings,kind:'prediction',record:false,follow:true};response=sample;await upload([file('prediction.png')]);assert.equal(imports[3].opts.record,false);assert.equal(imports[3].opts.follow,true);
 $('screenshot-clear').onclick();failSave=true;await upload([file('retry.png')]);button=rows()[0].children[1].children[2];assert.equal(button.hidden,false);assert.equal(imports.length,4);failSave=false;await button.onclick();assert.equal(imports.length,5,'save failure can be retried');
 $('screenshot-clear').onclick();const previous=requests.length;await upload([{name:'bad.svg',type:'image/svg+xml',size:500}]);assert.equal(requests.length,previous);await upload(Array.from({length:21},(_,i)=>file(i+'.png')));assert.equal(requests.length,previous);
 assert(requests.every(r=>r.path==='/api/v1/research/screenshots/recognize'&&r.opts.body.name&&!r.path.includes('0321')));
 console.log('batch upload, uncertain review, seconds, opt-in prediction, retry, clear-list preservation and bounds passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
