// Same-computer, explicitly enabled, one-minute context forwarding only.
// Does not change the official response, initialize, book or cancel anything.
// Argument is private session file's URI-encoded adapter_argument field.
(function () {
  "use strict";
  var completed = false;
  function done() { if (!completed) { completed = true; $done({}); } }
  try {
    var config = JSON.parse(decodeURIComponent($argument));
    var endpoint = /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})\/v1\/context$/.exec(config.url);
    var remaining = config.expires_ms - Date.now();
    if (config.schema_version !== 1 || !endpoint || Number(endpoint[1]) > 65535 ||
        !/^[a-f0-9]{64}$/.test(config.token) || !/^wx[a-f0-9]{16}$/.test(config.app_id) ||
        !Number.isSafeInteger(config.expires_ms) || remaining <= 0 || remaining > 60000 ||
        !Array.isArray(config.store_ids) || config.store_ids.length < 1 || config.store_ids.length > 3 ||
        config.store_ids.some(function (id) { return typeof id !== "string" || !/^[1-9][0-9]{0,18}$/.test(id); })) {
      done(); return;
    }
    var request = $request, response = $response;
    var match = /^https:\/\/sapi\.sushiro\.com\.cn\/gateway\/wechat\/api\/2\.0\/getStoreById\?storeId=([1-9][0-9]{0,18})$/.exec(request.url);
    if (request.method !== "GET" || !match || config.store_ids.indexOf(match[1]) < 0 || response.status !== 200 ||
        typeof response.body !== "string" || response.body.length > 262144) { done(); return; }
    var store = JSON.parse(response.body);
    if (!store || typeof store !== "object" || Array.isArray(store) ||
        !(typeof store.id === "string" || (Number.isSafeInteger(store.id) && store.id > 0)) ||
        String(store.id) !== match[1] || typeof store.name !== "string" ||
        store.name.length < 1 || store.name.length > 200 || /[\x00-\x1f\x7f]/.test(store.name)) { done(); return; }
    var allowed = ["authorization", "x-app-client", "x-app-code", "user-agent", "referer", "content-type"];
    var headers = [], seen = {};
    function add(name, value) {
      if (typeof name !== "string") throw new Error();
      name = name.toLowerCase();
      if (allowed.indexOf(name) < 0) return;
      if (seen[name] || typeof value !== "string" || !value || /[\r\n]/.test(value)) throw new Error();
      seen[name] = true; headers.push({name:name,value:value});
    }
    if (Array.isArray(request.headers)) {
      request.headers.forEach(function (h) { add(h.field, h.value); });
    } else {
      Object.keys(request.headers).forEach(function (name) { add(name, request.headers[name]); });
    }
    if (headers.length !== 6) { done(); return; }
    var referer = headers.filter(function (h) { return h.name === "referer"; })[0].value;
    var app = /^https:\/\/servicewechat\.com\/(wx[a-f0-9]{16})\/[0-9]+\/(?:page-frame\.html|index\.html)$/.exec(referer);
    if (!app || app[1] !== config.app_id) { done(); return; }
    var body = JSON.stringify({schema_version:1,request:{method:"GET",url:request.url,headers:headers},
      response:{status:200,store:{id:store.id,name:store.name}}});
    if (body.length > 32768) { done(); return; }
    $httpClient.post({url:config.url,headers:{"Content-Type":"application/json",Authorization:"Bearer " + config.token},
      body:body,timeout:Math.min(2,remaining/1000),policy:"DIRECT","auto-redirect":false,"auto-cookie":false}, done);
  } catch (_) { done(); }
}());
