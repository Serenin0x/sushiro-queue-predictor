// Isolated synthetic study only: no fetch, credentials, or statistics endpoint.
document.querySelectorAll('[data-preview-nav]').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('[data-preview-nav]').forEach(item => item.classList.remove('active'));
    button.classList.add('active');
    document.querySelector('h1').textContent = button.dataset.previewNav;
    document.querySelector('[data-current-title]').textContent = button.dataset.previewNav;
    showNotice('这是视觉方案预览；正式页面会接入现有对应功能。');
  });
});
let noticeTimer;
function showNotice(message) {
  const element = document.querySelector('.toast');
  element.textContent = message;
  element.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { element.hidden = true; }, 3500);
}
document.querySelectorAll('[data-preview-action]').forEach(button => {
  button.addEventListener('click', () => showNotice('示例数据固定展示；不会请求门店或修改采集。'));
});
