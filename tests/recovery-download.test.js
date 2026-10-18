// Pure DOM fixtures; real QR images and network access are never used.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../deploy/recovery_web/app.js'),'utf8');
(async()=>{
 const nodes=new Map(),card={dataset:{}},ticket='a'.repeat(32),handoffs=[];let now=1000;
 const node=id=>nodes.get(id)||nodes.set(id,{hidden:true,disabled:false,value:'',textContent:'',href:'',listeners:{},dataset:{},setAttribute(){},removeAttribute(){},
  addEventListener(event,fn){this.listeners[event]=fn;},querySelector(){return node(id+'-button');},focus(){},select(){}}).get(id);
 class Clock extends Date {static now(){return now;}}
 const context={URL,Date:Clock,Error,JSON,Map,console,
  location:{origin:'https://124.222.57.236',pathname:'/recover/',hash:'#'+'A'.repeat(43)},
  sessionStorage:{getItem(){return null;},setItem(){}},history:{replaceState(state,title,url){handoffs.push(url);}},navigator:{clipboard:{async writeText(){}}},
  document:{hidden:false,getElementById:node,querySelector(){return card;},addEventListener(){}},
  window:{addEventListener(){}},setInterval(){return 1;},clearInterval(){},setTimeout(){return 1;},clearTimeout(){},
  async fetch(url){return {ok:true,async json(){
   if(url==='/recover/api/status')return {state:'issued',login_required:false,verification:true};
   if(url==='/recover/api/qr')return {image_url:'/recover/image/'+ticket};
   throw new Error('unexpected network operation');
  }};}
 };
 vm.runInNewContext(source,context,{filename:'recovery-app.js'});
 for(let i=0;i<12;i++)await Promise.resolve();await node('start').listeners.click();
 assert.equal(node('download-qr').href,'/recover/download/'+ticket);
 assert.equal(node('open-qr').href,'/recover/image/'+ticket);
 let prevented=false;node('download-qr').listeners.click({preventDefault(){prevented=true;}});
 assert.equal(prevented,false);
 node('download-qr').href='https://example.invalid/unsafe.png';
 node('download-qr').listeners.click({preventDefault(){prevented=true;}});assert.equal(prevented,true);
 prevented=false;now+=120001;
 node('open-qr').listeners.click({preventDefault(){prevented=true;}});assert.equal(prevented,true);
 const shared=[];let imageReads=0;now=1000;
 context.File=class {constructor(parts,name,options){this.name=name;this.type=options.type;}};
 context.navigator.canShare=data=>data.files.length===1;
 context.navigator.share=data=>{shared.push(data);return Promise.resolve();};
 const originalFetch=context.fetch;
 context.fetch=async url=>{
  if(url.startsWith('/recover/image/')){imageReads++;return {ok:true,async blob(){return {type:'image/jpeg',size:1024};}};}
  return originalFetch(url);
 };
 await node('start').listeners.click();
 for(let i=0;i<6;i++)await Promise.resolve();
 assert.equal(node('save-qr').hidden,false);
 const before=imageReads;node('save-qr').listeners.click();
 assert.equal(imageReads,before);
 assert.deepEqual(Object.keys(shared[0]),['files']);
 assert.equal(shared[0].files[0].name,'sushiwait-authorization.jpg');
 const safariURL=new URL(handoffs[0],'https://124.222.57.236');
 const safariRequests=[];
 const safari={...context,location:safariURL,sessionStorage:{getItem(){return null;},setItem(){}},
  async fetch(url,options){safariRequests.push(JSON.parse(options.body));return {ok:true,async json(){return {state:'issued',login_required:false};}};}};
 vm.runInNewContext(source,safari,{filename:'recovery-app-new-browser.js'});
 for(let i=0;i<4;i++)await Promise.resolve();
 assert.equal(safariRequests[0].cap,'A'.repeat(43));
 console.log('6 download, system-save and browser-handoff boundary cases passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
