'use strict';
(() => {
  const $ = id => document.getElementById(id), validCap = value => /^[A-Za-z0-9_-]{43}$/.test(value || '');
  const fragment = location.hash.slice(1);
  let stored = null;
  try { stored = sessionStorage.getItem('sushiwait-recovery-cap'); } catch (_) {}
  const cap = validCap(fragment) ? fragment : validCap(stored) ? stored : null;
  if (cap) {
    try { sessionStorage.setItem('sushiwait-recovery-cap', cap); } catch (_) {}
    // Fragments stay out of HTTP requests and preserve QQ Mail → Safari handoff.
    history.replaceState(null, '', location.pathname + (location.search || '') + '#' + cap);
  }
  let timer = null, countdownTimer = null, busy = false, verification = false, lastState = null;
  let view = 'checking', imageExpiresAt = 0, imageFile = null, imageGeneration = 0;
  const messages = {
    link_invalid: '授权链接不可用，请从工作台或最新提醒重新打开。',
    link_expired: '授权链接已过期，请从工作台或最新提醒重新打开。',
    login_required: '输入现有管理密码，确认身份后继续。',
    login_rate_limited: '验证次数较多，请稍后重试。',
    qr_expired: '二维码已过期，刷新后再扫一扫。',
    qr_rate_limited: '二维码刷新次数较多，请稍后再试。',
    qr_image_unavailable: '二维码图片未能加载，请刷新后再试。',
    account_mismatch: '账号不一致，请使用原来的微信账号扫码。',
    confirmation_not_ready: '微信正在完成授权，请稍等。',
    incident_changed: '微信授权已更新，采集状态还需要进一步核对。',
    upstream_unavailable: '授权服务暂时没有响应，请稍后再试。',
    portal_unavailable: '暂时无法读取授权状态，请稍后重试。'
  };
  function stage(text, next) {
    view = next;
    const card = document.querySelector('.card');
    card.dataset.stage = next;
    card.dataset.state = next === 'success' ? 'success' : ['error','expired','unknown'].includes(next) ? 'error' : '';
    $('message').textContent = text;
    $('state-label').textContent = ({checking:'连接授权服务',login:'确认身份',ready:'准备就绪',verifying:'正在验证',generating:'生成二维码',pending:'等待微信扫码',scanned:'等待手机确认',restoring:'正在接续采集',success:'授权完成',expired:'二维码已过期',error:'需要处理',unknown:'等待核对'})[next];
    $('portal-visual').hidden = !$('qr-wrap').hidden;
    const step = ['checking','login','verifying'].includes(next) ? 1 : ['restoring','success'].includes(next) ? 3 : 2;
    for (let i = 1; i <= 3; i++) {
      const node = $('auth-step-' + i);
      node.dataset.state = i < step || next === 'success' ? 'done' : i === step ? 'active' : 'waiting';
      if (i === step && next !== 'success') node.setAttribute('aria-current', 'step');
      else node.removeAttribute('aria-current');
    }
  }
  function note(text) { $('interaction-note').textContent = text; $('interaction-note').hidden = !text; }
  function stop() { if (timer !== null) clearTimeout(timer); timer = null; }
  function stopCountdown() { if (countdownTimer !== null) clearInterval(countdownTimer); countdownTimer = null; }
  function schedule(fn) { stop(); if (!document.hidden) timer = setTimeout(fn, 4000); }
  function lock(active, id) {
    busy = active;
    for (const key of ['start','retry','recheck','login-submit']) {
      $(key).disabled = active; $(key).setAttribute('aria-busy', String(active && key === id));
    }
  }
  function hideQR() {
    $('qr-wrap').hidden = true; imageFile = null; imageExpiresAt = 0; ++imageGeneration; stopCountdown();
  }
  function expire() {
    stop(); hideQR(); $('retry').hidden = false; $('recheck').hidden = true;
    stage(messages.qr_expired, 'expired');
  }
  function countdown() {
    if (!imageExpiresAt || $('qr-wrap').hidden) return;
    const seconds = Math.max(0, Math.ceil((imageExpiresAt - Date.now()) / 1000));
    if (!seconds) { expire(); return; }
    $('qr-countdown').textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2,'0')} 后过期`;
    $('qr-lifetime').value = seconds;
  }
  function startCountdown() { stopCountdown(); countdown(); if (!document.hidden && imageExpiresAt) countdownTimer = setInterval(countdown, 1000); }
  async function api(action, extra = {}) {
    const response = await fetch('/recover/api/' + action, {
      method:'POST', cache:'no-store', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({cap, ...extra})
    });
    const value = await response.json();
    if (!response.ok) { const error = new Error(value.error || 'portal_unavailable'); error.code = value.error; throw error; }
    return value;
  }
  function failure(error) {
    const code = error.code || 'portal_unavailable';
    stop(); $('recheck').hidden = true;
    if (code === 'login_required') {
      hideQR(); $('login-form').hidden = false; $('start').hidden = true; $('retry').hidden = true;
      stage(messages.login_required, 'login'); return;
    }
    if (code === 'qr_expired') { expire(); return; }
    hideQR(); $('start').hidden = true;
    $('retry').hidden = ['link_invalid','link_expired','incident_changed','login_rate_limited','qr_rate_limited'].includes(code);
    if (['upstream_unavailable','portal_unavailable'].includes(code)) { $('retry').hidden = true; $('recheck').hidden = false; }
    if (['link_invalid','link_expired','incident_changed'].includes(code)) $('login-form').hidden = true;
    stage(messages[code] || '暂时无法完成，请稍后重试。', 'error');
  }
  function render(value) {
    verification = value.verification ?? verification;
    lastState = value.state;
    if (value.state === 'resumed' || value.state === 'scheduled' || value.state === 'authorized' && verification) {
      stop(); hideQR(); $('heading').textContent = '授权已完成';
      $('retry').hidden = true; $('start').hidden = true; $('login-form').hidden = true; $('recheck').hidden = true;
      $('statistics').hidden = false; note('');
      let text = verification ? '微信授权已更新，系统将按原计划继续采集。' : '新样本已经保存，采集已自动恢复。';
      if (value.state === 'scheduled') {
        const at = new Date(value.next_open_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
        text = '已安排在 ' + at + ' 开店时自动采集。';
      }
      stage(text, 'success'); return true;
    }
    if (value.state === 'authorized') {
      hideQR(); $('heading').textContent = '正在恢复采集';
      $('retry').hidden = true; $('start').hidden = true; $('login-form').hidden = true; $('recheck').hidden = true;
      stage('微信授权已保存，正在等待第一组新样本。', 'restoring'); schedule(check); return true;
    }
    if (value.state === 'confirmation_unknown') {
      stop(); hideQR(); $('start').hidden = true; $('retry').hidden = true; $('login-form').hidden = true; $('recheck').hidden = false;
      stage('授权结果需要核对，系统没有重复提交。', 'unknown'); return true;
    }
    $('heading').textContent = verification ? '微信授权' : '恢复采集';
    return false;
  }
  async function check() {
    if (busy) return;
    lock(true, 'recheck'); $('recheck').hidden = true;
    try {
      const value = await api('status');
      if (render(value)) return;
      if (value.login_required) { failure({code:'login_required'}); return; }
      if (value.state === 'qr_pending') {
        if (imageExpiresAt > Date.now() && !$('qr-wrap').hidden) { startCountdown(); schedule(poll); return; }
        lock(false); await start(); return;
      }
      hideQR(); $('login-form').hidden = true; $('start').hidden = false; $('retry').hidden = true;
      stage(verification ? '出门前，先更新一次微信授权。' : '更新微信授权后，系统会自动接续采集。', 'ready');
    } catch (error) { failure(error); }
    finally { lock(false); }
  }
  async function start() {
    if (busy) return;
    lock(true, 'retry'); stop(); hideQR(); note(''); $('retry').hidden = true; $('recheck').hidden = true;
    stage('正在生成微信授权二维码', 'generating');
    try {
      const value = await api('qr');
      if (!/^\/recover\/image\/[0-9a-f]{32}$/.test(value.image_url)) throw new Error('image_invalid');
      const generation = ++imageGeneration;
      $('qr').src = value.image_url + '?v=' + Date.now();
      if (typeof $('qr').decode === 'function') {
        try { await $('qr').decode(); } catch (_) { const error = new Error(); error.code = 'qr_image_unavailable'; throw error; }
      }
      $('download-qr').href = value.image_url.replace('/recover/image/', '/recover/download/');
      $('open-qr').href = value.image_url;
      imageExpiresAt = Math.min(Date.now()+120000, Number(value.expires_at || 0)*1000 || Date.now()+120000);
      if (imageExpiresAt <= Date.now()) { expire(); return; }
      imageFile = null; $('save-qr').hidden = true;
      // Prepare the file before the tap so Safari retains native user activation.
      if (typeof navigator.share === 'function' && typeof navigator.canShare === 'function' && typeof File === 'function') {
        fetch(value.image_url,{cache:'no-store',credentials:'same-origin'}).then(response => {
          if (!response.ok) throw new Error('image_unavailable'); return response.blob();
        }).then(blob => {
          if (generation !== imageGeneration || Date.now() >= imageExpiresAt || !['image/png','image/jpeg'].includes(blob.type) || blob.size > 1048576) return;
          const file = new File([blob],'sushiwait-authorization.'+(blob.type === 'image/jpeg' ? 'jpg' : 'png'),{type:blob.type});
          if (navigator.canShare({files:[file]})) { imageFile = file; $('save-qr').hidden = false; }
        }).catch(() => {});
      }
      $('qr-wrap').hidden = false; $('start').hidden = true; $('retry').hidden = false; $('login-form').hidden = true;
      $('scan-hint').textContent = '用微信扫一扫对准二维码';
      lastState = 'qr_pending'; stage('用微信扫一扫对准二维码，并在手机上确认。', 'pending');
      startCountdown(); schedule(poll);
    } catch (error) { failure(error); }
    finally { lock(false); }
  }
  async function poll() {
    if (busy || document.hidden || !imageExpiresAt || $('qr-wrap').hidden) return;
    if (Date.now() >= imageExpiresAt) { expire(); return; }
    lock(true);
    try {
      const value = await api('poll');
      if (render(value)) return;
      if (!imageExpiresAt || Date.now() >= imageExpiresAt) { expire(); return; }
      if (value.state === 'scanned') { stage('已识别，请在微信中确认授权。', 'scanned'); $('scan-hint').textContent = '二维码已扫描，等待手机确认'; }
      else if (['expired','cancelled','unknown'].includes(value.state)) { expire(); return; }
      else { stage('用微信扫一扫对准二维码，并在手机上确认。', 'pending'); $('scan-hint').textContent = '用微信扫一扫对准二维码'; }
      schedule(poll);
    } catch (error) {
      if (error.code === 'confirmation_not_ready' || error.code === 'upstream_unavailable') {
        stage(messages[error.code], view === 'scanned' ? 'scanned' : 'pending'); schedule(poll);
      } else failure(error);
    } finally { lock(false); }
  }
  $('start').addEventListener('click', start);
  $('retry').addEventListener('click', start);
  $('recheck').addEventListener('click', check);
  $('save-qr').addEventListener('click', () => {
    if (!imageFile || Date.now() >= imageExpiresAt) return failure({code:'qr_expired'});
    note('请在系统菜单中选择“存储图像”。');
    navigator.share({files:[imageFile]}).catch(error => { if (error.name !== 'AbortError') note('当前浏览器无法保存图片，请在 Safari 打开后保存。'); });
  });
  ['download-qr','open-qr'].forEach(id => $(id).addEventListener('click', event => {
    const link = new URL($(id).href, location.origin);
    if (link.origin !== location.origin || !/^\/recover\/(?:image|download)\/[0-9a-f]{32}$/.test(link.pathname) || !imageExpiresAt || Date.now() >= imageExpiresAt) {
      event.preventDefault(); failure({code:'qr_expired'});
    }
  }));
  $('copy-link').addEventListener('click', async () => {
    const link = location.origin + '/recover/#' + cap;
    try { await navigator.clipboard.writeText(link); $('copy-link').textContent = '已复制'; note('授权链接已复制。'); }
    catch (_) { $('share-link').value = link; $('share-link').hidden = false; $('share-link').focus(); $('share-link').select(); note('请复制上方完整链接。'); }
  });
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    lock(true,'login-submit'); stage('正在验证身份', 'verifying');
    const password = $('password').value; $('password').value = '';
    try { await api('login',{password}); $('login-form').hidden = true; lock(false); await start(); }
    catch (error) { failure(error); }
    finally { lock(false); }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stop(); stopCountdown(); return; }
    if (['success','unknown','expired'].includes(view)) return;
    if (!$('qr-wrap').hidden && imageExpiresAt) { countdown(); if (imageExpiresAt) { startCountdown(); schedule(poll); } }
    else check();
  });
  window.addEventListener('pagehide', () => { stop(); stopCountdown(); $('password').value = ''; });
  if (!cap) { failure({code:'link_invalid'}); return; }
  stage('正在检查授权状态', 'checking'); check();
})();
