// ── AI 写策略 / 因子 ────────────────────────────────────
let _aiGenMode = 'strat'   // 'strat' | 'factor'

function _checkAiConfigured(){
  if(!(_currentUser && _currentUser.is_admin)) return true   // 普通用户：AI配置由后端全局提供
  if(!aiCfg?.enabled || !aiCfg?.api_key_masked || aiCfg.api_key_masked==='未配置'){
    if(!confirm('AI功能尚未配置API Key。\n\n请先前往「⚙ 系统设置」填写API Key并启用。\n\n是否前往设置？')) return false
    switchPage('settings')
    return false
  }
  return true
}

function openAiGen(){
  if(!_checkAiConfigured()) return
  _aiGenMode = 'strat'
  document.querySelector('#aiGenOverlay .kl-title').textContent = '✨ AI 写选股策略'
  document.getElementById('aiGenDesc').placeholder = '例如：找出过去5日均线金叉20日均线，且成交量放大1.5倍以上的股票，排除ST股，优先选涨幅不超过5%的个股...'
  document.getElementById('aiGenBtn2').textContent = '✨ 生成策略'
  document.getElementById('aiGenQuickBtns').style.display = ''
  document.getElementById('aiGenOverlay').classList.add('show')
  document.getElementById('aiGenDesc').focus()
  document.getElementById('aiGenErr').style.display='none'
  document.getElementById('aiGenSt').textContent=''
}

function openAiFactorGen(){
  if(!_checkAiConfigured()) return
  _aiGenMode = 'factor'
  document.querySelector('#aiGenOverlay .kl-title').textContent = '✨ AI 写因子策略'
  document.getElementById('aiGenDesc').placeholder = '例如：计算近20日价格动量，过滤流动性不足的股票，动量越强得分越高...'
  document.getElementById('aiGenBtn2').textContent = '✨ 生成因子'
  document.getElementById('aiGenQuickBtns').style.display = 'none'
  document.getElementById('aiGenOverlay').classList.add('show')
  document.getElementById('aiGenDesc').value = ''
  document.getElementById('aiGenDesc').focus()
  document.getElementById('aiGenErr').style.display='none'
  document.getElementById('aiGenSt').textContent=''
}

function closeAiGen(){
  document.getElementById('aiGenOverlay').classList.remove('show')
}
function fillAiGenDesc(txt){
  document.getElementById('aiGenDesc').value=txt
}

// ── 策略模板系统 ─────────────────────────────────
let _stratTemplates = []
let _tplCategory = 'all'
let _selTplId = null

async function loadStrategyTemplates(){
  const loadEl = document.getElementById('aiTplLoading')
  try {
    const r = await api('/strategy_templates')
    if(!r?.ok) throw new Error(r?.error||'加载失败')
    _stratTemplates = r.templates||[]
    if(loadEl) loadEl.textContent = `(${_stratTemplates.length}个)`
    _renderTplCategories()
    _renderTplGrid('all')
  } catch(e){
    if(loadEl) loadEl.textContent = '(加载失败)'
  }
}

function _renderTplCategories(){
  const cats = ['all', ...[...new Set(_stratTemplates.map(t=>t.category))]]
  const el = document.getElementById('aiTplCategoryBtns')
  if(!el) return
  el.innerHTML = cats.map(c=>
    `<button class="tpl-cat-btn ${c==='all'?'active':''}" data-ui-click="_filterTpl('${c}',this)">${c==='all'?'全部':c}</button>`
  ).join('')
}

function _filterTpl(cat, btn){
  _tplCategory = cat
  document.querySelectorAll('.tpl-cat-btn').forEach(b=>b.classList.remove('active'))
  btn.classList.add('active')
  _renderTplGrid(cat)
}

function _renderTplGrid(cat){
  const grid = document.getElementById('aiTplGrid')
  if(!grid) return
  const list = cat==='all' ? _stratTemplates : _stratTemplates.filter(t=>t.category===cat)
  grid.innerHTML = list.map(t=>`
    <div class="tpl-card ${t.id===_selTplId?'active':''}" data-ui-click="_selectTplById('${t.id}')">
      <div class="tpl-name">${esc(t.name)}</div>
      <span class="tpl-tag">${esc(t.category)}</span>
      <div style="font-size:10px;color:var(--t4);margin-top:3px;line-height:1.3">${esc(t.desc.substring(0,30))}...</div>
    </div>`).join('')
}

// 按 id 从模板表取 prompt，避免把整段 prompt 塞进 data-ui-click 的模板字面量里
// （模板字面量参数无法被安全解析器处理，会逼着保留 CSP unsafe-eval）
function _selectTplById(id){
  const t = (_stratTemplates || []).find(x => String(x.id) === String(id))
  if(!t) return
  _selectTpl(t.id, t.prompt)
}

function _selectTpl(id, prompt){
  _selTplId = id
  _renderTplGrid(_tplCategory)
  document.getElementById('aiGenDesc').value = prompt
}

// 在打开AI写策略弹窗时加载模板
const _origOpenAiGen = openAiGen
openAiGen = function(){
  _origOpenAiGen()
  if(_stratTemplates.length===0) loadStrategyTemplates()
  document.getElementById('aiTplSection').style.display=''
}
const _origOpenAiFactorGen = openAiFactorGen
openAiFactorGen = function(){
  _origOpenAiFactorGen()
  // 因子模式隐藏策略模板
  document.getElementById('aiTplSection').style.display='none'
}

// ── 通知推送配置 ─────────────────────────────────
let _notifyConfig = {}

async function initNotifyConfig(){
  if(!_currentUser) return
  const r = await api('/user/notify_config')
  if(!r?.ok) return
  _notifyConfig = r
  // 渲染各渠道
  _applyNotifyChannel('wecom',   ['ntWeComEn',   'ntWeComFields',   'ntWeComWh'])
  _applyNotifyChannel('feishu',  ['ntFeiShuEn',  'ntFeiShuFields',  'ntFeiShuWh'])
  _applyNotifyChannel('dingtalk',['ntDingTalkEn','ntDingTalkFields','ntDingTalkWh','ntDingTalkSec'])
  _applyNotifyChannel('telegram',['ntTelegramEn','ntTelegramFields','ntTgToken','ntTgChatId'])
}

function _applyNotifyChannel(ch, ids){
  const cfg = _notifyConfig[ch]||{}
  const [enId, fieldsId, ...inputIds] = ids
  const enEl = document.getElementById(enId)
  const fieldsEl = document.getElementById(fieldsId)
  if(enEl) enEl.checked = !!cfg.enabled
  if(fieldsEl) fieldsEl.style.display = cfg.enabled ? '' : 'none'
  const keys = ch==='telegram' ? ['token','chat_id'] : ch==='dingtalk' ? ['webhook','secret'] : ['webhook']
  keys.forEach((k,i)=>{
    const el = document.getElementById(inputIds[i])
    if(el && cfg[k]) el.value = cfg[k]
  })
}

function notifyChannelToggle(ch, el){
  const fieldsId = {wecom:'ntWeComFields',feishu:'ntFeiShuFields',dingtalk:'ntDingTalkFields',telegram:'ntTelegramFields'}[ch]
  const fieldsEl = document.getElementById(fieldsId)
  if(fieldsEl) fieldsEl.style.display = el.checked ? '' : 'none'
}

async function saveNotifyConfig(){
  const cfg = {
    wecom:    { enabled: document.getElementById('ntWeComEn')?.checked||false,    webhook: document.getElementById('ntWeComWh')?.value||'' },
    feishu:   { enabled: document.getElementById('ntFeiShuEn')?.checked||false,   webhook: document.getElementById('ntFeiShuWh')?.value||'' },
    dingtalk: { enabled: document.getElementById('ntDingTalkEn')?.checked||false, webhook: document.getElementById('ntDingTalkWh')?.value||'', secret: document.getElementById('ntDingTalkSec')?.value||'' },
    telegram: { enabled: document.getElementById('ntTelegramEn')?.checked||false, token: document.getElementById('ntTgToken')?.value||'', chat_id: document.getElementById('ntTgChatId')?.value||'' },
  }
  const r = await api('/user/notify_config','POST', cfg)
  const msg = document.getElementById('ntSetMsg')
  if(r?.ok){ msg.style.color='var(--gl)'; msg.textContent='✅ 推送配置已保存' }
  else { msg.style.color='var(--rl)'; msg.textContent='❌ 保存失败: '+(r?.error||'') }
}

async function testNotify(){
  const msg = document.getElementById('ntSetMsg')
  msg.style.color='var(--t3)'; msg.textContent='发送中...'
  const r = await api('/notify/test','POST',{})
  if(r?.ok){
    msg.style.color='var(--gl)'
    const res = r.results||{}
    const ok = Object.entries(res).filter(([,v])=>v.ok).map(([k])=>k)
    const fail = Object.entries(res).filter(([,v])=>!v.ok).map(([k,v])=>`${k}(${v.error||'?'})`)
    msg.textContent = `✅ 推送测试完成 成功:${ok.join(',')||'无'} ${fail.length?'失败:'+fail.join(','):''}`
  } else {
    msg.style.color='var(--rl)'
    msg.textContent = '❌ 推送失败: '+(r?.error||'未启用任何渠道')
  }
}
async function doAiGen(){
  const desc = document.getElementById('aiGenDesc').value.trim()
  if(!desc){ document.getElementById('aiGenErr').textContent='请描述需求'; document.getElementById('aiGenErr').style.display=''; return }
  const btn = document.getElementById('aiGenBtn2')
  const st  = document.getElementById('aiGenSt')
  const err = document.getElementById('aiGenErr')
  btn.disabled=true; btn.textContent='⟳ AI生成中...'
  st.style.color='var(--bl)'; st.textContent='正在调用AI，通常需要 30～90 秒，请耐心等待...'
  err.style.display='none'

  const isFactor = _aiGenMode === 'factor'
  const endpoint = isFactor ? '/factors/ai_generate' : '/ai/generate_strategy'

  // 倒计时提示
  let secs = 0
  const ticker = setInterval(()=>{
    secs++
    st.textContent = `AI生成中... 已等待 ${secs}s（通常 30～90s，请勿关闭）`
  }, 1000)

  const r = await api(endpoint,'POST',{description:desc})
  clearInterval(ticker)

  btn.disabled=false
  btn.textContent = isFactor ? '✨ 生成因子' : '✨ 生成策略'

  if(!r){
    err.textContent='❌ 请求超时或网络错误，请刷新页面后重试'
    err.style.display=''; st.textContent=''; return
  }
  if(!r.ok){
    err.textContent='❌ '+(r.error||'生成失败，请检查AI设置')
    err.style.display=''; st.textContent=''; return
  }
  if(!r.code || r.code.length < 100){
    err.textContent=`❌ AI返回代码过短(${r.code?.length||0}字符)，模型未生成完整代码，请重试`
    err.style.display=''; st.textContent=''; return
  }
  // 填入编辑器
  editor.setValue(r.code||'')
  editor.setOption('readOnly', false)

  if(isFactor){
    _curMode = 'factor'
    const m = (r.code||'').match(/"name"\s*:\s*"([^"]+)"/)
    const fn = m&&m[1] ? m[1].replace(/[^a-zA-Z0-9一-龥_]/g,'_').toLowerCase().substring(0,30) : 'ai_factor'
    document.getElementById('sfn').value = fn
    setEdSt('AI因子已生成（未保存）— 请检查后保存')
  } else {
    _curMode = 'strat'
    const m = (r.code||'').match(/"name"\s*:\s*"([^"]+)"/)
    const fn = m&&m[1] ? m[1].replace(/[^a-zA-Z0-9一-龥_]/g,'_').toLowerCase().substring(0,30) : 'ai_strategy'
    document.getElementById('sfn').value = fn
    setEdSt('AI已生成（未保存）— 请检查后保存')
  }

  st.style.color='var(--gl)'; st.textContent='✅ 生成完成！已填入编辑器，请检查后保存。'
  setTimeout(()=>{
    closeAiGen()
    switchPage('strategy', document.querySelector('.ntab[data-page="strategy"]'))
    editor.focus()
  }, 2000)
}
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeAiGen() })


// ══════════════════════════════════════════════════════════
// 认证系统
// ══════════════════════════════════════════════════════════
let _currentUser = null

async function checkAuthState(){
  const r = await api('/auth/me')
  if(r?.logged_in){
    _currentUser = r
    hideAuthOverlay()
    updateNavUser(r)
  } else {
    showAuthOverlay()
  }
}

// ── 实盘下单总闸：一键开/关。关闭=桥接器收不到信号、不下单到同花顺 ──────────
async function refreshLiveTradeBadge(){
  try{
    const r = await api('/bridge/live_enabled')
    const el = document.getElementById('liveTradeBadge')
    if(!el || !r) return
    el.removeAttribute('style')
    if(r.enabled){
      el.className = 'top-switch live-switch on'
      el.innerHTML = '<i></i><b>实盘下单 开</b>'
    }else{
      el.className = 'top-switch live-switch off'
      el.innerHTML = '<i></i><b>实盘下单 已关</b>'
    }
    if(typeof _liveGateOn !== 'undefined'){ _liveGateOn = !!r.enabled }
    if(typeof _renderNavModeBadge === 'function') _renderNavModeBadge()
  }catch(e){}
}

async function toggleLiveTrade(){
  // 读不到状态时默认按「当前关闭」处理 → 点击=尝试开启=触发强确认，最安全
  let cur=false
  try{ const r0=await api('/bridge/live_enabled'); cur = !!(r0 && r0.enabled) }catch(e){}
  const next = !cur
  if(next){
    // 开启 = 危险方向（真金白银下单），必须强确认；防误点意外开闸
    if(!confirm('⚠️ 确定要【开启实盘下单】吗？\n\n开启后桥接器会把选股/策略信号真实下单到同花顺、动用真实资金！\n请先确认：同花顺已登录、下单窗口未被收成迷你条。\n\n确定开启？')) return
  }
  // 关闭 = 安全方向，一键即停，无需确认
  const r = await api('/bridge/live_enabled','POST',{enabled:next})
  if(r && r.ok){
    refreshLiveTradeBadge()
    toast(next?'⚠️ 已开启实盘下单，信号将真实成交':'🛑 已关闭实盘下单（桥接器不再下单）', next?'warn':'success')
  }else{
    toast('操作失败，请重试', 'error')
  }
}

// ── 平台公告：登录后拉未读公告并弹窗，关闭时标记已读 ──────────────
async function loadAnnouncements(){
  try{
    const r = await api('/announcements')
    if(!r || !r.ok || !Array.isArray(r.announcements)) return
    const unread = r.announcements.filter(a => !a.is_read)
    if(unread.length) showAnnouncements(unread)
  }catch(e){ console.warn('公告拉取失败', e) }
}

function showAnnouncements(list){
  const typeColor = {info:'#3b82f6', warn:'#f59e0b', warning:'#f59e0b', error:'#ef4444', success:'#22c55e'}
  const old = document.getElementById('annOverlay'); if(old) old.remove()
  const ov = document.createElement('div')
  ov.id = 'annOverlay'
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(58,44,34,.45);backdrop-filter:blur(3px);display:flex;align-items:center;justify-content:center;z-index:99999'
  const cards = list.map(a=>`
    <div style="border-left:4px solid ${typeColor[a.type]||'#3b82f6'};background:rgba(127,127,127,.08);border-radius:8px;padding:14px 16px;margin-bottom:12px">
      <div style="font-weight:700;font-size:15px;margin-bottom:6px">${esc(a.title||'公告')}</div>
      <div style="font-size:13px;line-height:1.6;opacity:.85;white-space:pre-wrap">${esc(a.content||'')}</div>
      <div style="font-size:11px;opacity:.5;margin-top:8px">${esc(a.created_at||'')}</div>
    </div>`).join('')
  ov.innerHTML = `
    <div style="background:#FFFDFA;border:1px solid #EADCC9;border-radius:16px;max-width:560px;width:90%;max-height:80vh;overflow:auto;padding:20px;box-shadow:0 16px 48px rgba(120,90,60,.25);color:#3A2E24">
      <div style="font-size:18px;font-weight:800;margin-bottom:16px">📢 平台公告</div>
      ${cards}
      <div style="text-align:right;margin-top:8px">
        <button id="annOkBtn" style="background:linear-gradient(135deg,#FF7B93,#B580E8);color:#fff;border:none;border-radius:8px;padding:9px 22px;font-size:14px;cursor:pointer">我知道了</button>
      </div>
    </div>`
  document.body.appendChild(ov)
  document.getElementById('annOkBtn').addEventListener('click', async ()=>{
    for(const a of list){ try{ await api('/announcements/'+a.id+'/read','POST',{}) }catch(e){} }
    ov.remove()
  })
}

function showAuthOverlay(){
  document.getElementById('authOverlay').style.display = 'flex'
}
function hideAuthOverlay(){
  document.getElementById('authOverlay').style.display = 'none'
}

function switchAuthTab(tab, el){
  document.querySelectorAll('.auth-tab').forEach(t=>t.classList.remove('active'))
  document.querySelectorAll('.auth-form').forEach(f=>f.classList.remove('active'))
  el.classList.add('active')
  document.getElementById('auth' + tab.charAt(0).toUpperCase() + tab.slice(1) + 'Form').classList.add('active')
}

function togglePwd(inputId, eyeEl){
  const inp = document.getElementById(inputId)
  const show = inp.type === 'password'
  inp.type = show ? 'text' : 'password'
  eyeEl.style.opacity = show ? '1' : '0.4'
}

async function doLogin(){
  const btn = document.getElementById('loginBtn')
  const msg = document.getElementById('loginMsg')
  const username = document.getElementById('loginUser').value.trim()
  const password = document.getElementById('loginPass').value
  if(!username || !password){ setAuthMsg(msg,'请填写用户名和密码','err'); return }
  btn.disabled = true; btn.textContent = '登录中...'
  const r = await api('/auth/login','POST',{username, password})
  btn.disabled = false; btn.textContent = '登录'
  if(r?.ok){
    _currentUser = r
    hideAuthOverlay()
    updateNavUser(r)
    initPublicShell()
    initAuthenticatedShell()
  } else {
    setAuthMsg(msg, r?.error || '登录失败', 'err')
  }
}

async function doRegister(){
  const btn = document.getElementById('regBtn')
  const msg = document.getElementById('regMsg')
  const inviteCode = document.getElementById('regInvite').value.trim()
  const username = document.getElementById('regUser').value.trim()
  const email    = document.getElementById('regEmail').value.trim()
  const password = document.getElementById('regPass').value
  if(!inviteCode){ setAuthMsg(msg,'请输入邀请码','err'); return }
  if(!username||!email||!password){ setAuthMsg(msg,'请填写所有字段','err'); return }
  // 前端即时校验（此前只靠后端报错，来回一趟才知道格式不对）
  if(username.length < 2){ setAuthMsg(msg,'用户名至少 2 个字符','err'); return }
  if(!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)){ setAuthMsg(msg,'邮箱格式不正确','err'); return }
  if(password.length < 6){ setAuthMsg(msg,'密码至少 6 位','err'); return }
  btn.disabled = true; btn.textContent = '注册中...'
  const r = await api('/auth/register','POST',{username, email, password, invite_code: inviteCode})
  btn.disabled = false; btn.textContent = '注册账号'
  if(r?.ok){
    setAuthMsg(msg,'✅ 注册成功！正在登录...','ok')
    // 自动登录
    const lr = await api('/auth/login','POST',{username, password})
    if(lr?.ok){
      _currentUser = lr
      setTimeout(()=>{
        hideAuthOverlay()
        updateNavUser(lr)
        initPublicShell()
        initAuthenticatedShell()
      }, 800)
    }
  } else {
    setAuthMsg(msg, r?.error || '注册失败', 'err')
  }
}

function setAuthMsg(el, text, cls){
  el.textContent = text
  el.className = 'auth-msg ' + (cls||'')
  if(cls==='ok') setTimeout(()=>el.textContent='', 4000)
}

// ── 找回密码 / 重置密码 ──────────────────────────────
let _resetToken = ''
function _hideAuthTabs(hide){ const t=document.querySelector('.auth-tabs'); if(t) t.style.display = hide?'none':'' }
function showForgot(){
  document.querySelectorAll('.auth-form').forEach(f=>f.classList.remove('active'))
  _hideAuthTabs(true)
  document.getElementById('authForgotForm').classList.add('active')
}
function backToLogin(){
  _hideAuthTabs(false)
  document.querySelectorAll('.auth-form').forEach(f=>f.classList.remove('active'))
  const loginTab = document.querySelector('.auth-tab')
  if(loginTab) switchAuthTab('login', loginTab)
}
async function doForgot(){
  const btn=document.getElementById('forgotBtn'), msg=document.getElementById('forgotMsg')
  const ident=document.getElementById('forgotIdent').value.trim()
  if(!ident){ setAuthMsg(msg,'请输入注册邮箱或用户名','err'); return }
  btn.disabled=true; btn.textContent='提交中...'
  const r=await api('/auth/forgot','POST',{email:ident})
  btn.disabled=false; btn.textContent='提交找回申请'
  if(r?.ok){ setAuthMsg(msg, r.msg||'已提交找回申请，请联系管理员处理', 'ok') }
  else{ setAuthMsg(msg, r?.error||'提交失败', 'err') }
}
async function showResetForm(token){
  _resetToken = token
  document.querySelectorAll('.auth-form').forEach(f=>f.classList.remove('active'))
  _hideAuthTabs(true)
  document.getElementById('authResetForm').classList.add('active')
  const r = await api('/auth/reset/validate?token='+encodeURIComponent(token))
  if(r?.ok){
    document.getElementById('resetUsernameHint').textContent = '为账号「'+(r.username||'')+'」设置新密码'
  } else {
    document.getElementById('authResetForm').innerHTML =
      '<div class="auth-msg err" style="text-align:center">重置链接无效或已过期，请重新申请。</div>'+
      '<div style="text-align:center;margin-top:12px"><span data-ui-click="clearResetUrl();backToLogin()" style="font-size:12px;color:var(--t3,#888);cursor:pointer">← 返回登录</span></div>'
  }
}
async function doReset(){
  const btn=document.getElementById('resetBtn'), msg=document.getElementById('resetMsg')
  const p1=document.getElementById('resetPass').value, p2=document.getElementById('resetPass2').value
  if(!p1||p1.length<6){ setAuthMsg(msg,'新密码至少6位','err'); return }
  if(p1!==p2){ setAuthMsg(msg,'两次输入的密码不一致','err'); return }
  btn.disabled=true; btn.textContent='提交中...'
  const r=await api('/auth/reset','POST',{token:_resetToken, new_password:p1})
  btn.disabled=false; btn.textContent='设置新密码'
  if(r?.ok){ setAuthMsg(msg,'✅ 密码已重置，请用新密码登录','ok'); setTimeout(()=>{ clearResetUrl(); backToLogin() },1500) }
  else{ setAuthMsg(msg, r?.error||'重置失败','err') }
}
function clearResetUrl(){ try{ history.replaceState({},'',location.pathname) }catch(e){} }

function updateNavUser(user){
  const el = document.getElementById('navUser')
  el.style.display = 'flex'
  document.getElementById('navUsername').textContent = user.username
  document.getElementById('navPoints').textContent   = user.points
  // 侧栏底部用户区（导航改造后的主入口）
  const foot = document.getElementById('sideFoot')
  if(foot){
    foot.classList.add('on')   // 用 class 而非内联 style：内联会击穿 760px 断点的隐藏规则
    const av = document.getElementById('sideAvatar')
    if(av) av.textContent = (user.username || '·').slice(0,1).toUpperCase()
    const un = document.getElementById('sideUname')
    if(un) un.textContent = user.username
    const sp = document.getElementById('sidePoints')
    if(sp) sp.textContent = user.points
  }
  const mobileUser = document.getElementById('mobileUser')
  if(mobileUser){
    mobileUser.classList.add('on')
    const av = document.getElementById('mobileAvatar')
    if(av) av.textContent = (user.username || '·').slice(0,1).toUpperCase()
    const un = document.getElementById('mobileUsername')
    if(un) un.textContent = user.username
    const mp = document.getElementById('mobilePoints')
    if(mp) mp.textContent = user.points
  }
  document.getElementById('navAdminBtn').style.display = user.is_admin ? 'inline-flex' : 'none'
  const sa = document.getElementById('sideAdminBtn')
  if(sa) sa.style.display = user.is_admin ? 'flex' : 'none'
  const ma = document.getElementById('mobileAdminItem')
  if(ma) ma.style.display = user.is_admin ? '' : 'none'
  // 系统设置（AI接口/券商通道/消息推送）仅管理员可见。
  // 注：原实现用 [onclick*="aiset"] 选择器，但这些元素用的是 data-ui-click，
  //     选择器从来没匹配上过 —— 非管理员其实一直能看到入口。这里改为按真实入口隐藏。
  const setTab = document.getElementById('sideSettingsTab')
  if(setTab) setTab.style.display = user.is_admin ? '' : 'none'
  const mobileSettings = document.getElementById('mobileSettingsItem')
  if(mobileSettings) mobileSettings.style.display = user.is_admin ? '' : 'none'
}

async function doLogout(){
  await api('/auth/logout','POST',{})
  _currentUser = null
  document.getElementById('navUser').style.display = 'none'
  const foot = document.getElementById('sideFoot')
  if(foot) foot.classList.remove('on')
  const mobileUser = document.getElementById('mobileUser')
  if(mobileUser) mobileUser.classList.remove('on')
  toggleMobileMore(false)
  showAuthOverlay()
}

// 积分不足提示
function showPointsWarn(msg){
  const el = document.getElementById('pointsWarn')
  el.textContent = '⚡ ' + msg
  el.style.display = 'block'
  clearTimeout(el._t)
  el._t = setTimeout(()=>el.style.display='none', 4000)
}

// 刷新导航栏积分显示
async function refreshNavPoints(){
  const r = await api('/user/points')
  if(r?.ok && document.getElementById('navPoints')){
    document.getElementById('navPoints').textContent = r.points
    const sp = document.getElementById('sidePoints')
    if(sp) sp.textContent = r.points
    const mp = document.getElementById('mobilePoints')
    if(mp) mp.textContent = r.points
    if(_currentUser) _currentUser.points = r.points
  }
}

// 拦截 402 积分不足响应（覆盖原有 api 函数）
const _origApi = api
window.api = async function(path, method='GET', body=null){
  const r = await _origApi(path, method, body)
  if(r && r.ok === false && r.error && r.error.includes('积分不足')){
    showPointsWarn(r.error)
    refreshNavPoints()
  }
  return r
}
