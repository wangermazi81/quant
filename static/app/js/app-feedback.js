// 问题反馈：自成一体，仅依赖已有的 api() 封装与 .rename-modal 主题
const FB_PAGE_LABELS = {
  'page-monitor':'监控中心','page-strategy':'策略中心','page-paper':'模拟盘',
  'page-search':'产业链研报','page-review':'复盘'
};
const FB_STATUS_LABELS = {open:'待处理',read:'已读',in_progress:'处理中',resolved:'已解决'};
const FB_STATUS_COLORS = {
  open:'background:#3d2b14;color:#ffa657', read:'background:#21262d;color:#8b949e',
  in_progress:'background:#11324d;color:#79c0ff', resolved:'background:#1a4731;color:#3fb950'
};
let _fbCat = 'bug';
function fbPickCat(el){
  document.querySelectorAll('#fbCat .fb-chip').forEach(c=>c.classList.remove('on'));
  el.classList.add('on'); _fbCat = el.dataset.cat;
}
function _fbCurrentPage(){
  const act = document.querySelector('.page.active');
  const id  = act ? act.id : '';
  return {id, label: FB_PAGE_LABELS[id] || '其他'};
}
function openFeedback(){
  const p = _fbCurrentPage();
  document.getElementById('fbPage').value = p.label;
  document.getElementById('feedbackModal').classList.add('open');
}
function closeFeedback(ev){
  if(ev && ev.target !== document.getElementById('feedbackModal')) return;
  document.getElementById('feedbackModal').classList.remove('open');
  document.getElementById('fbMine').style.display = 'none';
}
function fbToast(msg, isErr){
  let t = document.getElementById('_fbToast');
  if(!t){ t = document.createElement('div'); t.id='_fbToast'; t.className='fb-toast'; document.body.appendChild(t); }
  t.textContent = msg; t.className = 'fb-toast' + (isErr?' err':'');
  void t.offsetWidth; t.classList.add('show');
  clearTimeout(t._tmr); t._tmr = setTimeout(()=>t.classList.remove('show'), 2600);
}
async function submitFeedback(){
  const content = document.getElementById('fbContent').value.trim();
  if(!content){ fbToast('请填写详细描述', true); return; }
  const btn = document.getElementById('fbSubmitBtn');
  btn.disabled = true; btn.textContent = '提交中…';
  const body = {
    category: _fbCat,
    page: _fbCurrentPage().label,
    content,
    contact: document.getElementById('fbContact').value.trim(),
    app_version: '2.0.0'
  };
  let r = null, status = 0;
  try{
    const resp = await fetch(API + '/feedback', {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    status = resp.status;
    try { r = await resp.json(); } catch(e){}
  }catch(e){}
  btn.disabled = false; btn.textContent = '提交';
  if(r && r.ok){
    fbToast('✅ 反馈已提交（#' + r.id + '），感谢你的反馈！');
    document.getElementById('fbContent').value = '';
    document.getElementById('fbContact').value = '';
    if(document.getElementById('fbMine').style.display !== 'none') loadMyFeedback();
    setTimeout(()=>closeFeedback(), 700);
  } else if(status === 404){
    fbToast('反馈后端需重启一次服务才生效（仅此一项需重启）', true);
  } else if(status === 401){
    fbToast('请先登录后再提交反馈', true);
  } else {
    fbToast('提交失败，请稍后重试', true);
  }
}
function toggleMyFeedback(){
  const box = document.getElementById('fbMine');
  if(box.style.display === 'none'){ box.style.display='block'; loadMyFeedback(); }
  else { box.style.display='none'; }
}
async function loadMyFeedback(){
  const box = document.getElementById('fbMine');
  box.innerHTML = '<div style="font-size:12px;color:var(--t3)">加载中…</div>';
  let r = null, status = 0;
  try{ const resp = await fetch(API + '/feedback/mine', {credentials:'include'}); status = resp.status; try{ r = await resp.json(); }catch(e){} }catch(e){}
  if(!r || !r.ok){ box.innerHTML='<div style="font-size:12px;color:var(--rl)">'+(status===404?'反馈后端需重启一次服务才生效':'加载失败（请先登录）')+'</div>'; return; }
  if(!r.items.length){ box.innerHTML='<div style="font-size:12px;color:var(--t3)">你还没有提交过反馈</div>'; return; }
  box.innerHTML = '<div style="font-size:11px;color:var(--t3);margin-bottom:4px">我的反馈（'+r.items.length+'）</div>' +
    r.items.map(it=>{
      const st = FB_STATUS_LABELS[it.status]||it.status;
      const c  = FB_STATUS_COLORS[it.status]||'';
      const note = it.admin_note ? '<div style="color:var(--bl);margin-top:2px">↳ 回复：'+_fbEsc(it.admin_note)+'</div>' : '';
      return '<div class="fb-mine-item">#'+it.id+' · '+(it.created_at||'').slice(5,16)+
        '<span class="fb-st" style="'+c+'">'+st+'</span>'+
        '<div style="color:var(--t1);margin-top:2px">'+_fbEsc(it.content).slice(0,120)+'</div>'+note+'</div>';
    }).join('');
}
function _fbEsc(s){ return String(s==null?'':s).replace(/[&<>"]/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m])); }
