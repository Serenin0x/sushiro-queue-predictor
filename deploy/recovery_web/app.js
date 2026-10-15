'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const fragment = location.hash.slice(1);
  let cap = /^[A-Za-z0-9_-]{43}$/.test(fragment) ? fragment : sessionStorage.getItem('sushiwait-recovery-cap');
  if (/^[A-Za-z0-9_-]{43}$/.test(cap || '')) {
    sessionStorage.setItem('sushiwait-recovery-cap', cap);
    // Keep the fragment when handing off from an app webview to Safari. URL
    // fragments are not sent in HTTP requests; stripping it loses the capability
    // because sessionStorage cannot travel between different browsers.
    history.replaceState(null, '', location.pathname + (location.search || '') + '#' + cap);
  }
  let timer = null, busy = false, verification = false, lastState = null, imageExpiresAt = 0;
  let imageFile = null, imageGeneration = 0;
  const messages = {
    link_invalid: '请从最新授权提醒重新打开完整链接。',
    link_expired: '授权链接已过期，请打开最新授权提醒。',
    login_required: '请验证授权管理密码，再更新微信授权。',
    login_rate_limited: '验证次数较多，请稍后重试。',
    qr_expired: '二维码已过期，可以刷新后再试。',
    qr_rate_limited: '二维码刷新次数较多，请打开下一条提醒。',
    account_mismatch: '请使用原来的微信账号授权。',
    confirmation_not_ready: '微信正在完成授权，请稍等。',
    incident_changed: '微信授权已更新，但采集状态已变化，需要进一步检查。',
    upstream_unavailable: '授权服务暂时没有响应，请稍后再试。',
    portal_unavailable: '暂时无法读取授权状态，请稍后重试。'
  };
  function state(text, kind = '') {
    $('message').textContent = text;
    document.querySelector('.card').dataset.state = kind;
  }
  function stop() { if (timer) clearTimeout(timer); timer = null; }
  function schedule(fn) { stop(); timer = setTimeout(fn, 4000); }
  async function api(action, extra = {}) {
    const response = await fetch('/recover/api/' + action, {
      method: 'POST', cache: 'no-store', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({cap, ...extra})
    });
    const value = await response.json();
    if (!response.ok) { const error = new Error(value.error || 'portal_unavailable'); error.code = value.error; throw error; }
    return value;
  }
  function failure(error) {
    state(messages[error.code] || '暂时无法完成，请稍后重试。', 'error');
    if (error.code === 'login_required') {
      $('login-form').hidden = false; $('start').hidden = true;
    } else if (['link_invalid', 'link_expired', 'incident_changed'].includes(error.code)) {
      stop(); $('login-form').hidden = true; $('start').hidden = true; $('retry').hidden = true;
    } else $('retry').hidden = false;
  }
  function render(value) {
    verification = value.verification ?? verification;
    lastState = value.state;
    if (value.state === 'resumed' || value.state === 'authorized' && verification) {
      stop(); $('heading').textContent = '授权已完成'; $('mark').textContent = '✓';
      $('qr-wrap').hidden = true; $('retry').hidden = true; $('start').hidden = true; $('login-form').hidden = true;
      $('statistics').hidden = false;
      state(verification ? '微信授权已更新，采集继续运行。' : '新样本已经保存，采集已自动恢复。', 'success');
      return true;
    }
    if (value.state === 'authorized') {
      $('heading').textContent = '正在恢复采集'; $('mark').textContent = '✓';
      $('qr-wrap').hidden = true; $('retry').hidden = true; $('start').hidden = true; $('login-form').hidden = true;
      state('微信授权已保存，正在等待第一组新样本。');
      schedule(check); return true;
    }
    if (value.state === 'scheduled') {
      stop(); $('heading').textContent = '授权已完成'; $('mark').textContent = '✓';
      $('qr-wrap').hidden = true; $('retry').hidden = true; $('start').hidden = true; $('login-form').hidden = true;
      $('statistics').hidden = false;
      const at = new Date(value.next_open_at).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', month:'numeric', day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
      state('已安排在 ' + at + ' 开店时自动采集。', 'success'); return true;
    }
    if (value.state === 'confirmation_unknown') {
      stop(); state('授权结果需要核对，系统没有重复提交。', 'error'); return true;
    }
    return false;
  }
  async function check() {
    try {
      const value = await api('status');
      if (render(value)) return;
      if (value.state === 'qr_pending') { await start(); return; }
      $('login-form').hidden = !value.login_required;
      $('start').hidden = value.login_required;
      state(value.login_required ? messages.login_required : '更新微信授权后，系统会自动继续采集。');
    } catch (error) { failure(error); }
  }
  async function start() {
    if (busy) return;
    busy = true; $('start').disabled = true; $('retry').disabled = true;
    try {
      stop(); state('正在生成微信授权二维码');
      const value = await api('qr');
      if (!/^\/recover\/image\/[0-9a-f]{32}$/.test(value.image_url)) throw new Error('image_invalid');
      $('qr').src = value.image_url + '?v=' + Date.now();
      $('download-qr').href = value.image_url.replace('/recover/image/', '/recover/download/');
      $('open-qr').href = value.image_url;
      imageExpiresAt = Math.min(Date.now()+120000, Number(value.expires_at || 0)*1000 || Date.now()+120000);
      imageFile = null; const generation = ++imageGeneration; $('save-qr').hidden = true;
      // Prepare before the tap so Safari keeps native user activation. Share
      // only the image file; mixing text/URL has known WebKit limitations.
      if (typeof navigator.share === 'function' && typeof navigator.canShare === 'function' && typeof File === 'function') {
        fetch(value.image_url, {cache:'no-store',credentials:'same-origin'}).then(response => {
          if (!response.ok) throw new Error('image_unavailable');
          return response.blob();
        }).then(blob => {
          if (generation !== imageGeneration || Date.now() >= imageExpiresAt
              || !['image/png','image/jpeg'].includes(blob.type) || blob.size > 1048576) return;
          const file = new File([blob], 'sushiwait-authorization.' + (blob.type === 'image/jpeg' ? 'jpg' : 'png'), {type:blob.type});
          if (navigator.canShare({files:[file]})) { imageFile=file; $('save-qr').hidden=false; }
        }).catch(() => {});
      }
      $('qr-wrap').hidden = false; $('start').hidden = true; $('retry').hidden = false;
      state('用微信扫一扫对准二维码，并确认授权。');
      schedule(poll);
    } catch (error) { failure(error); }
    finally { busy = false; $('start').disabled = false; $('retry').disabled = false; }
  }
  async function poll() {
    try {
      const value = await api('poll');
      if (render(value)) return;
      if (value.state === 'scanned') state('已识别，请在微信中确认授权。');
      else if (['expired', 'cancelled', 'unknown'].includes(value.state)) {
        $('retry').hidden = false; state(messages.qr_expired); return;
      }
      schedule(poll);
    } catch (error) {
      if (error.code === 'confirmation_not_ready' || error.code === 'upstream_unavailable') {
        state(messages[error.code]); schedule(poll);
      } else failure(error);
    }
  }
  $('start').addEventListener('click', start);
  $('retry').addEventListener('click', start);
  $('save-qr').addEventListener('click', () => {
    if (!imageFile || Date.now() >= imageExpiresAt) return failure({code:'qr_expired'});
    state('请在系统菜单中选择“存储图像”。');
    navigator.share({files:[imageFile]}).catch(error => {
      if (error.name !== 'AbortError') state('当前浏览器无法保存图片，请在 Safari 打开后保存。');
    });
  });
  ['download-qr','open-qr'].forEach(id => $(id).addEventListener('click', event => {
    const link = new URL($(id).href, location.origin);
    if (link.origin !== location.origin || !/^\/recover\/(?:image|download)\/[0-9a-f]{32}$/.test(link.pathname)
        || !imageExpiresAt || Date.now() >= imageExpiresAt) {
      event.preventDefault(); failure({code:'qr_expired'});
    }
  }));
  $('copy-link').addEventListener('click', async () => {
    const link = location.origin + '/recover/#' + cap;
    try {
      await navigator.clipboard.writeText(link);
      $('copy-link').textContent = '已复制';
    } catch (_) {
      $('share-link').value = link; $('share-link').hidden = false;
      $('share-link').focus(); $('share-link').select();
      $('copy-link').textContent = '请复制下方链接';
    }
  });
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    busy = true; const button = $('login-form').querySelector('button'); button.disabled = true;
    try {
      await api('login', {password: $('password').value});
      $('password').value = ''; $('login-form').hidden = true; busy = false; await start();
    } catch (error) { $('password').value = ''; failure(error); }
    finally { busy = false; button.disabled = false; }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stop();
    else if (lastState === 'qr_pending' || $('qr-wrap').hidden === false) schedule(poll);
    else check();
  });
  if (!cap) { state(messages.link_invalid, 'error'); return; }
  check();
})();
