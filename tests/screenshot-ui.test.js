'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const nodes=new Map(),events={},submissions=[],requests=[],revoked=[];
class Node {
 constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.hidden=false;this.disabled=false;this.textContent='';this.dataset={};this.attrs={};this.classList={add(){},remove(){}};}
 append(...values){for(const v of values){v.parent=this;this.children.push(v);if(this.tag==='select'&&this.children.length===1)this.value=v.value;}}
 replaceChildren(...values){for(const c of this.children)c.parent=null;this.children=[];this.append(...values);}
 addEventListener(name,fn){this[name]=fn;}click(){this.onclick?.();}focus(){this.focused=true;}
 setAttribute(k,v){this.attrs[k]=v;}getAttribute(k){return this.attrs[k];}removeAttribute(k){delete this.attrs[k];}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this);this.parent=null;}
}
const $=id=>{if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);};
const cfg={today:'2026-10-15',storeNames:{'900001':'合成一店','900002':'合成二店'},cityById:{'900001':'测试甲市','900002':'测试乙市'}};
const sample={fields:{store_id:'900001',number:'0321',issued_at:'2026-10-15T04:45:19Z',checked_in:false,queue_type:'ordinary'},store_name:'合成一店',image_sha256:'a'.repeat(64),auto_accept:true};
let response=sample,failNumber=null,pending=null,opened=0;
class Clock extends Date{constructor(...args){super(...(args.length?args:['2026-10-15T12:00:00Z']));}static now(){return Date.parse('2026-10-15T12:00:00Z');}}
const context={window:{SushiWaitResearch:{screenshotSettings:()=>cfg,submitEntry:async(fields,opts)=>{if(fields.number===failNumber)throw Error('save_failed');submissions.push({fields,opts});return {saved:true};},showSuccess(){},openTickets(){opened++;}},addEventListener:(n,f)=>events[n]=f},document:{getElementById:$,createElement:tag=>new Node(tag),addEventListener:(n,f)=>events[n]=f},location:{protocol:'https:',hostname:'example.test'},Date:Clock,AbortSignal,URL:{createObjectURL:f=>'blob:'+f.name,revokeObjectURL:v=>revoked.push(v)},fetch:async(path,opts)=>{requests.push({path,opts});if(pending)return pending;return {ok:true,json:async()=>structuredClone(response)};}};
vm.createContext(context);vm.runInContext(fs.readFileSync('src/sushiwait/web/screenshot-ui.js','utf8'),context);events.DOMContentLoaded();
const api=context.window.SushiWaitScreenshots,file=name=>({name,type:'image/png',size:1200}),rows=()=>$('composer-rows').children;
const fields=row=>Object.fromEntries(row.children[2].children.map(label=>{const n=label.children[1];return [n.name,n];}));
const edit=(n,value)=>{n.value=value;(n.input||n.change)?.();};
const submit=()=>api.submit({preventDefault(){}});
function manual(row,number='456'){const f=fields(row);edit(f.city,'测试乙市');edit(f.store,'900002');edit(f.number,number);edit(f.date,'2026-10-15');edit(f.time,'12:30');return f;}
(async()=>{
 $('prediction-mode').value='auto';$('ticket-record-system').checked=true;$('ticket-follow').checked=true;
 assert.equal(rows().length,1);assert.equal(fields(rows()[0]).store.disabled,true);
 const f=manual(rows()[0]);assert.equal(f.store.disabled,false);assert.equal(f.time.step,'60');
 assert.equal(submissions.length,0);await submit();assert.equal(submissions.length,1);
 assert.equal(submissions[0].fields.store_id,'900002');assert.equal(submissions[0].fields.issued_at,'2026-10-15T04:30:00.000Z');assert.equal(submissions[0].opts.inputMethod,'manual');assert.equal(opened,1);
 assert.equal(fields(rows()[0]).number.value,'','successful manual entry clears its draft');

 edit(fields(rows()[0]).city,'测试乙市');edit(fields(rows()[0]).store,'900002');
 await api.upload([file('predict.png')]);assert.equal(submissions.length,1,'even confident OCR only fills a draft');
 let p=fields(rows()[0]);assert.equal(p.number.value,'0321');assert.equal(p.store.value,'900001');assert.equal(p.time.value,'12:45');
 $('prediction-mode').value='thinking';$('ticket-follow').checked=false;edit(p.number,'0322');await submit();
 assert.equal(submissions[1].opts.mode,'thinking','mode is chosen at submission, not upload');assert.equal(submissions[1].opts.follow,false);
 assert.equal(submissions[1].fields.number,'0322');assert.equal(submissions[1].fields.issued_at,'2026-10-15T04:45:19.000Z','untouched OCR seconds remain exact');
 await api.upload([file('time.png')]);edit(fields(rows()[0]).time,'12:46');await submit();assert.equal(submissions[2].fields.issued_at,'2026-10-15T04:46:00.000Z');

 $('entry-tab-record').onclick();assert.equal($('composer-prediction-options').hidden,true);
 response={...sample,fields:{...sample.fields,issued_at:'2026-10-13T04:45:19Z'}};
 await api.upload([file('one.png'),file('two.png')]);assert.equal(rows().length,2);assert.equal(submissions.length,3);
 edit(fields(rows()[0]).number,'901');edit(fields(rows()[1]).number,'902');failNumber='902';await submit();
 assert.equal(rows().length,1,'only successful rows are cleared');assert.equal(fields(rows()[0]).number.value,'902');assert.equal(submissions[3].opts.kind,'record');assert.equal(submissions[3].opts.record,true);assert.equal(submissions[3].opts.follow,false);
 assert($('composer-status').textContent.includes('1 条需重试'));failNumber=null;await submit();assert.equal(rows().length,1);assert.equal(fields(rows()[0]).number.value,'');assert.equal(revoked.length,4);

 // Each tab owns its drafts, including uploads that finish after switching.
 manual(rows()[0],'811');$('entry-tab-predict').onclick();assert.equal(fields(rows()[0]).number.value,'');manual(rows()[0],'812');$('entry-tab-record').onclick();assert.equal(fields(rows()[0]).number.value,'811');
 $('composer-add').onclick();let resolve;pending=new Promise(r=>resolve=r);response=sample;const uploading=api.upload([file('slow.png')]);
 const target=fields(rows()[1]);edit(target.number,'999');$('entry-tab-predict').onclick();assert.equal(fields(rows()[0]).number.value,'812');
 resolve({ok:true,json:async()=>structuredClone(sample)});await uploading;pending=null;
 $('entry-tab-record').onclick();assert.equal(fields(rows()[1]).number.value,'999','late OCR cannot overwrite typing');assert.equal(submissions.length,5);

 // Invalid input blocks all submission; editing and clearing never writes.
 edit(fields(rows()[1]).time,'');await submit();assert.equal(submissions.length,5);assert.equal(fields(rows()[1]).time.getAttribute('aria-invalid'),'true');
 while(rows().length>1)rows()[1].children[0].children[2].onclick();rows()[0].children[0].children[2].onclick();assert.equal(submissions.length,5);
 const before=requests.length;await api.upload([{name:'bad.svg',type:'image/svg+xml',size:500}]);assert.equal(requests.length,before);assert.equal(rows()[0].children[1].hidden,true);
 await api.upload(Array.from({length:21},(_,i)=>file(i+'.png')));assert.equal(requests.length,before);
 context.location.protocol='http:';await api.upload([file('insecure.png')]);assert.equal(requests.length,before);
 assert(requests.every(r=>r.path==='/api/v1/research/screenshots/recognize'&&r.opts.body.name));
 rows()[0].children[0].children[2].onclick();cfg.today='2026-10-16';api.directoryChanged();assert.equal(fields(rows()[0]).date.max,'2026-10-16');assert.equal(fields(rows()[0]).date.value,'2026-10-16');
 // A fuzzy candidate must never silently choose the wrong city or shop.
 context.location.protocol='https:';edit(fields(rows()[0]).city,'测试乙市');edit(fields(rows()[0]).store,'900002');
 response={...sample,fields:{...sample.fields,store_id:null},store_name:null,auto_accept:false,store_suggestion:{store_id:'900001',name:'合成一店'}};
 await api.upload([file('ambiguous.png')]);assert.equal(fields(rows()[0]).city.value,'');assert.equal(fields(rows()[0]).store.value,'');assert.equal(fields(rows()[0]).number.value,'0321');
 assert(rows()[0].children[0].children[1].textContent.includes('门店未确认'));
 console.log('manual store selection, OCR autofill only, editable minutes, submit-time model, isolated drafts, partial retry and automatic clearing passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
