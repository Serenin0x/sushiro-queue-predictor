"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const code = fs.readFileSync(path.join(__dirname,"../adapters/surge-context.js"),"utf8");
const now = 1800000000000;
const config = {schema_version:1,url:"http://127.0.0.1:12345/v1/context",token:"a".repeat(64),
  expires_ms:now+59000,app_id:"wx0000000000000000",store_ids:["900001"]};
const headers = {Authorization:"Bearer synthetic-authorization-secret","X-App-Client":"miniapp",
  "X-App-Code":"synthetic-code-secret","User-Agent":"Synthetic Agent",
  Referer:"https://servicewechat.com/wx0000000000000000/1/page-frame.html","Content-Type":"application/json",
  Cookie:"synthetic-ignored-cookie-secret"};
function input() {return {config:JSON.parse(JSON.stringify(config)),request:{method:"GET",
  url:"https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId=900001",headers:{...headers}},
  response:{status:200,body:JSON.stringify({id:900001,name:"合成门店",private_account:"synthetic-ignored-response-secret"})}};}
function run(change, callbackThrows=false) {
  const value=input();if(change)change(value);
  let doneCalls=[],posts=[];
  const sandbox = {$argument:encodeURIComponent(JSON.stringify(value.config)),$request:value.request,$response:value.response,
    Date:{now:()=>now},$done:value=>doneCalls.push(value),
    $httpClient:{post:(options,callback)=>{posts.push(options);callback(null,{status:200},"{}");if(callbackThrows)throw Error("synthetic-only");}}};
  vm.runInNewContext(code,sandbox,{timeout:1000});
  assert.equal(doneCalls.length,1);assert.equal(JSON.stringify(doneCalls[0]),"{}");
  return posts;
}
let checks=0;
function check(change, expected) {const posts=run(change);assert.equal(posts.length,expected);checks++;return posts;}
const posts=check(null,1), post=posts[0], body=JSON.parse(post.body);
assert.equal(post.url,config.url);assert.equal(post.policy,"DIRECT");
assert.equal(post["auto-redirect"],false);assert.equal(post["auto-cookie"],false);
assert.deepEqual(Object.keys(body.response.store).sort(),["id","name"]);
assert.equal(body.request.headers.length,6);
assert.ok(!post.body.includes("synthetic-ignored"));assert.ok(!post.body.includes("private_account"));
assert.equal(post.timeout,2);checks++;
for(const change of [v=>v.config.url="https://evil.invalid/context",v=>v.config.url="http://0.0.0.0:12345/v1/context",
  v=>v.config.url="http://127.0.0.1:65536/v1/context",v=>v.config.token="weak",
  v=>v.config.expires_ms=now,v=>v.config.expires_ms=now+60001,
  v=>v.config.store_ids=[],v=>v.config.store_ids.push("900002","900003","900004"),
  v=>v.config.app_id="wx1111111111111111",v=>v.request.method="POST",
  v=>v.request.url=v.request.url.replace("sapi.","crm-cn-prd."),v=>v.request.url+="&unexpected=1",
  v=>v.request.url+="#fragment",v=>v.request.url=v.request.url.replace("900001","900002"),
  v=>v.response.status=401,v=>v.response.body="invalid",
  v=>v.response.body=JSON.stringify({id:900002,name:"合成门店"}),
  v=>v.response.body=JSON.stringify({id:900001,name:"bad\nname"}),
  v=>delete v.request.headers.Authorization,v=>v.request.headers.authorization="duplicate-secret",
  v=>v.request.headers.Authorization="bad\r\nheader",
  v=>v.response.body="x".repeat(262145)]) check(change,0);
check(v=>v.request.headers=Object.entries(v.request.headers).map(([field,value])=>({field,value})),1);
const failed=run(null,true);assert.equal(failed.length,1);checks++;
process.stdout.write(JSON.stringify({checks,upstreamCalls:0,realCredentials:false})+"\n");
