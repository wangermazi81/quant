// ── 全局错误捕获，确保能看到JS错误 ──
window.onerror = function(msg, src, line, col, err){
  const el = document.getElementById('btResult')
  if(el) el.innerHTML = `<div style="padding:16px"><div style="background:#2d1a1e;border:1px solid #6e1c24;border-radius:8px;padding:14px;color:#f85149;font-size:13px"><b>⚠ JavaScript 错误（请截图报告）</b><pre style="margin-top:8px;font-size:11px;white-space:pre-wrap">${msg}\n第${line}行</pre></div></div>`
  return false
}

const API = '/api'
const MOBILE_API_FALLBACK = `${location.protocol}//${location.hostname}:5002/api`
let editor, curStrat=null, scResults=[], charts={}, dlTaskId=null, dlTimer=null
let _screenRunPromise = null
let _bridgePollStarted = false
let _bridgeStatusPromise = null

function _coreActionAttrs(action, payload){
  return `data-core-action="${action}" data-core-payload="${encodeURIComponent(JSON.stringify(payload || {}))}"`
}

document.addEventListener('click', e=>{
  const actionTarget = e.target.closest('[data-action]')
  const stopTarget = e.target.closest('[data-stop-modal]')
  if(stopTarget && (!actionTarget || !stopTarget.contains(actionTarget))){
    e.stopPropagation()
    return
  }
  if(actionTarget && handleUiAction(actionTarget, e)) return

  const target=e.target.closest('[data-core-action]')
  if(!target){
    if(e.target.closest('[data-core-stop]')) e.stopPropagation()
    return
  }
  e.stopPropagation()
  let payload
  try{payload=JSON.parse(decodeURIComponent(target.dataset.corePayload||'%7B%7D'))}catch(err){return}
  const actions={
    'show-kline': p=>showKLine(p.code,p.name),
    'add-one-q': p=>addOneQ(p),
    'manual-buy': p=>manualBuy(p),
  }
  const action=actions[target.dataset.coreAction]
  if(action) action(payload)
})

function handleUiAction(target, e){
  const action = target.dataset.action
  const actions = {
    'open-execution-console': () => openTodayOrders(),
    'close-today-orders': () => closeTodayOrders(),
    'close-today-orders-backdrop': () => closeTodayOrders(e),
    'refresh-execution': () => refreshExecutionState(),
    'mobile-mode': () => switchMobileMode(target.dataset.mode || 'simulation'),
    'toggle-bridge-process': () => toggleBridgeProcess(),
    'restart-mobile-bridge': () => restartMobileBridge(),
    'mobile-connection': () => setMobileConnection(target.dataset.mode || 'usb'),
    'open-mobile-ths': () => openMobileThs(),
    'refresh-mobile-funds': async () => { await refreshMobileFunds(); refreshExecutionState() },
    'toggle-trade-mode': () => toggleTradeMode(),
    'toggle-live-trade': () => toggleLiveTrade(),
    'save-trade-settings': () => saveTradeSettingsPanel(),
    'refresh-account-snapshot': async () => { await refreshAccountSnapshot(); refreshExecutionState() },
    'refresh-today-orders': () => refreshTodayOrders(),
  }
  const fn = actions[action]
  if(!fn) return false
  e.preventDefault()
  e.stopPropagation()
  fn()
  return true
}

window.addEventListener('load', async ()=>{
  // 先检查登录状态，未登录则显示弹层
  await checkAuthState()

  // 密码重置链接：URL 带 ?pwreset=TOKEN → 弹出重置表单（无论登录与否）
  const _pwToken = new URLSearchParams(location.search).get('pwreset')
  if(_pwToken){ showAuthOverlay(); showResetForm(_pwToken) }

  editor = CodeMirror.fromTextArea(document.getElementById('codeArea'),{
    mode:'python',theme:'dracula',lineNumbers:true,
    indentUnit:4,tabSize:4,indentWithTabs:false,lineWrapping:false,
    extraKeys:{"Tab":cm=>cm.execCommand("indentMore"),"Shift-Tab":cm=>cm.execCommand("indentLess")}
  })
  editor.setSize('100%','100%')
  editor.on('change',()=>setEdSt('已修改（未保存）'))
  initPublicShell()
  initAuthenticatedShell()
  // 兜底恢复 URL hash 指向的页面：initAuthenticatedShell 在未登录时会提前 return，
  // 若只在那里调用，未登录（或登录态尚未就绪）时 F5 会退回监控中心。
  _gotoHash()
})

function initPublicShell(){
  loadStratList(); loadDbList(); loadMarketContext()
}

function initAuthenticatedShell(){
  if(!_currentUser) return
  loadAnnouncements()
  refreshLiveTradeBadge()
  if(!_bridgePollStarted){
    _bridgePollStarted = true
    pollEvery(refreshBridgeProcessBadge, 15000)
  }
  startPoll()
  initAI()
  initBrokerConfig()
  initNotifyConfig()
  loadTradeMode()
  _gotoHash()   // 登录初始化后，按 URL hash 恢复到刷新前的页面
}

function bridgeProcessErrorText(r, path=''){
  const msg = (r && (r.msg || r.error)) || '请求没有返回有效结果'
  const bits = []
  if(path) bits.push(path)
  if(r?.status) bits.push('HTTP '+r.status)
  if(r?.network) bits.push('网络连接失败')
  const host = location.hostname || ''
  if(['127.0.0.1','localhost'].includes(host)) bits.push('手机访问时要用电脑局域网IP，不是 '+host)
  bits.push(msg)
  return bits.join(' ｜ ')
}

function _setSwitchBadge(el, cls, text, title){
  if(!el) return
  el.className = cls
  el.innerHTML = `<i></i><b>${text}</b>`
  if(title) el.title = title
}

// ── 监控页 KPI 指标条 ──────────────────────────────────────────
// 只用已有数据源：fetchSt(状态/护栏) + fetchPositions(持仓) + fetchFunds(可用资金)。
// 拿不到的一律显示「—」，不编造数字。
const _kpi = {equity:null, avail:null, mktValue:null, pnl:null, posCount:null, maxPos:null,
              today:null, maxDaily:null, auto:null}
function _kpiMoney(v){
  if(v==null || !isFinite(v)) return '—'
  return '¥' + (+v).toLocaleString('zh-CN',{maximumFractionDigits:0})
}
function _renderKpiBar(){
  const set = (id, text, cls) => {
    const el = document.getElementById(id); if(!el) return
    el.textContent = text
    el.className = 'kpi-value' + (cls ? ' '+cls : '')
  }
  const setSub = (id, text) => { const el=document.getElementById(id); if(el && text!=null) el.textContent = text }

  const net = _kpi.equity!=null
    ? +_kpi.equity
    : ((_kpi.avail!=null || _kpi.mktValue!=null)
      ? (+(_kpi.avail||0) + +(_kpi.mktValue||0)) : null)
  set('kpiNetAsset', _kpiMoney(net))
  setSub('kpiNetAssetSub', _kpi.mktValue!=null ? `可用 ${_kpiMoney(_kpi.avail)} + 市值 ${_kpiMoney(_kpi.mktValue)}` : '可用 + 持仓市值')

  if(_kpi.pnl==null) set('kpiPnl','—')
  else set('kpiPnl', (_kpi.pnl>=0?'+':'') + _kpiMoney(_kpi.pnl).replace('¥','¥'),
           _kpi.pnl>0?'up':(_kpi.pnl<0?'down':''))

  set('kpiAvail', _kpiMoney(_kpi.avail))

  set('kpiPosCount', _kpi.posCount==null ? '—' : String(_kpi.posCount),
      (_kpi.maxPos && _kpi.posCount>=_kpi.maxPos) ? 'warn' : '')
  setSub('kpiPosSub', _kpi.maxPos ? `只 / 上限 ${_kpi.maxPos}` : '只')

  set('kpiToday', _kpi.today==null ? '—' : String(_kpi.today),
      (_kpi.maxDaily && _kpi.today>=_kpi.maxDaily) ? 'warn' : '')
  setSub('kpiTodaySub', _kpi.maxDaily ? `笔 / 日限 ${_kpi.maxDaily}` : '笔')

  if(_kpi.auto==null) set('kpiAuto','—')
  else set('kpiAuto', _kpi.auto ? '开启' : '关闭', _kpi.auto ? 'warn' : '')
  setSub('kpiAutoSub', _kpi.auto ? '信号将自动下单' : '需人工确认')
}

function _renderCurrentModeBadge(id, mode, label){
  const el = document.getElementById(id)
  if(!el) return
  const raw = String(mode || '').toLowerCase()
  const isLive = raw === 'live' || raw === 'real'
  const isPaper = raw === 'paper' || raw === 'simulation' || raw === 'sim'
  const text = isLive ? '实盘' : (isPaper ? '模拟盘' : '读取中')
  el.className = 'exec-current-mode ' + (isLive ? 'live' : (isPaper ? 'paper' : 'unknown'))
  el.innerHTML = `<i></i><b>${label}当前：${text}</b>`
  el.title = `${label}当前交易环境：${text}`
}

// ── 全局轻提示（此前 toast() 从未定义，导致启动/停止/实盘总闸切换全部静默）──
// type: 'success' | 'error' | 'warn' | ''(信息)；兼容旧调用传布尔（true=错误）
function toast(msg, type){
  if(type === true) type = 'error'
  else if(type === false || type == null) type = ''
  let wrap = document.getElementById('_jwToastWrap')
  if(!wrap){
    wrap = document.createElement('div'); wrap.id = '_jwToastWrap'
    wrap.style.cssText = 'position:fixed;top:74px;right:20px;z-index:100000;display:flex;flex-direction:column;gap:8px;pointer-events:none'
    document.body.appendChild(wrap)
  }
  const palette = {
    success: ['#E7F7EC', '#1E7E45', '#B6E4C6'],
    error:   ['#FDEAEE', '#C1274B', '#F3C2CE'],
    warn:    ['#FFF6E5', '#8A5D00', '#F2DCA6'],
    '':      ['#FFFFFF', '#3A2E24', '#EADCC9'],
  }
  const [bg, fg, bd] = palette[type] || palette['']
  const el = document.createElement('div')
  el.textContent = String(msg == null ? '' : msg)
  el.style.cssText = `pointer-events:auto;min-width:180px;max-width:340px;padding:10px 14px;border-radius:12px;background:${bg};color:${fg};border:1px solid ${bd};box-shadow:0 8px 24px rgba(120,90,60,.18);font-size:13px;font-weight:600;line-height:1.5;white-space:pre-line;opacity:0;transform:translateX(12px);transition:opacity .18s ease,transform .18s ease`
  wrap.appendChild(el)
  requestAnimationFrame(()=>{ el.style.opacity = '1'; el.style.transform = 'none' })
  setTimeout(()=>{ el.style.opacity = '0'; el.style.transform = 'translateX(12px)'; setTimeout(()=>el.remove(), 220) }, type === 'error' ? 4200 : 3000)
}
window.toast = toast

// ── 顶栏常驻「模拟 / 实盘」徽章：真钱平台最关键的状态，任何页面一眼可见 ──
let _liveGateOn = null   // 实盘下单总闸状态（由 refreshLiveTradeBadge 写入）
function _renderNavModeBadge(){
  const el = document.getElementById('navModeBadge')
  if(!el) return
  const isLive = (typeof _tradeMode !== 'undefined' && _tradeMode === 'live')
  if(isLive){
    const gate = _liveGateOn === true ? '·闸开' : (_liveGateOn === false ? '·闸关' : '')
    el.className = 'nav-mode-badge live'
    el.textContent = '🔴 实盘' + gate
    el.title = '当前为实盘模式' + (_liveGateOn === true ? '，下单总闸已开=会真扣钱真买卖' : (_liveGateOn === false ? '，下单总闸已关（暂不会真下单）' : '')) + '。点击打开交易执行台'
    el.style.cssText = 'display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:800;cursor:pointer;background:#FDE7ED;color:#C1274B;border:1px solid #F0A9BC'
  } else if(_liveGateOn === true){
    // 模拟盘 + 实盘总闸已开：现在不会真下单，但只要模式一切到实盘就立刻真成交，
    // 属于"一步之遥"状态，必须在徽章上点出来，不能只显示一个安全的绿色模拟盘。
    el.className = 'nav-mode-badge paper gate-open'
    el.textContent = '🛡️ 模拟盘 · 闸已开'
    el.title = '当前为模拟盘（不动真钱），但实盘下单总闸已开启——一旦切到实盘，信号会立即真实成交。点击打开交易执行台'
    el.style.cssText = 'display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:800;cursor:pointer;background:#FFF6E5;color:#8A5D00;border:1px solid #F2DCA6'
  } else {
    el.className = 'nav-mode-badge paper'
    el.textContent = '🛡️ 模拟盘'
    el.title = '当前为模拟盘模式（不动真钱）。点击打开交易执行台'
    el.style.cssText = 'display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:800;cursor:pointer;background:#E7F5EE;color:#2A7A50;border:1px solid #B6E0C8'
  }
  syncMobileModeState()
}

async function refreshBridgeProcessBadge(){
  if(!_currentUser) return null
  if(_bridgeStatusPromise) return _bridgeStatusPromise
  _pollInflight.bridge = true
  _bridgeStatusPromise = (async()=>{
    const bridgeEl = document.getElementById('bridgeProcessBadge')
    const topEl = document.getElementById('mobileTradeBadge')
    const r = await api('/bridge/process')
    if(!r || !r.ok){
      const title = '读取桥接器后台状态失败：' + bridgeProcessErrorText(r, '/api/bridge/process')
      _setSwitchBadge(bridgeEl, 'bridge-switch checking', '桥接器 未知', title)
      _setSwitchBadge(topEl, 'bridge-switch checking', '手机交易 未知', title)
      return null
    }
    if(r.running){
      _setSwitchBadge(bridgeEl, 'bridge-switch on', `桥接器 开 ${r.pid||''}`, '点击关闭后台桥接器进程')
      _setSwitchBadge(topEl, 'bridge-switch on', '手机交易 已连接', '打开交易执行控制台：桥接器运行中')
    }else{
      _setSwitchBadge(bridgeEl, 'bridge-switch off', '桥接器 关', '点击后台启动桥接器，不弹 CMD 窗口')
      _setSwitchBadge(topEl, 'bridge-switch off', '手机交易 未启动', '打开交易执行控制台：桥接器未启动')
    }
    return r
  })()
  try{
    return await _bridgeStatusPromise
  }finally{
    _pollInflight.bridge = false
    _bridgeStatusPromise = null
  }
}

async function toggleBridgeProcess(){
    const el = document.getElementById('bridgeProcessBadge') || document.getElementById('mobileTradeBadge')
    if(el){
      el.classList.add('busy')
      el.innerHTML = '<i></i><b>处理中</b>'
    }
    const cur = await refreshBridgeProcessBadge()
    const path = cur && cur.running ? '/bridge/process/stop' : '/bridge/process/start'
    const r = await api(path, 'POST', {})
    if(el) el.classList.remove('busy')
    await refreshBridgeProcessBadge()
    toast(r && r.ok ? (r.running ? '桥接器已后台运行' : '桥接器已关闭') : ('桥接器操作失败：' + bridgeProcessErrorText(r, '/api'+path)), r && r.ok ? 'success' : 'error')
  }

function _setMobileChainStatus(text, ok=null){
  const el = document.getElementById('mobileChainStatus')
  if(!el) return
  const color = ok === true ? '#368B63' : (ok === false ? '#D6456A' : 'var(--t2)')
  el.style.color = color
  if(el.classList && el.classList.contains('exec-human-status')){
    el.innerHTML = '<i></i><span>'+esc(text)+'</span>'
    return
  }
  el.textContent = text
}

async function refreshMobileProfileBadge(){
  const r = await api('/mobile_ths/profile')
  const connEl = document.getElementById('mobileConnText')
  const modeEl = document.getElementById('mobileModeText')
  const profileEl = document.getElementById('mobileProfileText')
  if(!r || !r.ok){
    const msg = (r && (r.msg || r.error)) || '读取失败'
    if(connEl) connEl.textContent = '连接：读取失败'
    if(modeEl) modeEl.textContent = '模式：读取失败'
    if(profileEl) profileEl.textContent = msg
    _setMobileChainStatus('手机配置读取失败：' + msg, false)
    return null
  }
  const conn = r.connection_mode || 'usb'
  const mode = r.trade_mode === 'live' ? '实盘' : (r.trade_mode === 'simulation' ? '模拟盘' : (r.trade_mode || '未知'))
  const confirm = r.confirm_mode === 'auto' ? '全自动确认' : '手动确认'
  const task = r.mobile_task || {}
  _renderCurrentModeBadge('mobileCurrentModeBadge', r.trade_mode || 'unknown', '手机')
  if(connEl) connEl.textContent = `连接：${conn.toUpperCase()}${r.device_serial ? ' · '+r.device_serial : ''}`
  if(modeEl) modeEl.textContent = `模式：${mode}`
  if(profileEl) profileEl.textContent = `设备：${r.device_serial || '未绑定'} ｜ 确认：${confirm}`
  if(task.running) _setMobileChainStatus(`手机任务执行中：${task.action || 'unknown'} ${task.target || ''}`, null)
  else if(task.msg) _setMobileChainStatus(`手机任务：${task.msg}`, task.ok)
  else _setMobileChainStatus(`链路配置：${conn.toUpperCase()} / ${mode} / ${confirm}`, true)
  return r
}

async function _mobilePost(path, body, busyText){
  _setMobileChainStatus(busyText || '手机任务提交中...', null)
  const r = await api(path, 'POST', body || {})
  const busy = !!(r && r.busy)
  const ok = !!(r && r.ok && !busy)
  const msg = (r && (r.msg || r.error)) || (busy ? '手机任务执行中' : '任务失败')
  _setMobileChainStatus(busy ? msg : (ok ? msg || '任务已提交' : msg), busy ? null : ok)
  await refreshMobileProfileBadge()
  await refreshBridgeProcessBadge()
  if(path === '/mobile_ths/mode') refreshExecutionState()
  return r
}
function _mobileTaskId(resp){ return resp && resp.task && resp.task.task_id ? String(resp.task.task_id) : '' }
function _mobileAccountPayload(profile){
  const task = profile && profile.mobile_task || {}
  if(task.action === 'account' && task.result && typeof task.result === 'object') return task.result
  if(task.action === 'account' && task.msg && typeof task.msg === 'object') return task.msg
  return profile && profile.last_account_snapshot || null
}
async function waitMobileTaskDone(action, taskId, timeoutMs=65000){
  const deadline = Date.now() + timeoutMs
  let lastProfile = null
  while(Date.now() < deadline){
    await new Promise(resolve => setTimeout(resolve, 2500))
    const profile = await refreshMobileProfileBadge()
    if(!profile || !profile.ok) continue
    lastProfile = profile
    const task = profile.mobile_task || {}
    const sameAction = !action || task.action === action
    const sameTask = !taskId || String(task.task_id || '') === String(taskId)
    if(sameAction && sameTask && task.running === false) return profile
  }
  return lastProfile || await refreshMobileProfileBadge()
}

function setMobileConnection(mode){ return _mobilePost('/mobile_ths/connection', {connection_mode: mode}, '正在切换手机连接方式...') }
function switchMobileMode(mode){ return _mobilePost('/mobile_ths/mode', {mode}, '正在切换手机交易模式...') }
function openMobileThs(){ return _mobilePost('/mobile_ths/open_ths', {}, '正在打开手机同花顺...') }
function restartMobileBridge(){ return _mobilePost('/mobile_ths/restart_bridge', {}, '正在后台重启桥接器...') }

function setEdSt(s){ document.getElementById('edSt').textContent=s }

const _NAV_PAGES = ['monitor','execution','strategy','paper','search','review','aitrader','risk','settings']
const _PAGE_TITLES = {monitor:'\ud83d\udcca 监控中心', execution:'\u26a1 交易执行', strategy:'\ud83e\uddea 策略中心', paper:'\ud83d\udcb9 模拟盘',
                      search:'\ud83e\uddec 产业链研报', review:'\ud83d\udd01 复盘', aitrader:'\ud83e\udd16 AI交易',
                      risk:'\ud83d\udee1\ufe0f 风控中心', settings:'\u2699 系统设置'}
const _mobileLayoutQuery = window.matchMedia('(max-width: 1024px)')
let _mobileStrategyPane = 'library'
let _mobileRunLabel = '已停止'
function _syncMobileSheetLock(){
  const open = !!document.querySelector('.mobile-more-sheet.show,.mobile-run-sheet.show')
  document.body.classList.toggle('mobile-sheet-open', open)
}
function setMobileLayoutMode(){
  const enabled = _mobileLayoutQuery.matches
  document.body.classList.toggle('mobile-layout', enabled)
  if(!enabled){
    toggleMobileMore(false)
    toggleMobileRunSheet(false)
  }
  const activePage = document.querySelector('.page.active')
  syncMobileHeader(activePage ? activePage.id.replace(/^page-/,'') : 'monitor')
  if(enabled && editor) setTimeout(()=>editor.refresh(), 60)
}
function syncMobileViewport(){
  const height = window.visualViewport ? window.visualViewport.height : window.innerHeight
  document.documentElement.style.setProperty('--mobile-viewport-height', `${Math.round(height)}px`)
}
function syncMobileHeader(name){
  const title = document.getElementById('mobilePageTitle')
  const label = (_PAGE_TITLES[name] || name || '监控中心').replace(/^\S+\s*/, '')
  if(title) title.textContent = label
}
function renderRunState(monitoring,label){
  const text = monitoring ? (label || '运行中') : '已停止'
  _mobileRunLabel = text
  const rb = document.getElementById('runBadge')
  if(rb){
    rb.textContent = monitoring ? '运行中' : '已停止'
    rb.className = 'badge ' + (monitoring ? 'run' : 'stop')
  }
  const state = document.getElementById('mobileRunState')
  if(state){
    state.className = 'mobile-state ' + (monitoring ? 'run-on' : 'run-stop')
    const value = state.querySelector('b')
    if(value) value.textContent = monitoring ? '运行中' : '已停止'
  }
  const detail = document.getElementById('mobileRunDetail')
  if(detail) detail.textContent = text
}
function syncMobileModeState(){
  const isLive = typeof _tradeMode !== 'undefined' && _tradeMode === 'live'
  const gate = _liveGateOn === true ? ' · 闸开' : (_liveGateOn === false ? ' · 闸关' : '')
  const shortText = isLive ? '实盘' : '模拟盘'
  const detailText = shortText + gate
  const state = document.getElementById('mobileModeState')
  if(state){
    state.className = 'mobile-state ' + (isLive ? 'mode-live' : 'mode-paper')
    const value = state.querySelector('b')
    if(value) value.textContent = shortText
  }
  const detail = document.getElementById('mobileModeDetail')
  if(detail) detail.textContent = detailText
}
function mobileNavigate(name){
  const tab = document.querySelector(`.side .ntab[data-page="${name}"]`)
  if(tab) tab.click()
  toggleMobileMore(false)
  toggleMobileRunSheet(false)
}
function syncMobileNavigation(name){
  const primary = ['monitor','execution','strategy','review']
  document.querySelectorAll('.mobile-dock-item').forEach(item => {
    const target = item.dataset.mobilePage
    const active = primary.includes(name) ? target === name : target === 'more'
    item.classList.toggle('active', active)
    item.setAttribute('aria-current', active ? 'page' : 'false')
  })
}
function toggleMobileMore(open){
  const sheet = document.getElementById('mobileMoreSheet')
  if(!sheet) return
  const show = open === undefined ? !sheet.classList.contains('show') : !!open
  if(show) toggleMobileRunSheet(false)
  sheet.classList.toggle('show', show)
  sheet.setAttribute('aria-hidden', String(!show))
  const trigger = document.querySelector('.mobile-dock-item[data-mobile-page="more"]')
  if(trigger) trigger.setAttribute('aria-expanded', String(show))
  _syncMobileSheetLock()
}
function toggleMobileRunSheet(open){
  const sheet = document.getElementById('mobileRunSheet')
  if(!sheet) return
  const show = open === undefined ? !sheet.classList.contains('show') : !!open
  if(show) toggleMobileMore(false)
  sheet.classList.toggle('show', show)
  sheet.setAttribute('aria-hidden', String(!show))
  const trigger = document.getElementById('mobileRunTrigger')
  if(trigger) trigger.setAttribute('aria-expanded', String(show))
  _syncMobileSheetLock()
}
function setMobileStrategyPane(pane){
  if(!['library','editor','result'].includes(pane)) return
  _mobileStrategyPane = pane
  const wrap = document.querySelector('#page-strategy .strat-wrap')
  if(wrap) wrap.dataset.mobilePane = pane
  document.querySelectorAll('[data-mobile-strategy-pane]').forEach(button => {
    const active = button.dataset.mobileStrategyPane === pane
    button.classList.toggle('active', active)
    button.setAttribute('aria-selected', String(active))
  })
  if(pane === 'editor' && editor) setTimeout(()=>editor.refresh(), 40)
}
function switchPage(name,el){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'))
  document.querySelectorAll('.ntab').forEach(t=>t.classList.remove('active'))
  document.getElementById('page-'+name).classList.add('active')
  if(el && el.classList) el.classList.add('active')
  else { const t=document.querySelector(`.ntab[data-page="${name}"]`); if(t) t.classList.add('active') }
  const title = document.getElementById('navTitle')
  if(title && _PAGE_TITLES[name]) title.textContent = _PAGE_TITLES[name]
  // 写 URL hash：F5 刷新保持当前页 + 浏览器前进/后退可用（此前纯 class 切换，刷新永远回监控中心）
  if(_NAV_PAGES.includes(name) && location.hash.slice(1) !== name){
    location.hash = name
  }
  syncMobileNavigation(name)
  syncMobileHeader(name)
  toggleMobileMore(false)
  if(typeof aitSetPageActive === 'function') aitSetPageActive(name==='aitrader')
  if(_currentUser && _isCorePollingPage()){ fetchSt(); fetchPositions(); fetchFunds() }
  if(_isMarketContextPage()) loadMarketContext(false)
  if(name==='strategy'&&editor) setTimeout(()=>editor.refresh(),80)
}
// 按 URL hash 恢复页面：点对应页签（复用其 data-ui-click，连同 loadPaperPage/initReviewPage 等初始化一并触发）
function _gotoHash(){
  const name = (location.hash || '').replace(/^#/,'')
  if(!_NAV_PAGES.includes(name)) return
  const page = document.getElementById('page-'+name)
  if(!page || page.classList.contains('active')) return   // 已在该页则不动，防循环
  const tab = document.querySelector(`.ntab[data-page="${name}"]`)
  if(tab) tab.click()
}
window.addEventListener('hashchange', _gotoHash)
function switchRTab(name,el){
  document.querySelectorAll('.rpanel').forEach(p=>p.classList.remove('active'))
  document.querySelectorAll('.rtab').forEach(t=>t.classList.remove('active'))
  document.getElementById('rp-'+name).classList.add('active')
  el.classList.add('active')
  if(editor) setTimeout(()=>editor.refresh(),80)
}

let _pollStarted = false
const _pollInflight = {status:false, positions:false, funds:false, bridge:false}
const _statusRenderSig = {pool:'', queue:'', logs:''}
function _statusPayloadSig(value){
  try{return JSON.stringify(value||[])}catch(e){return ''}
}
function _activePageName(){
  return document.querySelector('.page.active')?.id?.replace(/^page-/,'') || 'monitor'
}
function _isCorePollingPage(){
  return ['monitor','execution'].includes(_activePageName())
}
function _isMarketContextPage(){
  return ['monitor','strategy'].includes(_activePageName())
}
function _isPaperPollingPage(){
  return _activePageName()==='paper'
}
async function refreshPaperLiveView(){
  if(typeof loadPaperStats!=='function' || typeof loadPaperPositions!=='function') return
  await Promise.all([loadPaperStats(), loadPaperPositions()])
}
function pollEvery(fn, ms, when=null){
  const run = () => { if(!document.hidden && (!when || when())) fn() }
  run()
  return setInterval(run, ms)
}
function startPoll(){
  if(!_currentUser) return
  if(_pollStarted) return
  _pollStarted = true
  pollEvery(fetchSt, 10000)
  pollEvery(fetchPositions, 5000, _isCorePollingPage)
  pollEvery(fetchFunds, 10000, _isCorePollingPage)
  pollEvery(refreshPaperLiveView, 5000, _isPaperPollingPage)
  document.addEventListener('visibilitychange', ()=>{
    if(!document.hidden){
      fetchSt()
      if(_isCorePollingPage()){ fetchPositions(); fetchFunds() }
      if(_isPaperPollingPage()) refreshPaperLiveView()
      if(_isMarketContextPage()) loadMarketContext(false)
    }
  })
  refreshAutoModeBadge()   // 顶部实盘自动化状态标签
}

// 实时可用资金：模拟盘读账本现金、实盘读 xiadan。更新顶部「可用资金」药丸，
// 并作为下单手数计算(pbCalcShares)的资金基数。失败则回退 fetchSt 的配置资金。
let _availFunds = null
let _fundsMode  = null   // 记录上次资金来源；'real' 时停止自动轮询（避免抢焦点）
let _accountTotalAsset = null
let _targetPositionRatio = 0.2
async function fetchFunds(){
  if(!_currentUser) return
  if(_pollInflight.funds) return
  _pollInflight.funds = true
  try{
    const d = await api('/account/funds')
    if(!d) return
    if(d.mode) _fundsMode = d.mode
    if(!d.ok || d.available==null) return   // 实盘暂无缓存时 available 为 null，保留药丸原值
    _availFunds = +d.available
    const totalAsset = d.total_asset!=null ? +d.total_asset : (d.equity!=null ? +d.equity : null)
    if(Number.isFinite(totalAsset) && totalAsset>0){
      _accountTotalAsset = totalAsset
      _kpi.equity = totalAsset
    }
    else if(Number.isFinite(_availFunds) && d.market_value!=null) _accountTotalAsset = _availFunds + (+d.market_value||0)
    const capEl = document.getElementById('nCapital')
    if(capEl){
      capEl.textContent = '¥'+_availFunds.toLocaleString('zh-CN',{maximumFractionDigits:0})
      const tip = d.mode==='paper'
        ? `模拟盘可用现金（权益 ¥${(+(d.equity||0)).toLocaleString('zh-CN',{maximumFractionDigits:0})}）`
        : `xiadan 实盘可用资金${d.total_asset!=null?`（总资产 ¥${(+d.total_asset).toLocaleString('zh-CN',{maximumFractionDigits:0})}）`:''}`
          + `｜事件驱动：下单成功后自动更新${d.age_sec!=null?`（${d.age_sec}秒前）`:''}，也可点「📊 查实盘账户」手动刷新`
      capEl.parentElement?.setAttribute('title', tip)
    }
    _kpi.avail = _availFunds
    if(d.market_value!=null) _kpi.mktValue = +d.market_value
    _renderKpiBar()
  }catch(e){}
  finally{ _pollInflight.funds = false }
}
async function fetchSt(force=false){
  if(_pollInflight.status && !force) return
  _pollInflight.status = true
  try{
  const d=await api('/status'); if(!d) return
  const monitoring = !!(d.running || d.vibe_active)
  const rb=document.getElementById('runBadge')
  if(rb){
    rb.textContent=monitoring?'运行中':'已停止'
    rb.className='badge '+(monitoring?'run':'stop')
  }
  const scannedEl=document.getElementById('nScanned')
  const executedEl=document.getElementById('nExecuted')
  if(scannedEl) scannedEl.textContent=d.scanned||0
  if(executedEl) executedEl.textContent=d.executed||0
  // 账户信息
  const todayEl=document.getElementById('nToday')
  const limitEl=document.getElementById('nLimit')
  const capEl  =document.getElementById('nCapital')
  if(todayEl) todayEl.textContent=d.today_trades||0
  if(limitEl) limitEl.textContent=d.max_daily_trades||3
  // 可用资金优先用 fetchFunds 拉到的实时值；未就绪时回退配置资金
  if(capEl && _availFunds==null) capEl.textContent='¥'+(+(d.capital||0)).toLocaleString('zh-CN',{maximumFractionDigits:0})
  // 同步账户设置框
  const ci=document.getElementById('cfgCapital')
  const ri=document.getElementById('cfgRatio')
  const si=document.getElementById('cfgSingleCap')
  const mi=document.getElementById('cfgMaxPos')
  if(ci&&!ci.matches(':focus')) ci.value=d.capital||1000000
  if(ri&&!ri.matches(':focus')) ri.value=Math.round((d.position_ratio||0.2)*100)
  // 日限不在这里回填：它是策略级 max_daily，由 refreshExecutionState 写入（只读展示）
  if(si&&!si.matches(':focus')) si.value=Math.round((d.single_position_max_ratio||0.2)*100)
  if(mi&&!mi.matches(':focus')) mi.value=d.max_positions||5
  const maxPosForTarget = +(d.max_positions||0)
  const ratioForTarget = +(d.position_ratio||0)
  _targetPositionRatio = ratioForTarget>0 ? ratioForTarget : (maxPosForTarget>0 ? 1/maxPosForTarget : 0.2)
  // KPI 条：护栏与当日计数（日限由 refreshExecutionState 写入策略级真值）
  _kpi.maxPos = maxPosForTarget || null
  _kpi.today  = d.today_trades!=null ? +d.today_trades : null
  if(_availFunds==null && d.capital!=null) _kpi.avail = +d.capital
  _renderKpiBar()
  const ss=document.getElementById('scanSt')
  ss.innerHTML=monitoring?`<span class="spin">⟳</span> ${d.vibe_active?'Alpha监控中':'监控中'}`:'已停止'
  ss.style.color=monitoring?'var(--gl)':'var(--t3)'
  // 同步自动执行开关
  if(typeof d.auto_execute !== 'undefined' && d.auto_execute !== _autoExec){
    _autoExec = d.auto_execute
    _renderAutoSwitch(_autoExec)
  }
  const pool = d.pool||[], queue = d.queue||[], logs = d.logs||[]
  const maxPos = +(d.max_positions||5)
  const occupiedCodes = new Set(Object.keys(d.positions||{}))
  queue.forEach(q=>{ if(q?.code && String(q.action||'buy').toLowerCase()==='buy' && ['pending','sent','submitted'].includes(String(q.status||'pending'))) occupiedCodes.add(q.code) })
  if(ss && monitoring && d.auto_execute && maxPos>0 && occupiedCodes.size>=maxPos){
    ss.innerHTML = `<span class="spin">⟳</span> 运行中 · 持仓已满 ${occupiedCodes.size}/${maxPos}，停止开新仓`
    ss.style.color = '#D6456A'
  }
  const poolSig = _statusPayloadSig(pool), queueSig = _statusPayloadSig(queue), logsSig = _statusPayloadSig(logs)
  const queueChanged = queueSig!==_statusRenderSig.queue
  if(poolSig!==_statusRenderSig.pool || queueChanged){
    _lastPool = pool
    if(queueChanged){
      _queuedCodes.clear()
      queue.forEach(q=>{ if(q?.code) _queuedCodes.add(q.code) })
    }
    renderPool(_lastPool)
    renderQueue(queue)
    _statusRenderSig.pool = poolSig
    _statusRenderSig.queue = queueSig
  } else {
    _lastPool = pool
  }
  if(logsSig!==_statusRenderSig.logs){
    renderLogs(logs)
    _statusRenderSig.logs = logsSig
  }
  document.getElementById('actLbl').textContent=d.active_strategy?'⚡ 自动:'+d.active_strategy:''
  // 同步扫描进度条（显示实际百分比）
  const prog = d.scan_progress||{}
  const progWrap = document.getElementById('scanProgWrap')
  const progBar  = document.getElementById('scanProgBar')
  const progTxt  = document.getElementById('scanProgTxt')
  if(prog.running){
    progWrap.style.display='block'
    const cur = prog.current||0, tot = prog.total||0
    if(tot > 0 && cur > 0){
      const pct = Math.min(100, Math.round(cur/tot*100))
      progBar.className = 'scan-prog-bar'   // 去掉 pulse
      progBar.style.width = pct+'%'
      progTxt.textContent = `${prog.stage||'扫描中'} ${cur} / ${tot} (${pct}%)`
    } else {
      progBar.className = 'scan-prog-bar pulse'
      progBar.style.width = ''
      progTxt.textContent = prog.stage||'扫描中...'
    }
  } else {
    progWrap.style.display='none'
    progTxt.textContent=''
    progBar.className='scan-prog-bar pulse'
    progBar.style.width=''
  }
  }finally{ _pollInflight.status = false }
}
function _markMonitorRunning(label='监控中'){
  const rb=document.getElementById('runBadge')
  if(rb){ rb.textContent='运行中'; rb.className='badge run' }
  const ss=document.getElementById('scanSt')
  if(ss){ ss.innerHTML=`<span class="spin">⟳</span> ${label}`; ss.style.color='var(--gl)' }
}
async function fetchPositions(){
  if(!_currentUser) return
  if(_pollInflight.positions) return
  _pollInflight.positions = true
  try{
    const data = await api('/positions'); if(!data) return
    renderPositions(Array.isArray(data)?data:[])
  }finally{ _pollInflight.positions = false }
}
async function mStart(){ const r=await api('/start','POST'); toast((r&&(r.msg||r.error))||(r&&r.ok?'策略已启动':'策略启动失败'), r&&r.ok?'success':'error'); fetchSt() }
async function mStop() { const r=await api('/stop','POST');  toast((r&&(r.msg||r.error))||(r&&r.ok?'策略已停止':'策略停止失败'), r&&r.ok?'success':'error'); fetchSt() }
async function clearQ(){
  if(!confirm('确认清空执行队列？')) return
  await api('/queue/clear','POST')
  _queuedCodes.clear()   // 同步重置前端入队状态，让 +队 按钮恢复
  fetchSt()
}
async function execQ(code,action){
  const item=(await api('/status'))?.queue?.find(q=>q.code===code)||{}
  await api('/execute','POST',{code,action,price:item.price,shares:item.shares})
  fetchSt()
}
let _autoExec = false
async function toggleAutoExec(){
  const want = !_autoExec
  const live = (typeof _tradeMode !== 'undefined' && _tradeMode === 'live')
  const modeLine = live
    ? '🔴 当前为实盘模式：信号出现将真实下单、动用真实资金！'
    : '🛡️ 当前为模拟盘模式：只记录日志，不实际下单。'
  if(want && !confirm(`⚠️ 开启自动执行后，打板信号出现将立即向同花顺下单，无需手动确认。\n\n${modeLine}\n\n确认开启？`)){
    return
  }
  const r = await api('/auto_execute','POST',{enabled: want})
  if(!r?.ok){ toast('自动执行切换失败', 'error'); return }
  _autoExec = want
  _renderAutoSwitch(_autoExec)
  toast(want ? '已开启自动执行' : '已关闭自动执行', want ? 'warn' : 'success')
}
function _renderAutoSwitch(on){
  _kpi.auto = !!on
  _renderKpiBar()
  const sw   = document.getElementById('autoExecStatus')
  const dot  = document.getElementById('autoExecDot')
  const txt  = document.getElementById('autoExecText')
  if(!sw) return
  sw.style.background  = on ? '#f59e0b' : '#374151'
  dot.style.left       = on ? '17px' : '2px'
  if(txt) { txt.textContent = on ? '自动执行 开' : '自动执行'; txt.style.color = on ? '#f59e0b' : 'var(--t3)' }
}

// 持仓渲染
function _positionWeightBase(positions, account){
  const accountTotal = account && account.total_asset!=null ? +account.total_asset : null
  if(Number.isFinite(accountTotal) && accountTotal>0) return accountTotal
  if(Number.isFinite(_accountTotalAsset) && _accountTotalAsset>0) return _accountTotalAsset
  const marketValue = (positions||[]).reduce((s,p)=>s+(+(p.market_value||0)),0)
  const cash = Number.isFinite(_availFunds) ? _availFunds : 0
  return marketValue + cash || marketValue || 0
}
function _positionWeightHtml(marketValue, base){
  const actual = base>0 ? marketValue/base : 0
  const target = _targetPositionRatio || 0.2
  const diff = actual - target
  const color = Math.abs(diff)<=0.01 ? 'var(--t3)' : (diff>0 ? '#f59e0b' : 'var(--t4)')
  return `<div style="font-size:10px;color:${color};margin-top:1px">${(actual*100).toFixed(1)}% / 目标${(target*100).toFixed(0)}%</div>`
}
function renderPositions(positions){
  const tb   = document.getElementById('posBody')
  const totEl= document.getElementById('posTotal')
  const pnlEl= document.getElementById('posPnl')
  if(!positions.length){
    tb.innerHTML='<tr><td colspan="8"><div class="empty" style="height:40px">暂无持仓</div></td></tr>'
    if(totEl) totEl.textContent='0只'
    if(pnlEl) pnlEl.textContent=''
    _kpi.posCount = 0
    _kpi.pnl = 0
    _kpi.mktValue = 0
    _renderKpiBar()
    return
  }
  if(totEl) totEl.textContent=positions.length+'只'
  // 汇总盈亏
  const totalPnl = positions.reduce((s,p)=>s+(p.pnl_amount||0),0)
  // 同步 KPI 条（持仓数 / 浮动盈亏 / 持仓市值）
  _kpi.posCount = positions.length
  _kpi.pnl = totalPnl
  const mv = positions.reduce((s,p)=>s+(+p.market_value||0),0)
  if(mv>0) _kpi.mktValue = mv
  _renderKpiBar()
  if(pnlEl){
    const sign = totalPnl>=0?'+':''
    pnlEl.textContent = sign+'¥'+totalPnl.toLocaleString('zh-CN',{maximumFractionDigits:0})
    pnlEl.style.color = totalPnl>=0?'var(--rl)':'var(--gl)'
    pnlEl.style.fontWeight='700'
  }
  const weightBase = _positionWeightBase(positions)
  tb.innerHTML = positions.map(p=>{
    const pUp  = p.pnl_pct>=0
    const sign = pUp?'+':''
    const pColor = pUp?'var(--rl)':'var(--gl)'
    const nm = (p.name||p.code).replace(/'/g,"\\'")
    return `<tr style="cursor:pointer" data-ui-click="showKLine('${p.code}','${nm}')">
      <td style="padding:5px 10px;font-weight:700;font-size:13px">${p.code}</td>
      <td style="padding:5px 10px;color:var(--t2);max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.name||p.code}</td>
      <td style="padding:5px 10px;text-align:right;color:var(--t3);font-size:11px">${p.shares}股</td>
      <td style="padding:5px 10px;text-align:right;color:var(--t4);font-size:11px">${(+p.cost).toFixed(2)}</td>
      <td style="padding:5px 10px;text-align:right;font-weight:700">${(+p.cur_price).toFixed(2)}</td>
      <td style="padding:5px 10px;text-align:right;font-weight:700;color:${pColor}">${sign}${p.pnl_pct.toFixed(2)}%</td>
      <td style="padding:5px 10px;text-align:right;color:var(--t3);font-size:11px">¥${(+(p.market_value||0)).toLocaleString('zh-CN',{maximumFractionDigits:0})}${_positionWeightHtml(+(p.market_value||0), weightBase)}</td>
      <td style="padding:5px 8px;text-align:right" data-ui-click="event.stopPropagation()">
        <button class="btn btn-red btn-xs" data-ui-click="sellPos('${p.code}',${p.cur_price})">卖出</button>
      </td>
    </tr>`
  }).join('')
}

async function sellPos(code, price){
  const live = (typeof _tradeMode !== 'undefined' && _tradeMode === 'live')
  const warn = live ? '🔴 实盘模式：将真实卖出、真实成交！' : '🛡️ 模拟盘模式：仅记录，不动真钱。'
  if(!confirm(`确认卖出 ${code} @ ${price}？\n${warn}`)) return
  const r = await api('/positions/sell','POST',{code, price})
  if(r?.ok){ toast(`已提交卖出 ${code}`, 'success'); fetchSt(); fetchPositions(); fetchFunds() }
  else toast('卖出失败: '+(r?.msg||''), 'error')
}

async function saveAccount(){
  const cap=parseFloat(document.getElementById('cfgCapital').value)||1000000
  const rat=parseFloat(document.getElementById('cfgRatio').value)||20
  const scap=parseFloat(document.getElementById('cfgSingleCap').value)||20
  const mpos=parseInt(document.getElementById('cfgMaxPos').value)||5
  // 前端兜底校验：单笔% 1~100，持仓 1~50
  if(scap<1||scap>100){ toast('单笔上限须在 1~100% 之间','error'); return }
  if(mpos<1||mpos>50){ toast('最多持仓数须在 1~50 之间','error'); return }
  // 只发后端真正接收的 4 个字段（web/trade.py set_account）。
  // 原来还发 max_daily_trades，但后端从不读取（config.py:83 已移除该配置），
  // 徒增"日限已保存"的错觉；真实日限是策略级 position_config.max_daily。
  const r=await api('/account','POST',{
    capital:cap, position_ratio:rat/100,
    single_position_max_ratio:scap/100, max_positions:mpos })
  if(r&&r.ok===false){ toast('保存失败: '+(r.error||''),'error'); return }
  toast('账户设置已保存','success')
  if(scap<rat) toast(`⚠️ 单笔上限 ${scap}% 低于仓位 ${rat}%，建仓时将按 ${scap}% 执行（护栏生效）`,'warn')
  fetchSt()
}

const _queuedCodes = new Set()

function renderPool(pool){
  const tb=document.getElementById('poolBody')
  if(!pool?.length){
    tb.innerHTML='<tr><td colspan="6"><div class="empty">监控运行中，等待选股信号...</div></td></tr>'
    return
  }
  const signals = pool.filter(r=>r.signal!==false)
  const sealed  = pool.filter(r=>r.signal===false)
  tb.innerHTML=[...signals,...sealed].map(r=>{
    const chg      = parseFloat(r.change||0)
    const isSignal = r.signal !== false
    const isQueued = _queuedCodes.has(r.code)
    const rowStyle = isSignal ? 'cursor:pointer' : 'opacity:.65;background:rgba(255,255,255,.02);cursor:pointer'
    // 盘口
    let boardHtml=''
    if(r.asks&&r.bids){
      const a1=r.asks[0]||{},b1=r.bids[0]||{}
      boardHtml=`<div style="font-size:10px;color:var(--t4);margin-top:2px">
        卖一 <span style="color:#f87171">${(+a1.price||0).toFixed(2)}</span>(${a1.vol||0}手)
        买一 <span style="color:#4ade80">${(+b1.price||0).toFixed(2)}</span>(${b1.vol||0}手)
      </div>`
    }
    // 连板标签
    const consec = r.consec_limit || 0
    const consecLbl = r.consec_label || ''
    const consecColor = consec >= 3 ? '#f97316' : consec >= 2 ? '#eab308' : '#6B95D6'
    const consecBadge = consecLbl
      ? `<span style="font-size:9px;background:${consecColor};color:#000;border-radius:3px;padding:1px 5px;margin-left:3px;font-weight:800">${consecLbl}</span>`
      : ''
    // 信号标签
    const tagHtml = isSignal
      ? `<span style="font-size:9px;background:#F2EAFB;color:#8B53C9;border-radius:3px;padding:1px 5px;margin-left:4px;font-weight:700">打板</span>${consecBadge}`
      : `<span style="font-size:9px;background:#2a2a2a;color:#666;border-radius:3px;padding:1px 5px;margin-left:4px">封板</span>${consecBadge}`
    // 操作按钮（"+队"加入待执行，"🛒"立即模拟买入）
    const buyPayload={code:r.code,name:r.name,price:r.price}
    const queuePayload={...buyPayload,reason:r.reason||''}
    let actionHtml
    if(!isSignal){
      actionHtml='<span style="font-size:11px;color:var(--t4)">参考</span>'
    } else if(isQueued){
      actionHtml='<button class="btn btn-queued btn-xs" disabled>✓ 已入队</button>'
      + ` <button class="btn btn-orange btn-xs" ${_coreActionAttrs('manual-buy',buyPayload)} title="立即按当前价模拟盘买入（按仓位%计算手数）">🛒 买入</button>`
    } else {
      actionHtml=`<button class="btn btn-purple btn-xs" ${_coreActionAttrs('add-one-q',queuePayload)}>+队</button>`
      + ` <button class="btn btn-orange btn-xs" ${_coreActionAttrs('manual-buy',buyPayload)} title="跳过队列直接按当前价模拟盘买入（按仓位%计算手数）">🛒 买入</button>`
    }
    const isTdx = r.reason?.includes('TDX')||r.reason?.includes('封板')
    return `<tr style="${rowStyle}" ${_coreActionAttrs('show-kline',{code:r.code,name:r.name||r.code})}>
      <td style="font-weight:700;white-space:nowrap">${r.code}${tagHtml}</td>
      <td style="color:var(--t2)">${r.name||'—'}</td>
      <td><b>${(+r.price||0).toFixed(2)}</b>${isTdx?'<span style="font-size:9px;color:#60a5fa;margin-left:3px">L2</span>':''}</td>
      <td class="${chg>=0?'rise':'fall'}" style="font-weight:700">${(chg>=0?'+':'')+chg.toFixed(2)}%</td>
      <td style="font-size:10px;color:var(--t3)">${boardHtml}<span>${(r.reason||'').slice(0,50)}</span></td>
      <td data-core-stop>${actionHtml}</td>
    </tr>`
  }).join('')
}
function renderQueue(q){
  document.getElementById('qLen').textContent=(q?.length||0)+'只'
  const el=document.getElementById('qBody')
  if(!q?.length){el.innerHTML='<div class="empty">无待处理</div>';return}
  const sm_={
    pending:  '<span class="sbadge s-pending">待确认</span>',
    done:     '<span class="sbadge s-done">已买(模拟)</span>',
    executed: '<span class="sbadge s-done" style="background:#22c55e">✅ 已成交(实盘)</span>',
    submitted:'<span class="sbadge s-pending" style="background:#f59e0b">⏳ 已报实盘·等成交</span>',
    sent:     '<span class="sbadge s-pending" style="background:#3b82f6">📤 桥接器处理中</span>',
    skip:     '<span class="sbadge s-skip">跳过</span>',
    error:    '<span class="sbadge s-error">失败</span>',
    failed:   '<span class="sbadge s-error">❌ 实盘失败</span>',
  }
  el.innerHTML=q.map(it=>{
    const nm=(it.name||it.code).replace(/'/g,"\\'")
    const failMsg = it.fail_msg ? `<div style="font-size:10px;color:#ef4444;padding-left:2px">⚠️ ${(it.fail_msg||'').substring(0,80)}</div>` : ''
    return `<div class="q-item" style="flex-direction:column;gap:4px;padding:8px 10px;cursor:pointer" data-ui-click="showKLine('${it.code}','${nm}')">
    <div style="display:flex;align-items:center;gap:6px">
      <span style="font-weight:700;width:60px">${it.code}</span>
      <span style="color:var(--t2);flex:1">${it.name}</span>
      <span style="color:#f59e0b;font-size:12px">¥${(+it.price||0).toFixed(2)}</span>
      <span data-ui-click="event.stopPropagation()">${it.status==='pending'
        ?`<button class="btn btn-green btn-sm" data-ui-click="execQ('${it.code}','buy')">买入</button><button class="btn btn-gray btn-sm" data-ui-click="execQ('${it.code}','skip')">跳过</button>`
        :sm_[it.status]||`<span class="sbadge s-pending">${it.status||''}</span>`}</span>
    </div>
    ${it.shares?`<div style="font-size:11px;color:var(--t3);padding-left:2px">
      计划买入 <b style="color:var(--t2)">${it.shares}</b> 股
      · 预算 <b style="color:var(--gl)">¥${(+(it.budget||0)).toLocaleString('zh-CN',{maximumFractionDigits:0})}</b>
      ${it.reason?'· <span style="color:var(--t4)">'+it.reason.substring(0,40)+'</span>':''}
    </div>`:''}
    ${failMsg}
  </div>`
  }).join('')
}
function renderLogs(logs){
  const el=document.getElementById('logBody'); if(!logs?.length) return
  el.innerHTML=logs.map(l=>`<div class="log-row log-${l.level||'info'}"><span class="lt">${l.time}</span><span class="lm">${l.msg}</span></div>`).join('')
}

// ── 侧边栏 Tab 切换 ──
let _libTab = 'strat'
function switchLibTab(tab){
  _libTab = tab
  document.getElementById('sltab-strat').classList.toggle('active', tab==='strat')
  document.getElementById('sltab-factor').classList.toggle('active', tab==='factor')
  document.getElementById('sl-hd-strat').classList.toggle('hidden', tab!=='strat')
  document.getElementById('sl-hd-factor').classList.toggle('hidden', tab!=='factor')
  document.getElementById('stratList').classList.toggle('hidden', tab!=='strat')
  document.getElementById('factorList').classList.toggle('hidden', tab!=='factor')
  // 同步中间编辑区工具栏：按 data-mode 显示/隐藏按钮
  document.querySelectorAll('.se-hd .se-acts [data-mode]').forEach(btn => {
    const m = btn.dataset.mode
    btn.style.display = (m === 'both' || m === tab) ? '' : 'none'
  })
  if(tab==='factor') loadFactorList()
}
// 页面初次加载时也跑一次切换（按 _libTab 默认 strat 渲染）
document.addEventListener('DOMContentLoaded', () => {
  const activePage = document.querySelector('.page.active')
  const activeName = activePage ? activePage.id.replace(/^page-/,'') : 'monitor'
  syncMobileNavigation(activeName)
  syncMobileHeader(activeName)
  setMobileStrategyPane(_mobileStrategyPane)
  setMobileLayoutMode()
  syncMobileViewport()
  syncMobileModeState()
  renderRunState(false, _mobileRunLabel)
  _mobileLayoutQuery.addEventListener('change', setMobileLayoutMode)
  window.addEventListener('resize', syncMobileViewport, {passive:true})
  if(window.visualViewport){
    window.visualViewport.addEventListener('resize', syncMobileViewport, {passive:true})
    window.visualViewport.addEventListener('scroll', syncMobileViewport, {passive:true})
  }
  document.addEventListener('focusin', event => {
    if(!document.body.classList.contains('mobile-layout')) return
    if(!event.target.matches('input,select,textarea,[contenteditable="true"]')) return
    setTimeout(()=>event.target.scrollIntoView({block:'center',inline:'nearest',behavior:'smooth'}), 120)
  })
  document.addEventListener('keydown', event => {
    if(event.key === 'Escape'){ toggleMobileMore(false); toggleMobileRunSheet(false) }
  })
  try { switchLibTab(_libTab) } catch(e) { console.warn('libtab init', e) }
  // 把所有 Modal 提升到 body 直接子元素，避免被父 .page.hidden 隐藏
  // 修复 bug：实盘切换 Modal 等只在策略中心可见、切到其他页消失
  // 注：page-execution 不是 modal，但同样写在 page-strategy 内部，
  //     必须提升到 body 才能作为同级 .page 被 switchPage 正常切换。
  try {
    ['renameModal','vibeActivateModal','liveModeModal','vibeModal',
     'aiGenModal','aiFactorGenModal','klOverlay','vibeBacktestModal',
     'page-execution','page-settings','page-risk','changelogModal'
    ].forEach(id => {
      const el = document.getElementById(id)
      if(el && el.parentNode !== document.body){
        document.body.appendChild(el)
      }
    })
  } catch(e) { console.warn('modal hoist', e) }
})

// ── 策略库 ──
async function loadStratList(){
  const list=await api('/strategies')||[]
  const el=document.getElementById('stratList')
  if(!list.length){el.innerHTML='<div class="empty">暂无策略</div>';return}
  el.innerHTML=list.map(s=>`
    <div class="si ${curStrat===s.file?'active':''}" data-ui-click="loadStrat('${s.file}')">
      <div class="si-name">
        ${esc(s.name)}
        ${s.builtin?'<span class="si-bi">内置</span>':''}
        ${!s.builtin?`<span class="si-rename" data-ui-click="event.stopPropagation();openRename('${s.file}')">✏ 改名</span>`:''}
      </div>
      <div class="si-desc">${esc(s.desc||'暂无描述')}</div>
    </div>`).join('')
}

// ── 因子库 ──
let curFactor = null
let _curMode  = 'strat'   // 'strat' | 'factor' — 决定保存/删除路由

async function loadFactorList(){
  const list = await api('/factors') || []
  const el   = document.getElementById('factorList')
  if(!list.length){el.innerHTML='<div class="empty">暂无因子</div>';return}
  el.innerHTML = list.map(f=>`
    <div class="fi-item ${curFactor===f.file?'active':''}" data-ui-click="loadFactor('${f.file}')">
      <div class="fi-name" style="display:flex;align-items:center;gap:4px">
        <span>${esc(f.name)}</span>
        <span class="fi-cat" style="background:${f.color}">${esc(f.category)}</span>
        ${!f.builtin?`<button class="btn btn-red btn-xs" style="margin-left:auto;padding:1px 6px;font-size:10px" data-ui-click="event.stopPropagation();delFactor('${f.file}')">🗑</button>`:''}
      </div>
      <div class="fi-desc">${esc(f.desc||'')}</div>
      <div class="fi-period">参考周期 ${f.period||'-'} 日 · ${f.builtin?'AlphaLens 内置':'用户自建'}</div>
    </div>`).join('')
}

async function loadFactor(fn){
  curFactor = fn
  _curMode  = 'factor'
  const d = await api('/factors/'+fn)
  if(!d) return
  editor.setValue(d.code||'')
  editor.setOption('readOnly', false)
  document.getElementById('sfn').value = fn
  setEdSt(`因子已载入: ${fn} — 点「🔍 扫描」开始因子选股`)
  loadFactorList()
  // ★ 不再自动选股：点因子只载入代码，由用户主动点「🔍 扫描」按钮才跑因子选股。
  //   原先点一下因子就触发全市场因子计算（几秒~几分钟），占满单 worker，
  //   导致这期间点策略/切换全卡、其他用户请求排队点不动。改成手动触发。
}

async function newFactor(){
  curFactor = null
  _curMode  = 'factor'
  const d = await api('/factors/template')
  editor.setValue(d?.code || '')
  editor.setOption('readOnly', false)
  document.getElementById('sfn').value = 'my_factor'
  setEdSt('新因子（未保存）')
  loadFactorList()
}

async function delFactor(fn){
  if(!confirm(`确认删除因子 "${fn}"？`)) return
  const r = await api('/factors/delete','POST',{filename:fn})
  if(r?.ok){
    if(curFactor===fn){ curFactor=null; editor.setValue(''); document.getElementById('sfn').value='' }
    loadFactorList()
  } else {
    alert(r?.msg||'删除失败（内置因子不可删除）')
  }
}

let _factorScanRunning = false

async function _runFactorScreen(fn){
  // 防止重复并发扫描
  if(_factorScanRunning){
    const scInfo = document.getElementById('scInfo')
    scInfo.textContent = '因子扫描正在进行中，请稍候...'
    return
  }

  const scInfo = document.getElementById('scInfo')
  const scProg = document.getElementById('scProgWrap')
  const scBody = document.getElementById('scBody')

  // 如果策略已有选股结果，提供选项：全市场 or 在此基础上筛选
  let scanCodes = []
  if(scResults && scResults.length > 0){
    const usePool = confirm(
      `当前已有 ${scResults.length} 只股票结果。\n\n` +
      `点击【确定】：在此股票池中运行因子筛选（快速）\n` +
      `点击【取消】：对全市场重新扫描（约30-60秒）`
    )
    if(usePool) scanCodes = scResults.map(r=>r.code)
  }

  _factorScanRunning = true
  const modeLabel = scanCodes.length ? `${scanCodes.length}只股票池` : '全市场(~5000只)'
  const timeHint  = scanCodes.length
    ? '约3-5秒'
    : '全市场覆盖 | 首次约60-90秒，行情缓存5分钟后续仅需约15秒'
  scInfo.textContent = `⟳ 因子选股中: ${fn} [${modeLabel}]...`
  scProg.style.display='block'
  document.getElementById('scProgBar').className='scan-prog-bar pulse'
  scBody.innerHTML=`<div class="empty" style="flex-direction:column;gap:8px">
    <span style="font-size:20px">⟳</span>
    <span>因子扫描 ${modeLabel} 中...</span>
    <span style="font-size:11px;color:var(--t4)">${timeHint}</span>
    <span style="font-size:11px;color:var(--t4)">多连接并行K线拉取，全覆盖不降精度</span>
  </div>`

  try{
    const r = await api('/factors/run','POST',{filename:fn, codes:scanCodes, max_codes:0})
    scProg.style.display='none'
    document.getElementById('addAllBtn').style.display='none'

    if(!r?.ok){
      scInfo.textContent='因子选股失败'
      scBody.innerHTML=`<div class="empty" style="color:var(--rl)">❌ ${esc(r?.msg||'未知错误')}<br><small style="color:var(--t4);font-size:11px">请检查TDX连接状态</small></div>`
      return
    }
    scResults = r.results||[]
    const hitCnt = scResults.length
    const scanCnt = r.scanned||0
    scInfo.textContent=`因子 [${fn}] 命中 ${hitCnt} 只（扫描 ${scanCnt} 只）`
    const _ob1=document.getElementById('optBtn'); if(_ob1) _ob1.style.display=hitCnt>=2?'':'none'
    if(hitCnt){
      document.getElementById('addAllBtn').style.display=''
      _renderScResults()
    } else {
      scBody.innerHTML=`<div class="empty" style="flex-direction:column;gap:6px">
        <span style="font-size:16px">🔍</span>
        <span>本次扫描 ${scanCnt} 只，未找到符合条件的股票</span>
        <span style="font-size:11px;color:var(--t4)">当前市场环境可能不满足因子条件，可尝试其他因子或等待更好时机</span>
      </div>`
    }
  }finally{
    _factorScanRunning = false
    scProg.style.display='none'
  }
}

// ── 重命名 Modal ──
let _renameOld = ''
function openRename(fn){
  _renameOld = fn
  document.getElementById('renameInput').value = fn
  document.getElementById('renameModal').classList.add('open')
  setTimeout(()=>document.getElementById('renameInput').select(), 80)
}
function closeRename(ev){
  if(ev && ev.target!==document.getElementById('renameModal')) return
  document.getElementById('renameModal').classList.remove('open')
}
async function doRename(){
  const newFn = document.getElementById('renameInput').value.trim()
  if(!newFn){alert('请输入新名称');return}
  const r = await api('/strategies/rename','POST',{old:_renameOld, new:newFn})
  if(r?.ok){
    if(curStrat===_renameOld) curStrat=r.file
    document.getElementById('sfn').value = r.file
    document.getElementById('renameModal').classList.remove('open')
    loadStratList()
  } else {
    alert(r?.msg||'重命名失败')
  }
}

// ══════════════════════════════════════════════════
//  实盘/模拟总开关
// ══════════════════════════════════════════════════
let _tradeMode = 'paper'  // 'paper' | 'live'

async function loadTradeMode(){
  if(!_currentUser) return
  const r = await api('/user/broker_config')
  if(r?.ok){
    _tradeMode = (r.broker_type === 'live' || r.broker_type === 'real') ? 'live' : 'paper'
    _renderTradeModeBadge()
  }
}

function _renderTradeModeBadge(){
  const el = document.getElementById('tradeModeBadge')
  _renderCurrentModeBadge('desktopCurrentModeBadge', _tradeMode, '电脑')
  _renderNavModeBadge()
  if(!el) return
  el.removeAttribute('style')
  if(_tradeMode === 'live'){
    el.className = 'top-switch trade-switch on'
    el.innerHTML = '<i></i><b>实盘</b>'
  } else {
    el.className = 'top-switch trade-switch off'
    el.innerHTML = '<i></i><b>模拟</b>'
  }
}

function toggleTradeMode(){
  if(_tradeMode === 'live'){
    // 实盘 → 模拟 不需要确认（变安全方向）
    _switchTradeMode('paper')
  } else {
    // 模拟 → 实盘 必须二次确认 + 必填 live_cash
    document.getElementById('liveModeModal').classList.add('open')
    // 回填已有 live_cash（用户切回再切去时省事）+ 回填当前自动化设置
    openLiveModeModalEnsureCash()
    loadTradeSettings('opt')
  }
}

function closeLiveModeModal(ev){
  if(ev && ev.target !== document.getElementById('liveModeModal')) return
  document.getElementById('liveModeModal').classList.remove('open')
}

// ── 实盘自动化设置（下单确认 / 未成交自动撤单），运行时切换、后端持久化 ──
// 两处共用：实盘切换弹窗(prefix='opt') 与 查实盘账户面板(prefix='ts')
async function _fetchTradeSettings(){
  if(!_currentUser) return null
  try{ const d = await api('/account/trade_settings'); return (d && d.ok) ? d : null }catch(e){ return null }
}
function _fillTradeRadios(prefix, s){
  if(!s) return
  document.querySelectorAll(`input[name="${prefix}Confirm"]`).forEach(r=>{ r.checked = (r.value === (s.auto_confirm_order?'auto':'manual')) })
  document.querySelectorAll(`input[name="${prefix}Cancel"]`).forEach(r=>{ r.checked = (r.value === (s.auto_cancel_unfilled?'auto':'manual')) })
  const dEl = document.getElementById(prefix+'CancelDelay'); if(dEl) dEl.value = Math.round(s.auto_cancel_delay||15)
}
function _readTradeRadios(prefix){
  const conf = (document.querySelector(`input[name="${prefix}Confirm"]:checked`)||{}).value
  const canc = (document.querySelector(`input[name="${prefix}Cancel"]:checked`)||{}).value
  const dEl = document.getElementById(prefix+'CancelDelay')
  return { auto_confirm_order: conf==='auto', auto_cancel_unfilled: canc==='auto',
           auto_cancel_delay: dEl ? (parseFloat(dEl.value)||15) : 15 }
}
async function loadTradeSettings(prefix){
  const s = await _fetchTradeSettings()
  _fillTradeRadios(prefix, s)
  _renderAutoModeBadge(s)
  return s
}
// 顶部「实盘自动化状态」小标签：一眼看现在下单/撤单是自动还是手动
function _renderAutoModeBadge(s){
  const el = document.getElementById('autoModeBadge'); if(!el) return
  if(!s){ el.style.display = 'none'; return }   // 操作员未就绪(paper-only) → 不显示
  const conf = s.auto_confirm_order ? '下单自动' : '下单手动'
  const canc = s.auto_cancel_unfilled ? `撤单自动${Math.round(s.auto_cancel_delay||15)}s` : '撤单手动'
  const full = s.auto_confirm_order && s.auto_cancel_unfilled
  el.textContent = `⚙️ ${conf}·${canc}`
  el.title = `实盘自动化（点击调整）：下单${s.auto_confirm_order?'自动确认':'手动确认'} / `
           + (s.auto_cancel_unfilled ? `未成交${Math.round(s.auto_cancel_delay||15)}秒自动撤` : '手动撤单')
  const c = full ? '#ef4444' : '#0ea5e9'   // 全自动=红(激进)，含手动=蓝
  el.style.color = c; el.style.borderColor = c
  el.style.background = full ? 'rgba(239,68,68,.12)' : 'rgba(14,165,233,.12)'
  el.style.display = ''
}
async function refreshAutoModeBadge(){ _renderAutoModeBadge(await _fetchTradeSettings()) }
async function saveTradeSettingsPanel(){
  const hint = document.getElementById('tsSaveHint')
  if(hint){ hint.textContent='保存中...'; hint.style.color='var(--t3)' }
  const payload = _readTradeRadios('ts')
  const r = await api('/account/trade_settings','POST', payload)
  if(r && r.ok){
    await api('/mobile_ths/confirm_mode','POST', {mode: payload.auto_confirm_order ? 'auto' : 'manual'})
    _renderAutoModeBadge(r)   // 顶部状态标签同步更新
    if(hint){ hint.textContent = `✅ 已保存：下单${r.auto_confirm_order?'自动确认':'手动确认'} · ${r.auto_cancel_unfilled?`自动撤单(${Math.round(r.auto_cancel_delay)}秒)`:'手动撤单'}`; hint.style.color='#22c55e' }
    refreshMobileProfileBadge()
  }else if(hint){
    hint.textContent = '⚠️ '+((r&&r.msg)||'保存失败（实盘操作员未就绪？仅实盘可设）'); hint.style.color='#ef4444'
  }
}

// ══════════════════════════════════════════════════
//  查 xiadan 实盘账户（F4 截图：资金 + 持仓 + 当日委托）
// ══════════════════════════════════════════════════
// ══════════════════════════════════════════════════
//  风控中心：统一查看四个作用域「当前生效」的护栏
//  设计红线：四者对应不同后端 API / 不同作用域，绝不合并存储。
//  本页只做：全局护栏就地编辑（复用 /account）+ 其余三处只读展示与跳转。
// ══════════════════════════════════════════════════
function _rkSet(id, text, cls){
  const el = document.getElementById(id)
  if(!el) return
  el.textContent = text
  if(cls !== undefined) el.className = cls || ''
}

async function initRiskCenter(){
  // ① 全局护栏 + 运行时状态：直接读 /status（权威来源），不依赖监控页 DOM 是否已回填
  const st = await api('/status').catch(()=>null)
  const setVal = (id, v) => {
    const el = document.getElementById(id)
    if(el && !el.matches(':focus') && v != null) el.value = v
  }
  let maxPos = 0, dailyLimit = 3
  if(st){
    maxPos = +(st.max_positions || 0)
    setVal('rkCapital', st.capital)
    setVal('rkRatio',  Math.round((st.position_ratio ?? 0.2) * 100))
    setVal('rkSingle', Math.round((st.single_position_max_ratio ?? 0.2) * 100))
    setVal('rkMaxPos', st.max_positions)

    const today = st.today_trades ?? 0
    _rkSet('rkToday', `${today} / ${dailyLimit}`, today >= dailyLimit ? 'rk-warn' : '')
    _rkSet('rkStrat', st.running ? '运行中' : '已停止', st.running ? 'rk-on' : '')
    _rkSet('rkAuto', st.auto_execute ? '开启' : '关闭', st.auto_execute ? 'rk-warn' : '')
    // positions 是 {code: {...}} 字典，不是数组
    const posObj = st.positions || {}
    const posCount = Array.isArray(posObj) ? posObj.length : Object.keys(posObj).length
    _rkSet('rkPos', maxPos ? `${posCount} / ${maxPos}` : String(posCount),
           (maxPos && posCount >= maxPos) ? 'rk-warn' : '')
  }
  // 策略级护栏（真正被 scan_service 强制执行的那一份，只读）
  // 关键：每日开仓上限来自策略 position_config.max_daily，**不是**账户面板那个输入框——
  // 后端 POST /api/account 根本不读 max_daily_trades（config.py:83 已移除该配置），
  // 那个输入框是幽灵控件，填了不生效。这里显示真正生效的值。
  const ex = await api('/execution/state').catch(()=>null)
  const pp = ex && ex.ok ? (ex.position_policy || {}) : null
  const stratName = ex && ex.ok ? (ex.strategy?.active_strategy || '未选择') : '—'
  const skv = document.getElementById('rkStratKv')
  if(skv){
    if(pp){
      const row = (k,v)=>`<div><span>${k}</span><b>${v ?? '—'}</b></div>`
      const money = v => v == null ? null : '¥' + (+v).toLocaleString('zh-CN',{maximumFractionDigits:0})
      dailyLimit = +(pp.max_daily || 0) || dailyLimit
      skv.innerHTML = row('当前策略', stratName)
        + row('每日开仓上限', pp.max_daily ? `${pp.max_daily} 笔` : '不限（沿用账户设置）')
        + row('单笔目标金额', money(pp.target_amount))
        + row('单笔金额上限', money(pp.max_order_amount))
        + row('仓位模式', pp.mode || '—')
      const todayEl = document.getElementById('rkToday')
      if(todayEl && st){
        const t = st.today_trades ?? 0
        _rkSet('rkToday', `${t} / ${dailyLimit}`, t >= dailyLimit ? 'rk-warn' : '')
      }
    } else {
      skv.innerHTML = '<span class="risk-loading">读取失败</span>'
    }
  }

  const isLive = (typeof _tradeMode !== 'undefined' && _tradeMode === 'live')
  _rkSet('rkMode', isLive ? '🔴 实盘' : '🛡️ 模拟盘', isLive ? 'rk-danger' : 'rk-on')
  _rkSet('rkGate', _liveGateOn === true ? '已开启' : (_liveGateOn === false ? '已关闭' : '—'),
         _liveGateOn === true ? 'rk-danger' : '')
  const note = document.getElementById('rkNote')
  if(note){
    if(!isLive && _liveGateOn === true){
      note.className = 'risk-note warn'
      note.textContent = '⚠️ 当前是模拟盘，但实盘下单总闸已开启——一旦把模式切到实盘，信号会立即真实成交。若近期不打算跑实盘，建议到「交易执行」把总闸关掉。'
    } else if(isLive && _liveGateOn === true){
      note.className = 'risk-note danger'
      note.textContent = '🔴 实盘模式 + 总闸开启：策略信号会真实下单、动用真实资金。'
    } else {
      note.className = 'risk-note ok'
      note.textContent = '✅ 当前不会产生真实成交。'
    }
  }

  // ② AI 交易风控（独立作用域）
  const aitBox = document.getElementById('rkAitKv')
  if(aitBox){
    const r = await api('/ai_trader/settings').catch(()=>null)
    const rk = r && r.ok && r.settings ? (r.settings.risk || {}) : null
    if(rk){
      const row = (k,v)=>`<div><span>${k}</span><b>${v ?? '—'}</b></div>`
      const pct = v => (v == null ? null : Math.round(v * 100) + '%')
      aitBox.innerHTML = row('最大持仓数', rk.max_positions)
        + row('单笔占比', pct(rk.per_trade_ratio))
        + row('单票上限', pct(rk.single_max_ratio))
        + row('当日交易上限', rk.daily_trade_limit)
        + row('当日亏损熔断', pct(rk.daily_max_loss_pct))
        + row('共识票数门槛', rk.consensus_min_votes)
    } else {
      aitBox.innerHTML = '<span class="risk-loading">未配置或读取失败</span>'
    }
  }

  // ④ 实盘会话
  const liveBox = document.getElementById('rkLiveKv')
  if(liveBox){
    const ts = await api('/account/trade_settings').catch(()=>null)
    const row = (k,v)=>`<div><span>${k}</span><b>${v ?? '—'}</b></div>`
    liveBox.innerHTML = row('实盘下单总闸', _liveGateOn === true ? '开' : (_liveGateOn === false ? '关' : '—'))
      + row('当前交易模式', isLive ? '实盘' : '模拟盘')
      + row('下单确认', ts ? (ts.auto_confirm_order ? '全自动确认' : '人工确认') : '—')
      + row('未成交自动撤单', ts ? (ts.auto_cancel_unfilled ? `开（${ts.auto_cancel_delay ?? '—'} 分钟）` : '关') : '—')
  }
}

async function riskSaveGlobal(){
  const hint = document.getElementById('rkSaveHint')
  const cap = parseFloat(document.getElementById('rkCapital').value) || 1000000
  const rat = parseFloat(document.getElementById('rkRatio').value) || 20
  const scap= parseFloat(document.getElementById('rkSingle').value) || 20
  const mpos= parseInt(document.getElementById('rkMaxPos').value) || 5
  // 只提交后端真正接收的 4 个字段（web/trade.py:150 set_account）。
  // 不再发 max_daily_trades —— 后端不读，发了会造成"已保存日限"的错觉。
  const r = await api('/account','POST',{
    capital: cap, position_ratio: rat/100,
    single_position_max_ratio: scap/100, max_positions: mpos
  })
  if(r && r.ok !== false){
    toast('全局护栏已保存', 'success')
    if(hint){ hint.textContent = '已保存'; hint.style.color = 'var(--gl)'; setTimeout(()=>hint.textContent='', 2500) }
    fetchSt(); setTimeout(initRiskCenter, 600)
  } else {
    toast('保存失败：' + ((r && (r.msg || r.error)) || '未知错误'), 'error')
    if(hint){ hint.textContent = '保存失败'; hint.style.color = 'var(--rl)' }
  }
}

// 「交易执行」已从弹窗升级为一级页面（#page-execution）。
// openTodayOrders/closeTodayOrders 保留为兼容垫片：仍被顶栏 chip、模式徽章、
// 手机链路徽章等多处 data-action 调用，直接删会让这些入口静默失效。
function openTodayOrders(){
  switchPage('execution')
  // 进页不自动截图（实盘 F4 会抢焦点 2-3 秒，须用户主动点刷新）。
  // 清掉上次的「当日委托」残留，避免误读旧数据。
  const ob = document.getElementById('acctOrdersBody')
  if(ob) ob.innerHTML = ''
  loadTradeMode()
  if(typeof refreshLiveTradeBadge === 'function') refreshLiveTradeBadge()
  loadTradeSettings('ts')   // 回填当前实盘自动化设置（下单确认/自动撤单）
  refreshBridgeProcessBadge()
  refreshMobileProfileBadge()
  refreshExecutionState()
}
// 页面化后没有"关闭"语义了；保留空实现兜住可能残留的调用点。
function closeTodayOrders(){ /* 交易执行已是页面，无需关闭 */ }

function openExecutionConsole(){
  openTodayOrders()
}

function _setExecText(id, text, ok=null){
  const el = document.getElementById(id)
  if(!el) return
  const color = ok === true ? '#368B63' : (ok === false ? '#D6456A' : 'var(--t2)')
  el.style.color = color
  if(el.classList && el.classList.contains('exec-human-status')){
    el.innerHTML = '<i></i><span>'+esc(text)+'</span>'
    return
  }
  el.textContent = text
  const lamp = el.closest && el.closest('.exec-lamp')
  if(lamp){
    lamp.classList.remove('on','off','idle')
    lamp.classList.add(ok === true ? 'on' : (ok === false ? 'off' : 'idle'))
  }
}

function openExecutionPage(){
  openExecutionConsole()
}

function _execMoney(v){
  return (v==null || isNaN(+v)) ? '—' : '¥'+(+v).toLocaleString('zh-CN',{maximumFractionDigits:2})
}

function _execPositionsCount(d){
  if(Array.isArray(d?.positions)) return d.positions.length
  if(d?.positions_count!=null) return +d.positions_count
  return null
}

function _renderExecFundsPanel(id, data, opts={}){
  const el = document.getElementById(id)
  if(!el) return
  const title = opts.title || (id === 'execMobileFundsPanel' ? '手机资金 / 仓位' : '电脑资金 / 仓位')
  const action = id === 'execMobileFundsPanel'
    ? '<button class="btn btn-gray btn-xs" data-action="refresh-mobile-funds" id="mobileFundsRefresh">查资金/仓位</button>'
    : '<button class="btn btn-gray btn-xs" data-action="refresh-account-snapshot" id="acctSnapRefresh" title="读取电脑账户资金和持仓">查资金/持仓</button>'
  const titleHtml = '<div class="exec-funds-title"><span>'+esc(title)+'</span>'+action+'</div>'
  if(opts.loading){
    el.innerHTML = titleHtml + '<div class="exec-funds-empty">'+esc(opts.loading)+'</div>'
    return
  }
  if(opts.error){
    el.innerHTML = titleHtml + '<div class="exec-funds-error">'+esc(opts.error)+'</div>'
    return
  }
  if(!data){ return }
  const totalAsset = data.total_asset!=null ? +data.total_asset : (data.equity!=null ? +data.equity : null)
  const targetAmount = Number.isFinite(totalAsset) && totalAsset > 0 ? totalAsset * (_targetPositionRatio || 0.2) : null
  const posCount = _execPositionsCount(data)
  const kpis = [
    ['可用资金', _execMoney(data.available), 'primary'],
    ['总资产', _execMoney(totalAsset), ''],
    ['目标单仓', _execMoney(targetAmount), 'warn'],
    ['持仓数', posCount==null ? '—' : String(posCount), 'neu'],
  ]
  const cards = kpis.map(([k,v,cls])=>'<div class="exec-funds-kpi '+cls+'"><div class="k">'+esc(k)+'</div><div class="v">'+esc(v)+'</div></div>').join('')
  const meta = [
    opts.source && '来源 '+opts.source,
    data.source && '数据 '+data.source,
    data.connection_mode && '连接 '+String(data.connection_mode).toUpperCase(),
    opts.mode && '模式 '+opts.mode,
  ].filter(Boolean).join(' ｜ ')
  el.innerHTML = titleHtml + '<div class="exec-funds-kpis">'+cards+'</div>'+(meta?'<div class="exec-funds-meta">'+esc(meta)+'</div>':'')
}


async function _fallbackExecutionState(){
  const settled = await Promise.allSettled([
    api('/status'),
    api('/user/broker_config'),
    api('/bridge/process'),
    api('/mobile_ths/profile'),
    api('/bridge/live_enabled'),
  ])
  const pick = i => settled[i].status === 'fulfilled' ? (settled[i].value || {}) : {}
  const st = pick(0), broker = pick(1), bridge = pick(2), mobile = pick(3), live = pick(4)
  const brokerType = String(broker.broker_type || st.broker_type || 'paper').toLowerCase()
  const lastAccount = mobile.last_account_snapshot || {}
  const positions = Array.isArray(lastAccount.positions) ? lastAccount.positions : []
  const ratio = +(st.position_ratio || _targetPositionRatio || 0.2)
  const accountBase = +(lastAccount.total_asset || lastAccount.equity || (broker.config||{}).live_cash || st.capital || 0)
  return {
    ok: true,
    fallback: true,
    strategy: {
      running: !!(st.running || st.vibe_active),
      auto_execute: !!st.auto_execute,
      active_strategy: st.active_strategy || '',
      today_trades: +(st.today_trades || 0),
      queue_count: Array.isArray(st.queue) ? st.queue.length : 0,
    },
    execution: {
      active_target: String(mobile.executor_type || 'android').toLowerCase().includes('android') ? 'mobile' : (brokerType === 'paper' ? 'paper' : 'desktop'),
      executor_type: mobile.executor_type || 'android',
      broker_type: brokerType,
      live_trade_enabled: !!live.enabled,
    },
    mobile: {
      bridge_running: !!bridge.running,
      bridge_pid: bridge.pid || null,
      connection_mode: mobile.connection_mode || 'usb',
      trade_mode: mobile.trade_mode || 'unknown',
      confirm_mode: mobile.confirm_mode || 'manual',
      device_serial: mobile.device_serial || '',
      app_package: mobile.app_package || 'com.hexin.plat.android',
      task: mobile.mobile_task || {},
      account: {
        ok: !!lastAccount.ok,
        available: lastAccount.available == null ? null : +lastAccount.available,
        total_asset: lastAccount.total_asset == null ? null : +lastAccount.total_asset,
        market_value: lastAccount.market_value == null ? null : +lastAccount.market_value,
        positions_count: positions.length,
      },
    },
    desktop: {
      xiadan_ready: brokerType !== 'paper',
      trade_mode: brokerType === 'paper' ? 'paper' : 'live',
      trade_settings: null,
    },
    position_policy: {
      mode: 'client_fallback',
      sizing: 'equal',
      max_daily: ratio > 0 ? Math.round(1 / ratio) : 0,
      max_hold: ratio > 0 ? Math.round(1 / ratio) : 0,
      max_positions: ratio > 0 ? Math.round(1 / ratio) : 0,
      target_ratio: ratio,
      target_amount: accountBase > 0 ? +(accountBase * ratio).toFixed(2) : null,
      account_base: accountBase > 0 ? accountBase : null,
      account_source: lastAccount.total_asset ? 'mobile_snapshot' : ((broker.config||{}).live_cash ? 'live_cash' : 'account_capital'),
      max_order_amount: null,
    },
  }
}

async function refreshExecutionState(){
  const hint = document.getElementById('execStateHint')
  if(hint) hint.textContent = '正在读取执行状态...'
  let r = await api('/execution/state')
  if(!r || !r.ok){
    if(hint && r && r.status === 404) hint.textContent = '当前服务未挂载聚合接口，正在切换旧接口聚合...'
    const fallback = await _fallbackExecutionState()
    if(fallback && fallback.ok){ r = fallback }
  }
  if(!r || !r.ok){
    const msg = (r && (r.msg || r.error)) || '执行状态接口读取失败'
    _setExecText('execStrategyLamp', '未知', false)
    _setExecText('execAutoLamp', '未知', false)
    _setExecText('execMobileLamp', '未知', false)
    _setExecText('execDesktopLamp', '未知', false)
    _setExecText('execPositionLamp', '未知', false)
    if(hint){ hint.textContent = msg; hint.style.color = '#D6456A' }
    return null
  }
  const st = r.strategy || {}
  const ex = r.execution || {}
  const mobile = r.mobile || {}
  const desktop = r.desktop || {}
  const pos = r.position_policy || {}
  _setExecText('execStrategyLamp', st.running ? '运行中' : '停止', !!st.running)
  _setExecText('execAutoLamp', st.auto_execute ? '开启' : '关闭', !!st.auto_execute)
  _setExecText('execMobileLamp', mobile.bridge_running ? '已连接' : '未启动', !!mobile.bridge_running)
  _setExecText('execDesktopLamp', ex.live_trade_enabled ? '开启' : '关闭', !!ex.live_trade_enabled)
  _renderCurrentModeBadge('mobileCurrentModeBadge', mobile.trade_mode || 'unknown', '手机')
  _renderCurrentModeBadge('desktopCurrentModeBadge', desktop.trade_mode || ex.broker_type || _tradeMode, '电脑')
  // 监控页「日限」只读回填：真实生效值 = 策略级 position_config.max_daily
  const dailyEl = document.getElementById('cfgDailyLimit')
  if(dailyEl) dailyEl.textContent = pos.max_daily ? String(pos.max_daily) : '不限'
  _kpi.maxDaily = pos.max_daily ? +pos.max_daily : null
  _renderKpiBar()
  const targetRatio = pos.target_ratio!=null ? `${(+pos.target_ratio*100).toFixed(0)}%` : '—'
  const targetAmount = pos.target_amount!=null ? '¥'+(+pos.target_amount).toLocaleString('zh-CN',{maximumFractionDigits:0}) : '—'
  _setExecText('execPositionLamp', `${targetRatio} / ${targetAmount}`, true)
  const mobileCls = mobile.bridge_running ? 'bridge-switch on' : 'bridge-switch off'
  const mobileText = mobile.bridge_running ? '手机桥接 已连接' : '手机桥接 未启动'
  _setSwitchBadge(document.getElementById('execMobileBadge'), mobileCls, mobileText)
  const conn = mobile.connection_mode ? String(mobile.connection_mode).toUpperCase() : '—'
  const mode = mobile.trade_mode === 'live' ? '实盘' : (mobile.trade_mode === 'simulation' ? '模拟盘' : (mobile.trade_mode || '—'))
  const confirm = mobile.confirm_mode === 'auto' ? '全自动确认' : '手动确认'
  const mobileMeta = document.getElementById('execMobileMeta')
  if(mobileMeta) mobileMeta.textContent = `连接：${conn} ｜ 模式：${mode} ｜ 确认：${confirm}`
  if(mobile.account && (mobile.account.ok || mobile.account.available!=null || mobile.account.total_asset!=null)){
    _renderExecFundsPanel('execMobileFundsPanel', {
      ...mobile.account,
      connection_mode: mobile.connection_mode,
      device_serial: mobile.device_serial,
    }, {source:'上次手机快照', mode})
  }
  const desktopMeta = document.getElementById('execDesktopMeta')
  if(desktopMeta) desktopMeta.textContent = `交易模式：${desktop.trade_mode || ex.broker_type || 'paper'} ｜ 实盘总闸：${ex.live_trade_enabled ? '开' : '关'}`
  if(hint){
    hint.textContent = `当前策略：${st.active_strategy || '未选择'} ｜ 执行端：${ex.active_target || '按当前交易模式'} ｜ 仓位模式：${pos.mode || 'strategy'}${r.fallback ? ' ｜ 旧接口聚合' : ''}`
    hint.style.color = 'var(--t2)'
  }
  return r
}

async function refreshMobileFunds(){
  const body = document.getElementById('mobileFundsBody')
  const btn = document.getElementById('mobileFundsRefresh')
  const oldTxt = btn ? btn.textContent : '查资金/仓位'
  if(btn){ btn.disabled = true; btn.textContent = '读取中...' }
  if(body) body.innerHTML = '<div class="empty-hint">正在读取手机同花顺资金，等待手机端返回...</div>'
  _renderExecFundsPanel('execMobileFundsPanel', null, {loading:'正在读取手机同花顺资金，等待手机端返回...'})
  try{
    const r = await api('/mobile_ths/account')
    if(!r || !r.ok){
      const msg = r && r.status === 404
        ? '手机资金接口未加载：当前 5001 还是旧服务，重启见微平台后加载 /api/mobile_ths/account'
        : ((r&&(r.msg||r.error))||'读取手机资金失败')
      if(body) body.innerHTML = `<div style="color:#D6456A;font-size:12px;padding:10px">${esc(msg)}</div>`
      _renderExecFundsPanel('execMobileFundsPanel', null, {error:msg})
      _setMobileChainStatus(msg, false)
      return
    }

    let profile = null
    let data = null
    const taskId = _mobileTaskId(r)
    if(r.accepted || (r.task && r.task.running)){
      _setMobileChainStatus('手机资金读取任务执行中...', null)
      profile = await waitMobileTaskDone('account', taskId)
      data = _mobileAccountPayload(profile)
    } else {
      data = r.last_account_snapshot && r.last_account_snapshot.ok ? r.last_account_snapshot : r
    }

    if(!data || data.ok === false){
      const task = profile && profile.mobile_task || {}
      const msg = (data && (data.msg || data.error)) || (task && (task.msg || task.error)) || '手机资金读取未返回账户数据'
      if(body) body.innerHTML = `<div style="color:#D6456A;font-size:12px;padding:10px">${esc(typeof msg === 'string' ? msg : JSON.stringify(msg))}</div>`
      _renderExecFundsPanel('execMobileFundsPanel', null, {error:typeof msg === 'string' ? msg : JSON.stringify(msg)})
      _setMobileChainStatus(typeof msg === 'string' ? msg : '手机资金读取未返回账户数据', false)
      return
    }

    if(body) body.innerHTML = Array.isArray(data.positions) ? renderPositionsTable(data.positions, 'mobile', data) : '<div class="empty-hint">暂无持仓明细</div>'
    _renderExecFundsPanel('execMobileFundsPanel', data, {source:'手机同花顺', mode:data.trade_mode || data.mode || '手机'})
    if(data.available!=null){
      const mobileCapEl = document.getElementById('nMobileCapital')
      const mobileFunds = +data.available
      if(mobileCapEl){
        mobileCapEl.textContent = '¥'+mobileFunds.toLocaleString('zh-CN',{maximumFractionDigits:0})
        mobileCapEl.parentElement?.setAttribute('title', `手机同花顺可用资金：¥${mobileFunds.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})}`)
      }
    }
    _setMobileChainStatus(`手机资金读取完成：${data.source || 'mobile_ui'}`, true)
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = oldTxt || '查资金/仓位' }
  }
}

function renderMobileFundsCard(d){
  const fmt = v => (v==null||isNaN(+v)) ? '—' : '¥'+(+v).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})
  const totalAsset = d.total_asset!=null ? +d.total_asset : null
  if(Number.isFinite(totalAsset) && totalAsset>0) _accountTotalAsset = totalAsset
  const targetAmount = Number.isFinite(totalAsset) && totalAsset>0 ? totalAsset * (_targetPositionRatio || 0.2) : null
  const kpis = [
    ['可用资金', fmt(d.available), 'primary'],
    ['总资产', fmt(d.total_asset), ''],
    ['目标单仓', fmt(targetAmount), 'warn'],
    ['持仓市值', fmt(d.market_value), 'neu'],
    ['浮动盈亏', fmt(d.profit_loss), (+d.profit_loss||0) >= 0 ? 'warn' : 'primary'],
  ]
  const cards = kpis.map(([k,v,cls])=>`<div class="mobile-kpi ${cls}"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('')
  const meta = [d.connection_mode && `连接 ${String(d.connection_mode).toUpperCase()}`, d.device_serial && `设备 ${d.device_serial}`, d.source && `来源 ${d.source}`].filter(Boolean).join(' ｜ ')
  return `<div class="mobile-kpi-grid">${cards}</div>${meta?`<div class="mobile-muted" style="margin-top:8px">${esc(meta)}</div>`:''}`
}
async function refreshTodayOrders(){
  const body = document.getElementById('acctOrdersBody')
  const btn = document.getElementById('todayOrdersRefresh')
  btn.disabled = true
  const oldTxt = btn.textContent
  btn.textContent = '⏳ 截图中...'
  body.innerHTML = '<div style="padding:24px;color:var(--t3);font-size:13px;border-top:1px dashed var(--b1)">⏳ 正在读取电脑当日委托...</div>'

  // navigate=1：让桥接器在 F4 后自校准点击左侧「当日委托」再截图（拿真实成交状态）
  const r = await api('/bridge/today_orders?navigate=1')
  btn.disabled = false
  btn.textContent = oldTxt

  if(!r){ body.innerHTML = '<div style="color:#ef4444;padding:16px;border-top:1px dashed var(--b1)">⚠️ 网络错误</div>'; return }
  if(!r.ok){
    body.innerHTML = `<div style="color:#ef4444;padding:16px;border-top:1px dashed var(--b1)">⚠️ ${r.msg||'获取失败'}</div>`
    return
  }
  // 截图路径：logs/screenshots/today_orders_xxx.png → /logs/screenshots/xxx.png
  const url = '/' + r.screenshot + '?t=' + Date.now()
  const tabsHint = (r.tabs && r.tabs.length)
    ? `<div style="font-size:11px;color:var(--t3);padding:2px 0">检出查询页签：${r.tabs.join(' · ')}${r.tabs.indexOf('当日委托')<0?'（⚠️ 未检出「当日委托」，可能未导航成功，请看截图核对）':''}</div>`
    : ''
  body.innerHTML = `
    <div style="border-top:1px dashed var(--b1);margin-top:8px;padding-top:8px">
      <div style="font-size:13px;font-weight:600;color:var(--t1);margin:2px 0">📋 当日委托</div>
      <div style="font-size:11px;color:var(--t3);padding:4px 0">${r.msg||''}</div>
      ${tabsHint}
      ${renderOrdersTable(r.orders)}
      <details ${(r.orders&&r.orders.length)?'':'open'} style="margin-top:10px">
        <summary style="cursor:pointer;font-size:12px;color:var(--t3)">账户页面凭证</summary>
        <img src="${url}" style="max-width:100%;border:1px solid var(--b1);border-radius:6px;margin-top:8px" alt="当日委托截图">
      </details>
    </div>
  `
}

// ── 实盘资金 + 持仓面板（F4「资金股票」页 OCR；模拟盘读本地账本）──
async function refreshAccountSnapshot(){
  const body = document.getElementById('acctSnapshotBody')
  const btn  = document.getElementById('acctSnapRefresh')
  if(btn) btn.disabled = true
  const oldTxt = btn ? btn.textContent : '查资金/持仓'
  if(btn) btn.textContent = '⏳ 读取中...'
  if(body) body.innerHTML = '<div style="padding:24px;color:var(--t3);font-size:13px;text-align:center">⏳ 正在读取账户资金和持仓...</div>'
  _renderExecFundsPanel('execDesktopFundsPanel', null, {loading:'正在读取电脑侧账户资金和持仓...'})

  const r = await api('/account/snapshot')
  if(btn) btn.disabled = false
  if(btn) btn.textContent = oldTxt

  if(!r){
    if(body) body.innerHTML = '<div style="color:#ef4444;padding:16px">⚠️ 网络错误</div>'
    _renderExecFundsPanel('execDesktopFundsPanel', null, {error:'网络错误'})
    return
  }
  if(!r.ok){
    const msg = r.msg || '获取失败'
    if(body) body.innerHTML = `<div style="color:#ef4444;padding:16px">⚠️ ${esc(msg)}</div>`
    _renderExecFundsPanel('execDesktopFundsPanel', null, {error:msg})
    return
  }

  let html = renderPositionsTable(r.positions, r.mode, r)
  if(r.screenshot){
    const url = '/' + r.screenshot + '?t=' + Date.now()
    html += `<details style="margin-top:10px">
      <summary style="cursor:pointer;font-size:12px;color:var(--t3)">账户页面凭证</summary>
      <img src="${url}" style="max-width:100%;border:1px solid var(--b1);border-radius:6px;margin-top:8px" alt="账户截图">
    </details>`
  }
  if(r.msg) html += `<div style="font-size:11px;color:var(--t3);padding:6px 0">${r.msg}</div>`
  if(body) body.innerHTML = html
  _renderExecFundsPanel('execDesktopFundsPanel', r, {source:r.mode === 'paper' ? '电脑模拟盘' : '电脑客户端', mode:r.mode || 'desktop'})
}

// 资金概览卡片：可用资金 / 总资产 / 持仓市值
function renderFundsCard(d){
  const fmt = v => (v==null||isNaN(+v)) ? '—' : '¥'+(+v).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})
  const totalAsset = d.total_asset!=null ? +d.total_asset : (d.equity!=null ? +d.equity : null)
  if(Number.isFinite(totalAsset) && totalAsset>0) _accountTotalAsset = totalAsset
  const targetAmount = Number.isFinite(totalAsset) && totalAsset>0 ? totalAsset * (_targetPositionRatio || 0.2) : null
  const modeBadge = d.mode==='paper'
    ? '<span style="font-size:10px;color:#0ea5e9;border:1px solid #0ea5e9;border-radius:4px;padding:1px 6px">模拟盘（本地账本）</span>'
    : '<span style="font-size:10px;color:#ef4444;border:1px solid #ef4444;border-radius:4px;padding:1px 6px">实盘账户</span>'
  const cards = [['可用资金', fmt(d.available), '#22c55e'], ['总资产', fmt(d.total_asset), 'var(--t1)'], ['目标单仓', fmt(targetAmount), '#f59e0b']]
  if(d.market_value!=null) cards.push(['持仓市值', fmt(d.market_value), 'var(--t2)'])
  const cardHtml = cards.map(([k,v,c])=>`
    <div style="flex:1;min-width:120px;background:rgba(255,255,255,.03);border:1px solid var(--b1);border-radius:8px;padding:10px 12px">
      <div style="font-size:11px;color:var(--t3)">${k}</div>
      <div style="font-size:18px;font-weight:700;color:${c};font-family:monospace">${v}</div>
    </div>`).join('')
  return `<div style="margin:2px 0 8px">${modeBadge}</div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">${cardHtml}</div>`
}

// 持仓表：模拟盘结构化字段齐全；实盘 OCR 仅 code/name 可靠 + numbers/raw 供参考
function renderPositionsTable(positions, mode, account){
  if(!positions || !positions.length){
    return '<div style="font-size:12px;color:var(--t3);padding:8px 0">暂无持仓明细</div>'
  }
  const isPaper = mode==='paper' || positions[0].cost_price!=null
  if(isPaper){
    const weightBase = _positionWeightBase(positions, account)
    const targetPct = ((_targetPositionRatio || 0.2) * 100).toFixed(0)
    const rows = positions.map(p=>{
      const curPrice = p.cur_price!=null ? +p.cur_price : +(p.current_price||0)
      const pnl = p.pnl_amount!=null ? +p.pnl_amount : +(p.profit_loss||0)
      const pc  = pnl>0?'#ef4444':(pnl<0?'#22c55e':'var(--t2)')   // A股红涨绿跌
      const pnlPct = p.pnl_pct!=null ? +p.pnl_pct : +(p.profit_rate||0)
      const pct = ((pnlPct>0?'+':'')+(Number.isFinite(pnlPct)?pnlPct:0))+'%'
      const marketValue = +(p.market_value||0)
      return `<tr>
        <td style="padding:4px 8px;font-family:monospace">${p.code||'-'}</td>
        <td style="padding:4px 8px">${p.name||'-'}</td>
        <td style="padding:4px 8px;text-align:right">${p.shares||0}</td>
        <td style="padding:4px 8px;text-align:right">${(+p.cost_price||0).toFixed(3)}</td>
        <td style="padding:4px 8px;text-align:right">${curPrice.toFixed(2)}</td>
        <td style="padding:4px 8px;text-align:right;color:${pc};font-weight:600">${pnl.toFixed(2)}<br><span style="font-size:10px">${pct}</span></td>
        <td style="padding:4px 8px;text-align:right">${marketValue.toFixed(0)}${_positionWeightHtml(marketValue, weightBase)}</td>
      </tr>`
    }).join('')
    return `<div style="font-size:11px;color:var(--t3);padding:2px 0">目标均仓：${targetPct}% / 只，实际按市值÷总资产显示</div>
      <div class="exec-position-wrap"><table style="width:100%;border-collapse:collapse;font-size:12px;margin:6px 0">
      <thead><tr style="border-bottom:1px solid var(--b1);color:var(--t3)">
        <th style="padding:4px 8px;text-align:left">代码</th><th style="padding:4px 8px;text-align:left">名称</th>
        <th style="padding:4px 8px;text-align:right">股数</th><th style="padding:4px 8px;text-align:right">成本</th>
        <th style="padding:4px 8px;text-align:right">现价</th><th style="padding:4px 8px;text-align:right">盈亏</th>
        <th style="padding:4px 8px;text-align:right">市值/仓位</th>
      </tr></thead><tbody>${rows}</tbody></table></div>`
  }
  // 实盘 OCR：代码/名称可靠，数字列仅供参考
  const rows = positions.map(p=>{
    const nums = (p.numbers||[]).join(' / ')
    return `<tr>
      <td style="padding:4px 8px;font-family:monospace">${p.code||'-'}</td>
      <td style="padding:4px 8px">${p.name||'-'}</td>
      <td style="padding:4px 8px;font-size:11px;color:var(--t2)">${nums||'-'}</td>
      <td style="padding:4px 8px;font-size:11px;color:var(--t3)" title="${(p.raw||'').replace(/"/g,'&quot;')}">${p.raw||''}</td>
    </tr>`
  }).join('')
  return `<div style="font-size:11px;color:#f59e0b;padding:2px 0">实盘持仓明细来自账户页面，数字列用于快速核对</div>
    <div class="exec-position-wrap"><table style="width:100%;border-collapse:collapse;font-size:12px;margin:6px 0">
    <thead><tr style="border-bottom:1px solid var(--b1);color:var(--t3)">
      <th style="padding:4px 8px;text-align:left">代码</th><th style="padding:4px 8px;text-align:left">名称</th>
      <th style="padding:4px 8px;text-align:left">识别数字</th><th style="padding:4px 8px;text-align:left">页面原文</th>
    </tr></thead><tbody>${rows}</tbody></table></div>`
}
// 委托状态 → 颜色
function _orderStatusColor(s){
  if(!s) return 'var(--t3)'
  if(s.indexOf('全部成交')>=0 || s==='已成交' || s==='已成') return '#22c55e'
  if(s.indexOf('部')>=0) return '#0ea5e9'
  if(s.indexOf('撤')>=0 || s.indexOf('废')>=0) return '#ef4444'
  if(s.indexOf('报')>=0 || s.indexOf('未')>=0) return '#f59e0b'
  return 'var(--t2)'
}
// 把 OCR 解析出的结构化委托渲染成表格
function renderOrdersTable(orders){
  if(!orders || !orders.length){
    return '<div style="font-size:12px;color:var(--t3);padding:8px 0">暂无委托明细</div>'
  }
  const _n = v => (v==null||isNaN(+v)) ? '-' : (+v).toLocaleString('zh-CN')
  let rows = orders.map(o=>{
    const c = _orderStatusColor(o.status)
    const act = o.action==='买入' ? '<span style="color:#ef4444">买入</span>'
              : o.action==='卖出' ? '<span style="color:#22c55e">卖出</span>'
              : (o.action||'-')
    return `<tr>
      <td style="padding:4px 8px;font-family:monospace">${o.code||'-'}</td>
      <td style="padding:4px 8px">${o.name||'-'}</td>
      <td style="padding:4px 8px;text-align:center">${act}</td>
      <td style="padding:4px 8px;text-align:center;color:${c};font-weight:600">${o.status||'-'}</td>
      <td style="padding:4px 8px;text-align:right">${_n(o.order_qty)}</td>
      <td style="padding:4px 8px;text-align:right">${_n(o.filled_qty)}</td>
      <td style="padding:4px 8px;text-align:right">${o.order_price==null?'-':(+o.order_price).toFixed(3)}</td>
      <td style="padding:4px 8px;font-size:11px;color:var(--t3)" title="${(o.raw||'').replace(/"/g,'&quot;')}">${(o.raw||'').slice(0,40)}${(o.raw||'').length>40?'…':''}</td>
    </tr>`
  }).join('')
  return `<table style="width:100%;border-collapse:collapse;font-size:12px;margin:6px 0">
    <thead><tr style="border-bottom:1px solid var(--b1);color:var(--t3)">
      <th style="padding:4px 8px;text-align:left">代码</th>
      <th style="padding:4px 8px;text-align:left">名称</th>
      <th style="padding:4px 8px">买卖</th>
      <th style="padding:4px 8px">状态</th>
      <th style="padding:4px 8px;text-align:right">委托量</th>
      <th style="padding:4px 8px;text-align:right">成交量</th>
      <th style="padding:4px 8px;text-align:right">委托价</th>
      <th style="padding:4px 8px;text-align:left">页面原文</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`
}

// ══════════════════════════════════════════════════
//  更新日志
// ══════════════════════════════════════════════════
const _clTypeLabel = {feature:'🆕 新功能',fix:'🐛 修复',optimize:'⚡ 优化',security:'🔒 安全',notice:'📢 公告'}
const _clTypeColor = {feature:'#22c55e',fix:'#ef4444',optimize:'#f59e0b',security:'#8b5cf6',notice:'#3b82f6'}
function openChangelog(){
  document.getElementById('changelogModal').classList.add('open')
  loadChangelog()
}
function closeChangelog(ev){
  if(ev && ev.target !== document.getElementById('changelogModal')) return
  document.getElementById('changelogModal').classList.remove('open')
}
async function loadChangelog(){
  const body = document.getElementById('changelogBody')
  body.innerHTML = '<div style="color:var(--t3);text-align:center;padding:20px">加载中...</div>'
  try{
    const r = await fetch('/api/changelog?limit=50')
    const d = await r.json()
    if(!d.ok || !d.items.length){
      body.innerHTML = '<div style="color:var(--t3);text-align:center;padding:20px">暂无更新日志</div>'
      return
    }
    body.innerHTML = d.items.map(item => {
      const t = _clTypeLabel[item.change_type] || item.change_type
      const c = _clTypeColor[item.change_type] || 'var(--t2)'
      const ver = item.version ? `<span style="background:var(--inp);border:1px solid var(--b1);border-radius:10px;padding:1px 8px;font-size:11px;color:var(--t2)">${item.version}</span>` : ''
      return `
        <div style="border-bottom:1px solid var(--b1);padding:12px 0">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span style="color:${c};font-size:12px;font-weight:600">${t}</span>
            <span style="font-size:14px;font-weight:600;color:var(--t1)">${item.title}</span>
            ${ver}
            <span style="margin-left:auto;font-size:11px;color:var(--t4)">${item.created_at||''}</span>
          </div>
          ${item.content ? `<div style="margin-top:6px;font-size:13px;color:var(--t2);line-height:1.6;white-space:pre-wrap">${item.content}</div>` : ''}
        </div>
      `
    }).join('')
  }catch(e){
    body.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px">加载失败</div>'
  }
}

async function confirmSwitchToLive(){
  // 读取 live_cash 输入框（必填）
  const liveCashEl = document.getElementById('liveCashInput')
  const liveCash = parseFloat(liveCashEl?.value || 0)
  if(!liveCash || liveCash < 1000){
    alert('⚠️ 请填入 xiadan 真账户可用余额（至少 1000 元）\n\n这个值用来计算单笔仓位 = 余额 × 仓位%。\n如果填错，预估金额会超过 MAX_ORDER_AMOUNT 被桥接器拒。')
    liveCashEl?.focus()
    return
  }
  document.getElementById('liveModeModal').classList.remove('open')
  // 先把弹窗里选的自动化设置存下来（实盘下单/撤单会按这个走）
  try{ const sr = await api('/account/trade_settings', 'POST', _readTradeRadios('opt')); if(sr&&sr.ok) _renderAutoModeBadge(sr) }catch(e){}
  await _switchTradeMode('live', { live_cash: liveCash })
}

async function _switchTradeMode(newMode, extraConfig){
  // 切到实盘要带 live_cash 进 config；切回模拟保留旧 config（不清空 live_cash 历史值）
  let postBody
  if(newMode === 'live'){
    postBody = { broker_type: 'live', config: extraConfig || {} }
  } else {
    // 切回模拟时也保留已有 live_cash（用户切回来再切去时不必重新输）
    let existing = {}
    try {
      const cur = await api('/user/broker_config')
      existing = cur?.config || {}
    } catch(e){}
    postBody = { broker_type: 'paper', config: existing }
  }
  const r = await api('/user/broker_config', 'POST', postBody)
  if(r?.ok){
    _tradeMode = newMode
    _renderTradeModeBadge()
    refreshExecutionState()
    const msg = newMode === 'live'
      ? `⚠️ 已切到实盘模式 — 真余额 ¥${(extraConfig?.live_cash || 0).toLocaleString()}，仓位按此 × 20% 算。请确认桥接器在运行`
      : '✅ 已切回模拟模式 — 后续所有买卖单走 paper_broker'
    if(typeof setEdSt === 'function') setEdSt(msg)
    else alert(msg)
  } else {
    alert('切换失败: ' + (r?.error || '未知错误'))
  }
}

// 打开 Modal 时回填已有 live_cash
async function openLiveModeModalEnsureCash(){
  try {
    const cur = await api('/user/broker_config')
    const cash = parseFloat((cur?.config || {}).live_cash || 0)
    const inp = document.getElementById('liveCashInput')
    if(inp && cash > 0) inp.value = cash
  } catch(e){}
}

// ══════════════════════════════════════════════════
//  见微 Alpha 内置因子库
// ══════════════════════════════════════════════════
let _vibeFactors = []  // 缓存：[{id, theme, formula, universe, ...}]
let _vibeMode = 'manual'  // 'manual' | 'market_top'

async function openVibeFactors(){
  document.getElementById('vibeModal').classList.add('open')
  if(_vibeFactors.length === 0){
    // 首次打开拉因子列表
    const sel = document.getElementById('vibeFactorSel')
    sel.innerHTML = '<option value="">加载中...</option>'
    const r = await api('/factors_zoo/list')
    if(!r?.ok){
      sel.innerHTML = '<option value="">加载失败</option>'
      document.getElementById('vibeFactorCount').textContent = ''
      return
    }
    _vibeFactors = r.factors || []
    // 按 zoo 分组（<optgroup>）便于浏览 4 大因子库
    const zooMap = {}
    _vibeFactors.forEach(f => {
      (zooMap[f.zoo] = zooMap[f.zoo] || []).push(f)
    })
    const zooLabels = {
      gtja191:  '见微 A 股增强因子集 (A股微观结构优化)',
      qlib158:  '见微 量价工程因子集 (滚动均值/动量/波动率)',
      alpha101: '见微 短线公式因子集 (高频量价)',
      academic: '见微 经典风格因子集 (Size/Value/Momentum)',
    }
    sel.innerHTML = Object.keys(zooMap).sort().map(zoo => {
      const opts = zooMap[zoo].map(f =>
        `<option value="${f.id}" title="${(f.description_cn||'').replace(/"/g,'&quot;')}">${f.id}${f.theme && f.theme.length ? ' · ' + f.theme.join('/') : ''}</option>`
      ).join('')
      return `<optgroup label="📚 ${zooLabels[zoo] || zoo}">${opts}</optgroup>`
    }).join('') || '<option value="">（空）</option>'
    document.getElementById('vibeFactorCount').textContent =
      `共 ${_vibeFactors.length} 个 · ${Object.keys(zooMap).length} 个库`
    renderVibeFactorMeta()
  }
}

function closeVibeFactors(ev){
  if(ev && ev.target !== document.getElementById('vibeModal')) return
  document.getElementById('vibeModal').classList.remove('open')
}

function renderVibeFactorMeta(){
  const id = document.getElementById('vibeFactorSel').value
  const meta = _vibeFactors.find(f => f.id === id)
  const box = document.getElementById('vibeFactorMeta')
  if(!meta){ box.style.display = 'none'; return }
  box.style.display = 'block'
  // 中文描述（最显眼）
  const desc = meta.description_cn || ''
  document.getElementById('vfm-desc').textContent = desc
  document.getElementById('vfm-desc').style.display = desc ? '' : 'none'
  // 数学公式
  document.getElementById('vfm-formula').textContent = meta.formula || '(无公式)'
  // 标签
  const tags = []
  if(meta.zoo) tags.push(`<span style="background:rgba(14,165,233,.15);color:#0ea5e9;padding:1px 6px;border-radius:3px;margin-right:4px">${meta.zoo}</span>`)
  if(meta.universe) meta.universe.forEach(u => tags.push(`<span style="background:rgba(34,197,94,.15);color:#22c55e;padding:1px 6px;border-radius:3px;margin-right:4px">${u}</span>`))
  if(meta.theme) meta.theme.forEach(t => tags.push(`<span style="background:rgba(168,85,247,.15);color:#a855f7;padding:1px 6px;border-radius:3px;margin-right:4px">${t}</span>`))
  if(meta.min_warmup_bars) tags.push(`<span style="color:var(--t3)">预热 ${meta.min_warmup_bars} 天</span>`)
  document.getElementById('vfm-tags').innerHTML = tags.join('')

  // 自动调整 days 输入框：≥ min_warmup_bars + 20
  const daysInp = document.getElementById('vibeDays')
  if(daysInp && meta.min_warmup_bars){
    const need = meta.min_warmup_bars + 20
    const cur = parseInt(daysInp.value) || 60
    if(cur < need){
      daysInp.value = need
      daysInp.style.background = 'rgba(245,158,11,.15)'
      daysInp.title = `因子需要至少 ${meta.min_warmup_bars} 天预热，已自动调整为 ${need} 天`
      setTimeout(() => { daysInp.style.background = '' }, 2000)
    }
  }
}

function setVibeMode(mode){
  _vibeMode = mode
  document.getElementById('vibeModeManual').classList.toggle('btn-blue', mode === 'manual')
  document.getElementById('vibeModeMarket').classList.toggle('btn-blue', mode === 'market_top')
  document.getElementById('vibePanelManual').style.display = mode === 'manual' ? 'block' : 'none'
  document.getElementById('vibePanelMarket').style.display = mode === 'market_top' ? 'block' : 'none'
}

async function runVibeFactor(){
  const factorId = document.getElementById('vibeFactorSel').value
  if(!factorId){ alert('请选择因子'); return }
  const days = parseInt(document.getElementById('vibeDays').value) || 60
  const btn = document.getElementById('vibeRunBtn')
  const resBox = document.getElementById('vibeResult')
  btn.disabled = true
  const oldTxt = btn.textContent
  btn.textContent = '⏳ 计算中...'
  resBox.innerHTML = '<div class="empty-hint" style="text-align:center;padding:20px 0;color:var(--t3);font-size:12px">⏳ 准备计算...</div>'

  const body = { factor_id: factorId, mode: _vibeMode, days }
  let expectedCount = 0
  if(_vibeMode === 'manual'){
    body.codes = document.getElementById('vibeCodes').value.trim()
    if(!body.codes){ alert('请输入股票代码'); btn.disabled = false; btn.textContent = oldTxt; return }
    expectedCount = body.codes.split(/[,，]/).filter(x=>x.trim()).length
  } else {
    body.max_codes = parseInt(document.getElementById('vibeMaxCodes').value)
    if(isNaN(body.max_codes)) body.max_codes = 500
    expectedCount = body.max_codes === 0 ? 5000 : body.max_codes
  }

  const t0 = Date.now()
  // 超过 200 只走异步任务（避免 HTTP 长连接超时 + 显示进度条）
  let r
  if(expectedCount > 200){
    r = await _runVibeFactorAsync(body, factorId, resBox, t0)
  } else {
    resBox.innerHTML = '<div class="empty-hint" style="text-align:center;padding:20px 0;color:var(--t3);font-size:12px">⏳ 计算中...</div>'
    r = await api('/factors_zoo/compute', 'POST', body)
  }
  btn.disabled = false
  btn.textContent = oldTxt

  // 隐藏"加入选股池"按钮（结果出来才显）
  document.getElementById('vibeAddPool').hidden = true
  if(!r){ resBox.innerHTML = '<div style="color:#ef4444;padding:14px">⚠️ 网络错误或请求超时</div>'; return }
  if(!r.ok){ resBox.innerHTML = `<div style="color:#ef4444;padding:14px">⚠️ ${r.error||'计算失败'}</div>`; return }

  const ranked = r.ranked || []
  const validCount = ranked.filter(x => x.score !== null).length
  const elapsed = ((Date.now() - t0)/1000).toFixed(1)
  const covMsg = r.scanned < r.requested
    ? `<span style="color:#f59e0b">扫描成功 ${r.scanned}/${r.requested} 只（数据源限制，建议手动输入有缓存的代码）</span>`
    : `<span style="color:#22c55e">扫描 ${r.scanned} 只全部成功</span>`

  let html = `<div style="font-size:11px;color:var(--t3);margin-bottom:6px">
    ${covMsg} · 有效分数 ${validCount} 只 · 耗时 ${elapsed}s（服务端 ${r.took_ms}ms）
  </div>`

  if(ranked.length === 0){
    html += '<div class="empty-hint" style="text-align:center;padding:20px 0;color:var(--t3)">无结果</div>'
  } else {
    html += `<table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead><tr style="background:var(--inp);color:var(--t3)">
        <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--b1)">排名</th>
        <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--b1)">代码</th>
        <th style="padding:6px 8px;text-align:left;border-bottom:1px solid var(--b1)">名称</th>
        <th style="padding:6px 8px;text-align:right;border-bottom:1px solid var(--b1)">现价</th>
        <th style="padding:6px 8px;text-align:right;border-bottom:1px solid var(--b1)">分数</th>
        <th style="padding:6px 8px;text-align:center;border-bottom:1px solid var(--b1)">操作</th>
      </tr></thead><tbody>`
    ranked.slice(0, 50).forEach((row, i) => {
      const medal = row.rank === 1 ? '🥇' : (row.rank === 2 ? '🥈' : (row.rank === 3 ? '🥉' : `#${row.rank}`))
      const scoreStr = row.score === null ? '<span style="color:var(--t3)">—</span>' :
        `<span style="color:${row.score >= 0 ? '#22c55e' : '#ef4444'};font-family:monospace">${row.score.toFixed(4)}</span>`
      const priceStr = (row.price && row.price > 0)
        ? `<span style="font-family:monospace">¥${row.price.toFixed(2)}</span>`
        : '<span style="color:var(--t3)">—</span>'
      // 操作列：买入按钮（无价或分数为 null 时禁用）
      const canBuy = (row.price && row.price > 0) && (row.score !== null)
      const btnHtml = canBuy
        ? `<button class="btn btn-xs" style="background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff;padding:2px 8px;font-size:11px" data-ui-click="vibeQuickBuy(${i})" title="按顶部「模拟/实盘」总开关下单">🛒 买入</button>`
        : `<button class="btn btn-xs btn-gray" style="padding:2px 8px;font-size:11px" disabled title="无现价 / 分数无效">—</button>`
      html += `<tr style="border-bottom:1px solid var(--b1)">
        <td style="padding:5px 8px">${medal}</td>
        <td style="padding:5px 8px;font-family:monospace">${row.code}</td>
        <td style="padding:5px 8px">${row.name||''}</td>
        <td style="padding:5px 8px;text-align:right">${priceStr}</td>
        <td style="padding:5px 8px;text-align:right">${scoreStr}</td>
        <td style="padding:5px 8px;text-align:center">${btnHtml}</td>
      </tr>`
    })
    html += '</tbody></table>'
    if(ranked.length > 50){
      html += `<div style="font-size:10px;color:var(--t3);text-align:center;padding:6px 0">仅展示前 50 行（共 ${ranked.length} 只）</div>`
    }
    // 有结果 → 显示"加入选股池"按钮
    _vibeLastResult = { factor_id: factorId, ranked, factor_meta: r.factor_meta }
    document.getElementById('vibeAddPool').hidden = false
    document.getElementById('vibeAddTopN').max = Math.max(1, validCount)
    document.getElementById('vibeAddTopN').value = Math.min(20, Math.max(1, validCount))
  }
  resBox.innerHTML = html
}

// Vibe 结果表 行内"🛒 买入"  —— 顶部「模拟/实盘」开关决定路径
//   - 模拟模式: broker_type=paper → paper_broker.submit_buy 立即成交
//   - 实盘模式: broker_type≠paper → 写 trade_signals 给桥接器拉走按 F1
//   后端 /api/paper/buy 是统一入口，会根据 broker_type 自动选路径
async function vibeQuickBuy(rowIdx){
  if(!_vibeLastResult || !_vibeLastResult.ranked) return
  const row = _vibeLastResult.ranked[rowIdx]
  if(!row){ alert('行号越界'); return }
  if(!row.price || row.price <= 0){ alert('无现价无法下单'); return }

  const isLive = (typeof _tradeMode !== 'undefined' && _tradeMode === 'live')
  const modeLabel = isLive ? '🔴 实盘' : '🛡️ 模拟'
  const factorLabel = _vibeLastResult.factor_id || '因子'

  // 实盘模式：先拿 broker_config 看 live_cash 是否设置，并显示给用户
  let liveCash = 0
  if(isLive){
    try {
      const cfgResp = await api('/user/broker_config')
      liveCash = parseFloat((cfgResp?.config || {}).live_cash || 0)
    } catch(e){}
    if(!liveCash || liveCash <= 0){
      alert('⚠️ 实盘下单前必须先配置 xiadan 真账户余额\n\n请回到顶部 🔴 实盘 开关 → 点开二次确认 Modal → 在余额栏填入 200000（或你 xiadan 上的真实可用金额）\n\n这样系统才能按 20% 仓位 × 真余额 算出正确手数（而不是用模拟盘 1000000 的虚假基数导致超限被金额护栏拒）')
      return
    }
  }

  // 拉取真实账户设置（仓位比 + 护栏 + 当前持仓数），与后端口径一致
  let positionRatio = 0.20, singleCap = 0.20, maxPos = 5, heldCount = 0
  try {
    const acct = await api('/account')
    if(acct){
      positionRatio = +acct.position_ratio || 0.20
      singleCap = +acct.single_position_max_ratio || 0.20
      maxPos = +acct.max_positions || 5
      heldCount = Object.keys(acct.positions || {}).length
    }
  } catch(e){}
  // 单笔上限护栏：生效仓位比 = min(仓位比, 单笔上限)（后端 paper_manual_buy 同样口径）
  const effRatio = Math.min(positionRatio, singleCap)
  const capped = effRatio < positionRatio

  // 二次确认
  const targetBudget = isLive ? (liveCash * effRatio) : null
  const estShares = isLive
    ? Math.max(Math.floor(targetBudget / row.price / 100) * 100, 100)
    : null
  const guardLine =
      `🛡 仓位护栏:\n`
    + `   单笔上限: ${(singleCap*100).toFixed(0)}%`
    + (capped ? ` ← 已夹住仓位比 ${(positionRatio*100).toFixed(0)}%，按 ${(effRatio*100).toFixed(0)}% 建仓\n`
              : `（仓位比 ${(positionRatio*100).toFixed(0)}% 未超限）\n`)
    + `   最多持仓: ${maxPos} 只（当前持 ${heldCount} 只）`
    + (heldCount >= maxPos ? ` ⚠️ 已达上限，自动扫描不再开新仓\n` : `\n`)

  const msg = `${modeLabel} · 立即买入\n\n`
    + `股票: ${row.code} ${row.name||''}\n`
    + `现价: ¥${row.price.toFixed(2)}\n`
    + `分数: ${row.score === null ? '—' : row.score.toFixed(4)} (来自 ${factorLabel})\n\n`
    + (isLive
        ? `📊 仓位计算（基于 xiadan 真账户）:\n`
          + `   真余额: ¥${liveCash.toLocaleString()}\n`
          + `   生效仓位比: ${(effRatio*100).toFixed(0)}%\n`
          + `   预算: ¥${targetBudget.toLocaleString()}\n`
          + `   预估手数: ${estShares} 股\n`
          + `   预估金额: ¥${(estShares * row.price).toFixed(0)}\n\n`
          + guardLine + `\n`
          + '⚠️ 实盘模式：信号 → 桥接器 → xiadan 按 F1 真下单'
        : `📊 后端按生效仓位比 ${(effRatio*100).toFixed(0)}% 自动算手数\n\n`
          + guardLine + `\n`
          + '✅ 模拟模式：仅动 paper_trading.db，无任何资金风险。')
  if(!confirm(msg)) return

  try {
    const res = await api('/paper/buy', 'POST', {
      code:   row.code,
      name:   row.name || row.code,
      price:  row.price,
      shares: 0,   // 后端按 broker_type 自动算
    })
    if(!res){ alert('网络错误'); return }
    if(!res.ok){
      alert('❌ ' + (res.msg || '下单失败'))
      return
    }

    // 模拟模式：res 已是终态，直接显示
    if(!isLive){
      const detail = res.msg || `已模拟买入 ${row.code} ${res.shares || ''}股`
      if(typeof showToast === 'function') showToast('✅ ' + detail, 'success')
      else alert('✅ ' + detail)
      if(typeof fetchSt === 'function') fetchSt()
      if(typeof fetchPositions === 'function') setTimeout(fetchPositions, 500)
      return
    }

    // 实盘模式：res.signal_id 是异步的，要轮询桥接器回传结果
    const signalId = res.signal_id
    if(!signalId){
      alert('⏳ ' + (res.msg || '实盘信号已下发')+ '\n\n但未拿到 signal_id，无法跟踪执行结果。\n请在监控中心日志查看实际状态。')
      return
    }
    if(typeof showToast === 'function') showToast(`⏳ 信号 ${signalId} 已下发，等待桥接器执行...`, 'info')

    // 轮询 8 次 × 2 秒 = 最长等 16 秒（含弹委托框 + AUTO_CONFIRM=False 等手动按 Y 的时间）
    let finalStatus = null
    for(let i = 0; i < 8; i++){
      await new Promise(r => setTimeout(r, 2000))
      try {
        const st = await api('/bridge/signals/' + signalId + '/status')
        if(st?.ok && st.signal){
          const s = st.signal.status
          // executed/failed/cancelled = 终态；submitted = 已按 F1 提交但未确认；sent = 桥接器收到尚未按 F1
          if(s === 'executed' || s === 'failed' || s === 'cancelled' || s === 'submitted'){
            finalStatus = st.signal
            break
          }
        }
      } catch(e){}
    }

    if(finalStatus){
      const s = finalStatus.status
      // submitted = 已提交到券商但桥接器没等到确认弹框（多半已成交，但需要查当日委托）
      // executed  = 桥接器明确看到"委托成功"
      // failed    = 资金不足/价格偏离/封板等
      // cancelled = 用户/系统取消
      let icon, head
      if(s === 'executed'){ icon = '✅'; head = '实盘买入成功' }
      else if(s === 'submitted'){ icon = '⏳'; head = '已提交到券商，请在 xiadan「当日委托」核对' }
      else if(s === 'failed'){ icon = '❌'; head = '实盘买入失败' }
      else if(s === 'cancelled'){ icon = '🚫'; head = '已取消' }
      else { icon = '⏳'; head = `状态 ${s}` }
      alert(`${icon} ${head}\n\n股票: ${row.code} ${row.name||''}\n状态: ${s}\n消息: ${finalStatus.report_msg || '-'}\n${finalStatus.exec_price ? '成交价: ¥'+finalStatus.exec_price : ''}`)
    } else {
      alert(`⏳ 信号 ${signalId} 已下发到桥接器，16 秒内未拿到回传\n\n可能原因:\n• xiadan 未在线 / 桥接器卡住\n• AUTO_CONFIRM_ORDER=False 时你还没在 xiadan 上按 Y 提交\n• 委托正在排队\n\n请查 xiadan「当日委托」+ 监控中心日志 + bridge.log`)
    }
    if(typeof fetchSt === 'function') fetchSt()
    if(typeof fetchPositions === 'function') setTimeout(fetchPositions, 500)
  } catch(e){
    alert('异常: ' + e.message)
  }
}

// 异步任务模式：启动 → 轮询进度 → 取结果（适合 > 200 只）
async function _runVibeFactorAsync(body, factorId, resBox, t0){
  const startResp = await api('/factors_zoo/compute_async', 'POST', body)
  if(!startResp?.ok){
    return startResp  // 让外层错误处理走
  }
  const taskId = startResp.task_id
  const totalCodes = startResp.total || 0
  resBox.innerHTML = `
    <div style="padding:14px">
      <div id="vibeProgPhase" style="font-size:12px;color:var(--t2);margin-bottom:8px">⏳ 启动中... (task=${taskId})</div>
      <div style="background:var(--inp);border:1px solid var(--b1);border-radius:6px;height:8px;overflow:hidden">
        <div id="vibeProgBar" style="height:100%;width:0%;background:linear-gradient(90deg,#0ea5e9,#06b6d4);transition:width 0.5s"></div>
      </div>
      <div id="vibeProgInfo" style="font-size:10px;color:var(--t3);margin-top:6px;font-family:monospace"></div>
    </div>
  `
  // 轮询进度
  for(let i = 0; i < 1800; i++){  // 最多 30 分钟（1800 × 1s）
    await new Promise(r => setTimeout(r, 1000))
    const prog = await api(`/factors_zoo/progress/${taskId}`)
    if(!prog?.ok){ return { ok: false, error: prog?.error || '进度查询失败' } }
    const phaseEl = document.getElementById('vibeProgPhase')
    const barEl   = document.getElementById('vibeProgBar')
    const infoEl  = document.getElementById('vibeProgInfo')
    if(!phaseEl) return { ok: false, error: '用户取消' }   // Modal 被关掉了
    phaseEl.textContent = `⏳ ${prog.phase || '处理中'}（任务 ${taskId}）`
    if(prog.scan_total > 0){
      const pct = Math.min(99, Math.round(prog.scan_current / prog.scan_total * 100))
      barEl.style.width = pct + '%'
      infoEl.textContent = `K线扫描 ${prog.scan_current}/${prog.scan_total} (${pct}%) [${prog.scan_code||''}] · 已用 ${((Date.now()-t0)/1000).toFixed(0)}s`
    } else {
      infoEl.textContent = `已用 ${((Date.now()-t0)/1000).toFixed(0)}s`
    }
    if(prog.status === 'done'){
      barEl.style.width = '100%'
      // 拉结果
      const resp = await api(`/factors_zoo/result/${taskId}`)
      return resp
    }
    if(prog.status === 'error'){
      return { ok: false, error: prog.error || '后台任务失败' }
    }
  }
  return { ok: false, error: '任务超时（30 分钟）' }
}

// 因子回测：用当前 Modal 配置（手动 codes 或 market_top）+ 默认参数跑历史
async function runVibeBacktest(){
  const factorId = document.getElementById('vibeFactorSel').value
  if(!factorId){ alert('请选择因子'); return }
  const btn = document.getElementById('vibeBtBtn')
  const resBox = document.getElementById('vibeResult')
  btn.disabled = true
  const oldTxt = btn.textContent
  btn.textContent = '⏳ 回测中...'

  const body = {
    factor_id: factorId,
    mode: _vibeMode,
    days: 200,           // 历史天数
    top_k: 10,
    rebalance_days: 5,
    hold_days: 5,
    fee_rate: 0.0,
  }
  if(_vibeMode === 'manual'){
    body.codes = document.getElementById('vibeCodes').value.trim()
    if(!body.codes){ alert('请输入股票代码'); btn.disabled=false; btn.textContent=oldTxt; return }
  } else {
    body.max_codes = Math.min(parseInt(document.getElementById('vibeMaxCodes').value) || 100, 500)
  }
  resBox.innerHTML = '<div class="empty-hint" style="text-align:center;padding:20px 0;color:var(--t3);font-size:12px">⏳ 回测计算中（可能需要 30-90 秒）...</div>'
  const t0 = Date.now()
  const r = await api('/factors_zoo/backtest', 'POST', body)
  btn.disabled = false
  btn.textContent = oldTxt

  if(!r){ resBox.innerHTML = '<div style="color:#ef4444;padding:14px">⚠️ 网络错误或超时</div>'; return }
  if(!r.ok){ resBox.innerHTML = `<div style="color:#ef4444;padding:14px">⚠️ ${r.error||'回测失败'}</div>`; return }

  const s = r.stats || {}
  const ec = r.equity_curve || []
  // 渲染统计 + 净值曲线 + 最近交易
  const colorByVal = (v, good='#22c55e', bad='#ef4444', neutral='var(--t3)') =>
    v > 0 ? good : (v < 0 ? bad : neutral)
  const fmtPct = v => v == null ? '—' : (v*100).toFixed(2) + '%'
  const fmt = v => v == null ? '—' : (typeof v === 'number' ? v.toFixed(3) : v)
  let html = `
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:10px">
      ${[
        ['总收益',   fmtPct(s.total_return),   colorByVal(s.total_return)],
        ['年化',     fmtPct(s.annual_return),  colorByVal(s.annual_return)],
        ['最大回撤', fmtPct(s.max_drawdown),   '#ef4444'],
        ['夏普',     fmt(s.sharpe),            colorByVal(s.sharpe)],
        ['胜率',     fmtPct(s.win_rate),       colorByVal(s.win_rate-0.5)],
        ['交易笔数', s.trade_count,            'var(--t1)'],
        ['IC 均值',  fmt(s.ic_mean),           colorByVal(s.ic_mean)],
        ['IC IR',    fmt(s.ic_ir),             colorByVal(s.ic_ir)],
      ].map(([k,v,c]) => `
        <div style="background:var(--inp);border:1px solid var(--b1);border-radius:6px;padding:8px 10px;text-align:center">
          <div style="font-size:10px;color:var(--t3)">${k}</div>
          <div style="font-size:14px;font-weight:700;color:${c}">${v}</div>
        </div>
      `).join('')}
    </div>
  `
  // 净值曲线 SVG sparkline（自绘，避免依赖外部图库 — 现在 ECharts 已经在用，但简单 SVG 也够看）
  if(ec.length >= 2){
    const navs = ec.map(p => p.nav)
    const minN = Math.min(...navs), maxN = Math.max(...navs)
    const span = (maxN - minN) || 1
    const w = 700, h = 100, pad = 4
    const pts = ec.map((p, i) => {
      const x = pad + (w - 2*pad) * i / (ec.length - 1)
      const y = pad + (h - 2*pad) * (1 - (p.nav - minN) / span)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    }).join(' ')
    const lineColor = navs[navs.length-1] > navs[0] ? '#22c55e' : '#ef4444'
    html += `
      <div style="background:var(--inp);border:1px solid var(--b1);border-radius:6px;padding:8px;margin-bottom:10px">
        <div style="font-size:10px;color:var(--t3);margin-bottom:4px">净值曲线（${ec[0].date} → ${ec[ec.length-1].date}, 起始 1.00 → ${navs[navs.length-1].toFixed(4)}）</div>
        <svg width="100%" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
          <polyline points="${pts}" fill="none" stroke="${lineColor}" stroke-width="1.5"/>
          <line x1="0" y1="${h-pad}" x2="${w}" y2="${h-pad}" stroke="var(--b1)" stroke-width="0.5"/>
        </svg>
      </div>
    `
  }
  // 最近 5 笔交易
  const tr = (r.trades || []).slice(-5).reverse()
  if(tr.length){
    html += `<div style="font-size:11px;color:var(--t3);margin-bottom:4px">最近 ${tr.length} 笔交易</div>`
    html += '<table style="width:100%;border-collapse:collapse;font-size:11px">'
    html += '<thead><tr style="color:var(--t3);background:var(--inp)"><th style="padding:4px 6px;text-align:left">建仓</th><th style="padding:4px 6px;text-align:left">平仓</th><th style="padding:4px 6px;text-align:right">收益</th><th style="padding:4px 6px;text-align:left">持仓</th></tr></thead><tbody>'
    tr.forEach(t => {
      const pcol = t.pnl_pct > 0 ? '#22c55e' : (t.pnl_pct < 0 ? '#ef4444' : 'var(--t3)')
      html += `<tr style="border-bottom:1px solid var(--b1)">
        <td style="padding:4px 6px;font-family:monospace">${t.open_date}</td>
        <td style="padding:4px 6px;font-family:monospace">${t.close_date}</td>
        <td style="padding:4px 6px;text-align:right;color:${pcol};font-family:monospace">${(t.pnl_pct >= 0 ? '+' : '')+t.pnl_pct.toFixed(2)}%</td>
        <td style="padding:4px 6px;color:var(--t2);font-size:10px">${(t.codes||[]).slice(0,5).join(',')}${t.codes&&t.codes.length>5?'...':''}</td>
      </tr>`
    })
    html += '</tbody></table>'
  }
  html += `<div style="font-size:10px;color:var(--t3);margin-top:8px">回测配置: top_k=${r.config.top_k} · rebalance=${r.config.rebalance_days}d · hold=${r.config.hold_days}d · 共扫描 ${s.scanned}/${r.config.n_codes} 只 · 用时 ${((Date.now()-t0)/1000).toFixed(1)}s</div>`
  resBox.innerHTML = html
  document.getElementById('vibeAddPool').hidden = true  // 回测不支持加入选股池
}

// ── Vibe 激活到自动交易 ──
function openVibeActivateModal(){
  const factorId = document.getElementById('vibeFactorSel').value
  if(!factorId){ alert('请先选择因子'); return }
  document.getElementById('vibeActFactorIdLabel').textContent = factorId
  // 实时显示当前实盘开关状态
  document.getElementById('vactModeWarn').style.display = _tradeMode === 'live' ? '' : 'none'
  document.getElementById('vibeActivateModal').classList.add('open')
}

function closeVibeActivate(ev){
  if(ev && ev.target !== document.getElementById('vibeActivateModal')) return
  document.getElementById('vibeActivateModal').classList.remove('open')
}

async function confirmVibeActivate(){
  const factorId = document.getElementById('vibeActFactorIdLabel').textContent
  const intervalSec = parseInt(document.getElementById('vactIntervalSec').value) || 120
  const body = {
    factor_id: factorId,
    mode: 'market_top',
    max_codes: parseInt(document.getElementById('vactMaxCodes').value) || 500,
    daily_limit: parseInt(document.getElementById('vactDailyLimit').value) || 3,
    position_pct: (parseFloat(document.getElementById('vactPositionPct').value) || 20) / 100,
    score_percentile: parseFloat(document.getElementById('vactScorePct').value) || 0.8,
    stop_loss_pct: (parseFloat(document.getElementById('vactStopLoss').value) || -3) / 100,
    interval_sec: intervalSec,
  }
  const r = await api('/vibe_activations/activate', 'POST', body)
  if(r?.ok){
    closeVibeActivate()
    const mins = intervalSec >= 60 ? `${Math.floor(intervalSec/60)} 分钟` : `${intervalSec} 秒`
    alert(`⚡ 因子 ${factorId} 已激活！\n\n下次扫描会在 ${mins} 内自动触发（仅交易时段）。\n顶部开关 = ${_tradeMode==='live'?'实盘':'模拟'} 决定下单路径。`)
    renderVibeActivationCard()
    _markMonitorRunning('Alpha监控中')
    await fetchSt(true)
  } else {
    alert('激活失败: ' + (r?.error || '未知错误'))
  }
}

async function renderVibeActivationCard(){
  const box = document.getElementById('vibeCurActivation')
  if(!box) return
  const r = await api('/vibe_activations/me')
  if(!r?.ok || !r.activation){
    box.style.display = 'none'
    return
  }
  const a = r.activation
  if(!a.enabled){
    box.style.display = 'none'
    return
  }
  const halted = a.halted ? `<span style="color:#ef4444;font-weight:700">🛑 已熔断: ${a.halt_reason||''}</span>` : '<span style="color:#22c55e">✓ 运行中</span>'
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <b>⚡ 当前已激活: ${a.factor_id}</b>
      <button class="btn btn-red btn-xs" data-ui-click="deactivateVibe()">停用</button>
    </div>
    <div style="color:var(--t2);line-height:1.6">
      状态: ${halted} · 今日 ${a.daily_count}/${a.daily_limit} 笔 · 浮盈 ${(a.daily_pnl_pct*100).toFixed(2)}%<br>
      仓位 ${(a.position_pct*100).toFixed(0)}% · 分数阈值 top ${((1-a.score_percentile)*100).toFixed(0)}% · 止损 ${(a.stop_loss_pct*100).toFixed(1)}% · 池 ${a.max_codes} 只
    </div>
  `
  box.style.display = ''
}

async function deactivateVibe(){
  if(!confirm('停用当前激活的 Alpha 因子？\n已开仓位不影响，仅停止后续自动扫描。')) return
  const r = await api('/vibe_activations/deactivate', 'POST', {})
  if(r?.ok){
    document.getElementById('vibeCurActivation').style.display = 'none'
  }
}

// openVibeFactors 末尾追加：自动拉激活状态
const _originalOpenVibeFactors = openVibeFactors
openVibeFactors = async function(){
  await _originalOpenVibeFactors.apply(this, arguments)
  renderVibeActivationCard()
}

// 把最后一次 Vibe 因子计算结果注入右侧选股结果区
let _vibeLastResult = null
function vibeAddToScreenPool(){
  if(!_vibeLastResult){ alert('请先计算因子分数'); return }
  const topN = Math.max(1, parseInt(document.getElementById('vibeAddTopN').value) || 20)
  const validRanked = _vibeLastResult.ranked.filter(x => x.score !== null && !isNaN(x.score))
  const slice = validRanked.slice(0, topN)
  if(slice.length === 0){ alert('没有有效因子分数可加入'); return }

  const fid = _vibeLastResult.factor_id
  // 转 scResults 格式（注意：score 需要从 -1~+1 缩放到 0~100 给 UI 评分用）
  const minScore = Math.min(...slice.map(x => x.score))
  const maxScore = Math.max(...slice.map(x => x.score))
  const span = (maxScore - minScore) || 1
  scResults = slice.map((x, i) => ({
    code:   x.code,
    name:   x.name || x.code,
    price:  x.price || 0,
    change: x.change || 0,
    // 归一化到 0~100 给徽章用
    score:  Math.round((x.score - minScore) / span * 100),
    reason: `📚 ${fid} #${i+1} (raw=${x.score.toFixed(4)})`,
  }))
  // 让"加入持仓队列"等按钮可见
  const _btn = document.getElementById('addAllBtn'); if(_btn) _btn.style.display = scResults.length ? '' : 'none'
  const _ob  = document.getElementById('optBtn');   if(_ob)  _ob.style.display  = scResults.length >= 2 ? '' : 'none'

  // 渲染
  document.getElementById('scInfo').textContent =
    `📚 Alpha 因子[${fid}] · 前 ${slice.length} 名（共 ${_vibeLastResult.ranked.length} 只参与排名）`
  _renderScResults()
  // 切到选股 tab + 关闭 Modal
  switchRTab('screen', document.querySelectorAll('.rtab')[1])
  closeVibeFactors()
}

async function loadStrat(fn){
  curStrat=fn; _curMode='strat'
  const d=await api('/strategies/'+fn); if(!d) return
  editor.setValue(d.code||'')
  // ★ 内置策略受保护：非 admin 拿到的是占位 + locked，编辑器置只读并提示
  editor.setOption('readOnly', d.locked ? 'nocursor' : false)
  document.getElementById('sfn').value=fn
  setEdSt(d.locked ? '🔒 内置策略 · 源码受保护（可激活运行，不可查看/编辑）' : '已加载: '+fn)
  document.querySelectorAll('#stratList .si').forEach(el => {
    const action = el.getAttribute('data-ui-click') || ''
    el.classList.toggle('active', action.includes(`'${fn}'`))
  })
  scResults=[]
  document.getElementById('scBody').innerHTML='<div class="empty">点击「选股」开始</div>'
  document.getElementById('scInfo').textContent='点击「选股」开始'
  document.getElementById('addAllBtn').style.display='none'
}
function newStrat(){
  curStrat=null; _curMode='strat'; editor.setOption('readOnly', false); document.getElementById('sfn').value='my_strategy'
  editor.setValue(`# META: {"name": "自定义策略", "desc": "描述", "author": "用户", "builtin": false}

def scan(codes: list, get_kline, get_quotes) -> list:
    quotes = get_quotes(codes)
    results = []
    for code in codes:
        df = get_kline(code, 60)
        if df.empty or len(df) < 22: continue
        close  = df['close']; volume = df['volume']
        ma5    = close.rolling(5).mean(); ma20 = close.rolling(20).mean()
        if not (ma5.iloc[-1] > ma20.iloc[-1] and ma5.iloc[-2] <= ma20.iloc[-2]): continue
        avg_vol   = volume.iloc[-21:-1].mean()
        vol_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 0
        if vol_ratio < 1.5: continue
        row   = quotes[quotes['代码'] == code]
        price = float(row.iloc[0]['最新价']) if not row.empty else float(close.iloc[-1])
        name  = row.iloc[0]['名称'] if not row.empty else ''
        chg   = float(row.iloc[0]['涨跌幅']) if not row.empty else 0
        results.append({"code":code,"name":name,"price":round(price,2),
                        "change":round(chg,2),"score":round(vol_ratio*10,1),
                        "reason":f"MA金叉 量比{vol_ratio:.1f}x"})
    return sorted(results, key=lambda x: x['score'], reverse=True)
`)
  setEdSt('新策略（未保存）'); loadStratList()
}
async function saveStrat(){
  const fn=document.getElementById('sfn').value.trim(), code=editor.getValue()
  if(!fn){alert('请填写名称');return}
  if(_curMode==='factor'){
    // 保存到因子库
    const r=await api('/factors/save','POST',{filename:fn,code})
    if(r?.ok){curFactor=r.file;setEdSt('因子已保存: '+r.file);loadFactorList()}
    else alert('保存失败: '+(r?.msg||''))
  } else {
    // 保存到策略库
    const r=await api('/strategies/save','POST',{filename:fn,code})
    if(r?.ok){curStrat=r.file;setEdSt('已保存: '+r.file);loadStratList()}
    else alert('保存失败: '+(r?.msg||''))
  }
}
async function delStrat(){
  if(_curMode==='factor'){
    if(!curFactor){alert('请先选择因子');return}
    await delFactor(curFactor)
    return
  }
  if(!curStrat){alert('请先选择策略');return}
  if(!confirm(`确认删除 "${curStrat}"？`)) return
  const r=await api('/strategies/delete','POST',{filename:curStrat})
  if(r?.ok){curStrat=null;editor.setValue('');loadStratList();document.getElementById('sfn').value=''}
  else alert(r?.msg||'内置策略不可删除')
}
async function activateStrat(){
  if(_curMode==='factor'){alert('因子库不支持激活自动交易。\n请在策略库中选择策略激活。');return}
  if(!curStrat){alert('请先保存或选择策略');return}
  const r = await api('/strategies/activate','POST',{filename:curStrat})
  if(!r?.ok){
    alert('激活失败：'+(r?.error||'请确认已登录，或查看后端日志'))
    return
  }
  _markMonitorRunning()
  await fetchSt(true)
  const b=document.getElementById('actBtn'); b.textContent='✅ 已激活'
  setTimeout(()=>b.textContent='⚡ 激活',2000)
}

// ── 选股 ──
async function runScreen(maxCodes = 0){
  if(_screenRunPromise) return _screenRunPromise
  document.querySelectorAll('[data-ui-click^="runScreen("]').forEach(button=>{
    button.disabled = true
    button.setAttribute('aria-busy','true')
  })
  _screenRunPromise = _runScreen(maxCodes)
  try{
    return await _screenRunPromise
  }finally{
    _screenRunPromise = null
    document.querySelectorAll('[data-ui-click^="runScreen("]').forEach(button=>{
      button.disabled = false
      button.removeAttribute('aria-busy')
    })
  }
}

async function _runScreen(maxCodes = 0){
  // 因子模式：转发到因子选股
  if(_curMode==='factor'){
    if(!curFactor){alert('请先选择或保存因子');return}
    setMobileStrategyPane('result')
    switchRTab('screen',document.querySelectorAll('.rtab')[1])
    await _runFactorScreen(curFactor)
    return
  }
  if(!curStrat){alert('请先保存或选择策略');return}
  setMobileStrategyPane('result')
  switchRTab('screen',document.querySelectorAll('.rtab')[1])
  const scInfo = document.getElementById('scInfo')
  const scProg = document.getElementById('scProgWrap')
  const scBar  = document.getElementById('scProgBar')
  const scanScope = maxCodes > 0 ? `活跃前 ${maxCodes} 只` : '全市场'
  scInfo.innerHTML=`<span class="spin">⟳</span> ${scanScope}扫描任务启动中...`
  document.getElementById('scBody').innerHTML=`<div class="empty"><span class="spin" style="font-size:22px">⟳</span><br><span style="font-size:12px;color:var(--t4)">正在获取${scanScope}行情并筛选...</span></div>`
  scProg.style.display='block'
  scBar.className='scan-prog-bar pulse'; scBar.style.width=''

  // 异步模式：先获取 task_id，再轮询进度和结果
  const initR = await api('/strategies/run_async','POST',{filename:curStrat, max_codes: maxCodes})
  if(!initR?.ok || !initR.task_id){
    scProg.style.display='none'
    scInfo.textContent='❌ 选股失败: '+(initR?.error||initR?.msg||'未知错误')
    document.getElementById('scBody').innerHTML=`<div style="padding:14px"><div style="background:#2d1a1e;border:1px solid var(--red);border-radius:8px;padding:12px;color:var(--rl);font-size:13px">${initR?.error||initR?.msg||'选股失败'}</div></div>`
    return
  }
  const taskId = initR.task_id
  // 轮询进度
  let r = null
  let progressFailures = 0
  while(true){
    await new Promise(res => setTimeout(res, 1500))
    const prog = await api('/strategies/progress/'+taskId)
    if(!prog?.ok){
      progressFailures += 1
      if(progressFailures < 3){
        scInfo.innerHTML=`<span class="spin">⟳</span> 进度连接波动，正在重试 ${progressFailures}/3...`
        continue
      }
      scProg.style.display='none'
      scInfo.textContent='❌ 无法读取扫描进度: '+(prog?.error||prog?.msg||'网络异常')
      document.getElementById('scBody').innerHTML=`<div style="padding:14px"><div style="background:#2d1a1e;border:1px solid var(--red);border-radius:8px;padding:12px;color:var(--rl);font-size:13px">${prog?.error||prog?.msg||'无法读取扫描进度，请稍后重试'}</div></div>`
      return
    }
    progressFailures = 0
    if(prog.status === 'error'){
      scProg.style.display='none'
      scInfo.textContent='❌ 选股失败: '+(prog.error||'未知错误')
      document.getElementById('scBody').innerHTML=`<div style="padding:14px"><div style="background:#2d1a1e;border:1px solid var(--red);border-radius:8px;padding:12px;color:var(--rl);font-size:13px">${prog.error||'选股失败'}</div></div>`
      return
    }
    // 更新进度条
    if(prog.scan_total > 0){
      const pct = Math.min(100, Math.round(prog.scan_current/prog.scan_total*100))
      scBar.className = 'scan-prog-bar'
      scBar.style.width = pct+'%'
      const codeTxt = prog.scan_code ? ` <span style="opacity:.6">[${prog.scan_code}]</span>` : ''
      const stage = prog.scan_stage || '扫描'
      scInfo.innerHTML = `<span class="spin">⟳</span> ${stage} ${prog.scan_current}/${prog.scan_total} (${pct}%)${codeTxt}...`
    }
    if(prog.status === 'done'){
      r = await api('/strategies/result/'+taskId)
      break
    }
  }
  scProg.style.display='none'
  scBar.className='scan-prog-bar pulse'; scBar.style.width=''

  if(!r?.ok){
    scInfo.textContent='❌ 选股失败: '+(r?.error||r?.msg||'未知错误')
    document.getElementById('scBody').innerHTML=`<div style="padding:14px"><div style="background:#2d1a1e;border:1px solid var(--red);border-radius:8px;padding:12px;color:var(--rl);font-size:13px">${r?.error||r?.msg||'选股失败'}</div></div>`
    return
  }

  scResults=r?.results||[]
  const scanned = r?.scanned || scResults.length
  scInfo.textContent=`扫描 ${scanned} 只，命中 ${scResults.length} 只`
  document.getElementById('addAllBtn').style.display=scResults.length?'':'none'
  const _ob2=document.getElementById('optBtn'); if(_ob2) _ob2.style.display=scResults.length>=2?'':'none'
  const el=document.getElementById('scBody')
  if(!scResults.length){el.innerHTML='<div class="empty">未发现符合条件的标的（全市场已扫描）</div>';return}
  _renderScResults()
}
function _renderScResults(){
  const el=document.getElementById('scBody')
  el.innerHTML=scResults.map(r=>{
    const chg=parseFloat(r.change||0)
    const isQueued=_queuedCodes.has(r.code)
    const btnPayload = {code:r.code,name:r.name,price:r.price,reason:r.reason||""}
    const queueBtn = isQueued
      ? '<button class="btn btn-queued btn-xs" disabled>✓ 已入队</button>'
      : `<button class="btn btn-green btn-xs" ${_coreActionAttrs('add-one-q',btnPayload)}>+队</button>`
    const buyBtn = `<button class="btn btn-orange btn-xs" ${_coreActionAttrs('manual-buy',btnPayload)} title="立即按当前价模拟盘买入">🛒 买入</button>`
    const btn = queueBtn + ' ' + buyBtn
    // 六维评分徽章（当因子/选股有返回score时显示信号）
    let sigBadge = ''
    if(r.signal && r.signal_color){
      sigBadge = `<span class="sig-badge" style="background:${r.signal_color}">${r.signal}</span>`
    } else if(typeof r.score === 'number' && r.score > 0){
      const scoreColor = r.score>=75?'#238636':r.score>=60?'#1f6feb':r.score>=45?'#e3b341':r.score>=30?'#bd561d':'#da3633'
      sigBadge = `<span class="sig-badge" style="background:${scoreColor}">${r.score}分</span>`
    }
    return `<div class="sc-row" style="cursor:pointer" ${_coreActionAttrs('show-kline',{code:r.code,name:r.name||r.code})}>
      <span class="sc-stock-code" style="font-weight:700;width:52px">${r.code}</span>
      <span class="sc-stock-name" style="color:var(--t2);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.name||r.code}</span>
      <span class="sc-stock-price" style="width:56px;text-align:right;font-weight:600">¥${(+r.price||0).toFixed(2)}</span>
      <span class="sc-stock-change ${chg>=0?'rise':'fall'}" style="width:50px;text-align:right">${(chg>=0?'+':'')+chg.toFixed(2)}%</span>
      <span class="sc-stock-score">${sigBadge||`<span style="width:30px;text-align:center;color:var(--ol);font-weight:700">${r.score||0}</span>`}</span>
      <span class="sc-stock-reason" style="flex:2;color:var(--t3);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.reason||''}</span>
      <span class="sc-stock-actions">${btn}</span>
    </div>`
  }).join('')
}
async function addOneQ(r){
  const res = await api('/queue/add','POST',{code:r.code, price:r.price, name:r.name, reason:r.reason||''})
  if(res?.ok){
    _queuedCodes.add(r.code)
    renderPool(_lastPool)        // 立即刷新监控池按钮
    if(scResults.length) _renderScResults()  // 刷新策略选股按钮
    fetchSt(); setTimeout(fetchPositions,800)
  } else if(res?.msg) alert(res.msg)
}

// ── 监控中心 / 选股结果 行内"🛒 立即买入" — 跳过队列直接调模拟盘下单
//    shares=0 让后端按 position_ratio 自动计算手数
async function manualBuy(r){
  const live = (typeof _tradeMode !== 'undefined' && _tradeMode === 'live')
  const modeTxt = live ? '🔴 实盘买入（真实资金、真实成交）' : '🛡️ 模拟买入（不动真钱）'
  if(!confirm(`${modeTxt}\n\n${r.code} ${r.name||''} @ ¥${(+r.price||0).toFixed(2)}\n确认下单？`)) return
  const res = await api('/paper/buy','POST',{
    code:r.code, name:r.name||r.code, price:r.price, shares:0
  })
  if(res?.ok){
    // 提示成功，刷新持仓 + 状态条
    toast(res.msg || `✅ 已${live?'实盘':'模拟'}买入 ${r.code}`, 'success')
    fetchSt()
    setTimeout(fetchPositions, 500)
    if(typeof loadPaperPage === 'function') setTimeout(loadPaperPage, 800)
  } else {
    toast('❌ ' + (res?.msg || (live?'实盘':'模拟')+'买入失败'), 'error')
  }
}
let _lastPool = []
async function addAllQ(){
  const btn = document.getElementById('addAllBtn')
  if(btn){ btn.disabled=true; btn.textContent='入队中...' }
  for(const r of scResults){
    const res = await api('/queue/add','POST',{code:r.code,price:r.price,name:r.name,reason:r.reason||''})
    if(res?.ok) _queuedCodes.add(r.code)
  }
  _renderScResults()   // 刷新选股列表按钮为"✓ 已入队"
  renderPool(_lastPool) // 刷新监控池按钮
  if(btn){ btn.disabled=false; btn.textContent='全部入队' }
  fetchSt()
  setTimeout(fetchPositions, 800)
}

// ── 数据管理 ──
async function loadDbList(){
  const list=await api('/data/stocks')||[]
  document.getElementById('dbCount').textContent=`${list.length} 只`
  const el=document.getElementById('dbList')
  if(!list.length){el.innerHTML='<div class="empty">暂无本地数据</div>';return}
  el.innerHTML=list.map(s=>{
    const nm=(s.name||s.code).replace(/'/g,"\\'")
    return `<div class="sk-row" style="cursor:pointer" data-ui-click="showKLine('${s.code}','${nm}')">
    <span class="sk-code">${s.code}</span>
    <span class="sk-name" title="${s.name||s.code}">${s.name||'—'}</span>
    <span class="sk-range" style="font-size:10px">${s.start||''}~${s.end||''}</span>
    <span class="sk-rows">${s.rows||0}条</span>
    <span style="font-size:10px;color:var(--t4);margin-right:4px">📈</span>
    <button class="btn btn-red btn-xs" data-ui-click="event.stopPropagation();delStock('${s.code}')">删</button>
  </div>`
  }).join('')
}
async function startDownload(){
  const codes=document.getElementById('dlCodes').value.trim()
  const start=document.getElementById('dlStart').value
  if(!codes){alert('请输入股票代码');return}
  const r=await api('/data/download','POST',{codes,start_date:start})
  if(!r?.ok){alert(r?.msg||'启动失败');return}
  dlTaskId=r.task_id
  document.getElementById('dlBtn').disabled=true
  document.getElementById('dlInfo').textContent=`任务 ${r.task_id} — 准备中...`
  document.getElementById('dlBar').style.width='0%'
  dlTimer=setInterval(checkDl,1500)
}
async function checkDl(){
  if(!dlTaskId) return
  const p=await api('/data/progress/'+dlTaskId)
  if(!p||p.status==='not_found') return
  const pct=p.total>0?Math.round(p.done/p.total*100):0
  document.getElementById('dlBar').style.width=pct+'%'
  document.getElementById('dlInfo').textContent=
    p.status==='done'
      ?`✅ 完成！${p.done} 只${p.errors?.length?' | ❌ '+p.errors.length+'只失败':''}`
      :`下载中 ${p.done}/${p.total}${p.current?' — '+p.current:''}`
  if(p.status==='done'){
    clearInterval(dlTimer);dlTimer=null
    document.getElementById('dlBtn').disabled=false
    loadDbList()
  }
}
async function delStock(code){
  if(!confirm(`删除 ${code} 的本地数据？`)) return
  await api('/data/delete','POST',{code}); loadDbList()
}
async function refreshNames(){
  const r=await api('/data/refresh_names','POST')
  if(r?.ok) alert('已在后台刷新股票名称，约30秒后重新刷新列表')
}

// ── 回测 ──
function goBacktest(){ switchRTab('bt',document.querySelectorAll('.rtab')[2]) }

// ══════════════════════════════════════════════
// AI 多智能体分析（参考 TradingAgents-CN 架构）
// ══════════════════════════════════════════════
let aiCfg = {}

async function initAI(){
  if(!(_currentUser && _currentUser.is_admin)){ aiCfg = {}; return }   // AI接口配置仅管理员可见(管理后台可改)
  aiCfg = await api('/ai/config') || {}
  renderProvGrid()
  document.getElementById('aiBaseUrl').value = aiCfg.base_url || ''
  document.getElementById('aiModel').value   = aiCfg.model    || ''
  document.getElementById('aiEnabled').checked = !!aiCfg.enabled
  document.getElementById('aiEnabledLbl').textContent = aiCfg.enabled ? '已启用 ✅' : '已禁用'
  const masked = aiCfg.api_key_masked || ''
  document.getElementById('aiKeyMasked').textContent  = masked || '未配置'
  // 用脱敏 key 更新 placeholder，让用户知道 key 已存在（输入框为空 ≠ key 为空）
  const keyInp = document.getElementById('aiApiKey')
  if(masked && masked !== '****'){
    keyInp.placeholder = `已保存: ${masked}  （留空=保持不变）`
  } else {
    keyInp.placeholder = 'sk-...  （填入新 Key 后保存）'
  }
}

function renderProvGrid(){
  const presets = aiCfg.presets || {}
  const cur     = aiCfg.provider || 'deepseek'
  const labels  = {deepseek:'DeepSeek',openai:'OpenAI',anthropic:'Claude',nvidia:'NVIDIA',qwen:'通义千问',zhipu:'智谱GLM',xiaomimimo:'小米MiMo',custom:'自定义'}
  const el = document.getElementById('provGrid')
  el.innerHTML = Object.keys(labels).map(k=>`
    <div class="prov-btn ${k===cur?'active':''}" data-ui-click="selectProv('${k}')">
      ${labels[k]}
    </div>`).join('')
}

function selectProv(p){
  aiCfg.provider = p
  const preset = (aiCfg.presets||{})[p] || {}
  if(preset.base_url) document.getElementById('aiBaseUrl').value = preset.base_url
  if(preset.model)    document.getElementById('aiModel').value   = preset.model
  renderProvGrid()
}

function toggleAiEnabled(cb){
  document.getElementById('aiEnabledLbl').textContent = cb.checked ? '已启用 ✅' : '已禁用'
}

async function saveAiConfig(){
  const payload = {
    provider: aiCfg.provider || 'deepseek',
    base_url: document.getElementById('aiBaseUrl').value.trim(),
    model:    document.getElementById('aiModel').value.trim(),
    enabled:  document.getElementById('aiEnabled').checked,
    api_key:  document.getElementById('aiApiKey').value.trim(),
  }
  const r = await api('/ai/config','POST',payload)
  const msg = document.getElementById('aiSetMsg')
  if(r?.ok){
    msg.style.color='var(--gl)'; msg.textContent='✅ 配置已保存'
    // 清空明文输入框，重新读取已保存的脱敏 Key 显示到 placeholder
    document.getElementById('aiApiKey').value = ''
    await initAI()   // 刷新显示（含 当前Key: xxxx****xxxx）
    // 把 placeholder 改成已保存的脱敏 key，让用户知道 key 没有丢失
    const masked = document.getElementById('aiKeyMasked').textContent
    if(masked && masked !== '未配置')
      document.getElementById('aiApiKey').placeholder = `已保存: ${masked}  （留空=保持不变）`
  } else {
    msg.style.color='var(--rl)'; msg.textContent='❌ 保存失败'
  }
  setTimeout(()=>msg.textContent='',3000)
}

async function testAiConn(){
  const msg     = document.getElementById('aiSetMsg')
  const keyInp  = document.getElementById('aiApiKey')
  const urlInp  = document.getElementById('aiBaseUrl')
  const modInp  = document.getElementById('aiModel')

  // 如果输入框里有新填写的 key，提示先保存再测试
  if(keyInp.value.trim()){
    msg.style.color='var(--yl)'
    msg.textContent='⚠ 检测到新 API Key，正在自动保存后测试...'
    await saveAiConfig()   // 先保存，再继续测试
  }

  msg.style.color='var(--bl)'; msg.textContent='🔗 正在向AI发送测试请求...'

  // 把当前输入框中的 URL / 模型传给后端（key 已保存，不需要传）
  const payload = {
    base_url: urlInp.value.trim() || undefined,
    model:    modInp.value.trim() || undefined,
  }
  const r = await api('/ai/test','POST', payload)
  if(r === null){
    msg.style.color='var(--rl)'; msg.textContent='❌ 后端无法访问（请重启平台后再试）'
  } else if(r.ok){
    msg.style.color='var(--gl)'
    msg.textContent=`✅ 连接成功 — 模型: ${r.model||'未知'} | 回复: "${r.reply||'OK'}"`
  } else {
    msg.style.color='var(--rl)'
    msg.textContent=`❌ ${r.error||'AI接口调用失败'}`
  }
  setTimeout(()=>msg.textContent='',8000)
}

// ══════════════════════════════════════════════
// 券商接口配置
// ══════════════════════════════════════════════
let _brokerCfg = { broker_type: 'paper', config: {} }

const BROKER_LABELS = {
  paper:      { icon: '🎯', name: '模拟盘',      desc: '内置虚拟账户，无需配置' },
  easytrader: { icon: '🖥',  name: 'EasyTrader', desc: '同花顺/通达信客户端自动化' },
  longbridge: { icon: '🌏', name: '长桥证券',    desc: '长桥 OpenAPI（REST）' },
  custom:     { icon: '🔧', name: '自定义HTTP',  desc: '对接私有交易系统' },
}

const BROKER_FIELDS = {
  paper: [],
  easytrader: [
    { id:'et_broker',  label:'客户端类型', type:'select',
      options:[{v:'ths',l:'同花顺(THS)'},{v:'tdx',l:'通达信(TDX)'},{v:'gj',l:'国金证券'},{v:'yh',l:'银河证券'},{v:'ht',l:'华泰证券'}] },
    { id:'et_account', label:'资金账号', type:'text',   ph:'请输入资金账号' },
    { id:'et_pass',    label:'交易密码', type:'password', ph:'请输入交易密码' },
  ],
  longbridge: [
    { id:'lb_key',    label:'App Key',      type:'text',     ph:'从长桥开发者中心获取' },
    { id:'lb_secret', label:'App Secret',   type:'password', ph:'从长桥开发者中心获取' },
    { id:'lb_token',  label:'Access Token', type:'password', ph:'从长桥开发者中心获取' },
  ],
  custom: [
    { id:'cu_url',   label:'接口 Base URL', type:'text',     ph:'http://127.0.0.1:8080' },
    { id:'cu_token', label:'Bearer Token',  type:'password', ph:'留空则不鉴权' },
  ],
}

async function initBrokerConfig(){
  if(!_currentUser) return
  const r = await api('/user/broker_config')
  if(!r?.ok) return
  _brokerCfg = { broker_type: r.broker_type || 'paper', config: r.config || {} }
  renderBrokerTypeGrid()
  renderBrokerFields(_brokerCfg.broker_type)
}

function renderBrokerTypeGrid(){
  const cur = _brokerCfg.broker_type
  const box = document.getElementById('brokerTypeGrid')
  box.innerHTML = Object.entries(BROKER_LABELS).map(([k,v])=>`
    <div data-ui-click="selectBrokerType('${k}')" style="
      padding:10px 12px;border-radius:8px;cursor:pointer;border:1px solid ${k===cur?'var(--blue)':'var(--b1)'};
      background:${k===cur?'rgba(31,111,235,.12)':'var(--card)'};transition:.15s">
      <div style="font-size:15px;margin-bottom:3px">${v.icon} <b style="font-size:13px;color:${k===cur?'var(--bl)':'var(--t1)'}">${v.name}</b></div>
      <div style="font-size:10px;color:var(--t3)">${v.desc}</div>
    </div>`).join('')
}

function selectBrokerType(t){
  _brokerCfg.broker_type = t
  renderBrokerTypeGrid()
  renderBrokerFields(t)
}

function renderBrokerFields(t){
  const box    = document.getElementById('brokerFieldsBox')
  const fields = BROKER_FIELDS[t] || []
  const cfg    = _brokerCfg.config || {}

  if(t === 'paper'){
    box.innerHTML = `<div style="padding:10px 12px;background:var(--card);border-radius:8px;font-size:12px;color:var(--gl);margin-bottom:12px">
      ✅ 使用内置模拟盘，所有交易仅在本平台虚拟账户中执行，不会真实下单。
    </div>`
    return
  }

  const rows = fields.map(f => {
    const val = cfg[f.id.split('_').slice(1).join('_')] || cfg[f.id] || ''
    if(f.type === 'select'){
      const opts = f.options.map(o=>`<option value="${o.v}" ${val===o.v?'selected':''}>${o.l}</option>`).join('')
      return `<div class="fc" style="margin-bottom:10px">
        <label style="font-size:11px;color:var(--t3)">${f.label}</label>
        <select id="${f.id}" class="set-fi" style="padding:6px 8px">${opts}</select></div>`
    }
    return `<div class="fc" style="margin-bottom:10px">
      <label style="font-size:11px;color:var(--t3)">${f.label}</label>
      <input id="${f.id}" class="set-fi" type="${f.type}" placeholder="${f.ph}" value="${f.type==='password'?'':val}"></div>`
  }).join('')

  box.innerHTML = `<div style="margin-bottom:12px">${rows}</div>`
}

async function saveBrokerConfig(){
  const msg = document.getElementById('brokerSetMsg')
  const t   = _brokerCfg.broker_type
  let cfg   = {}

  if(t === 'easytrader'){
    cfg = {
      broker:   document.getElementById('et_broker')?.value  || 'ths',
      account:  document.getElementById('et_account')?.value.trim() || '',
      password: document.getElementById('et_pass')?.value    || '',
    }
  } else if(t === 'longbridge'){
    cfg = {
      app_key:      document.getElementById('lb_key')?.value.trim()    || '',
      app_secret:   document.getElementById('lb_secret')?.value        || '',
      access_token: document.getElementById('lb_token')?.value         || '',
    }
  } else if(t === 'custom'){
    cfg = {
      base_url: document.getElementById('cu_url')?.value.trim() || '',
      token:    document.getElementById('cu_token')?.value      || '',
    }
  }

  const r = await api('/user/broker_config','POST',{ broker_type: t, config: cfg })
  if(r?.ok){
    msg.style.color = 'var(--gl)'; msg.textContent = '✅ 券商配置已保存'
    _brokerCfg.config = cfg
  } else {
    msg.style.color = 'var(--rl)'; msg.textContent = '❌ 保存失败'
  }
  setTimeout(()=>msg.textContent='', 3000)
}

function fillAiFromEditor(){
  const inp = document.getElementById('aiCode')
  const sl  = document.getElementById('aiStatusLine')

  // 优先级：① 策略选股结果 → ② 因子选股结果 → ③ 监控池 → ④ 回测代码框
  if(scResults?.length){
    inp.value = scResults[0].code
    if(sl) sl.textContent = `已填入选股结果第一只: ${scResults[0].name||scResults[0].code}`
    return
  }
  const pool = (_lastPool||[]).filter(r=>r.signal!==false)
  if(pool.length){
    inp.value = pool[0].code
    if(sl) sl.textContent = `已填入监控池: ${pool[0].name||pool[0].code}`
    return
  }
  const bt = document.getElementById('btCodes')?.value.trim().split(/[,，]/)[0].trim()
  if(bt){
    inp.value = bt
    if(sl) sl.textContent = `已填入回测代码: ${bt}`
    return
  }
  // 都没有时给出引导提示（不用alert，显示在状态栏）
  if(sl){
    sl.style.color='var(--yl)'
    sl.textContent = '提示：请先运行「选股」获取结果，或直接在上方输入框手动填入股票代码（如 000001）'
    setTimeout(()=>{ sl.style.color=''; sl.textContent='选择股票后点击分析（需配置AI设置）' }, 4000)
  }
}

// ── 大盘市场环境（含实时指数行情）──
let _mktRefreshTimer = null
let _mktInflight = false
let _mktFollowupTimer = null
let _mktIndicesTimer = null
let _mktIndicesInflight = false
function _renderMarketIndices(indices){
  const shortName = {'上证指数':'沪','深证成指':'深','创业板指':'创'}
  const idxHtml = Object.entries(indices||{}).map(([nm,info])=>{
    const chg = info.change || 0
    const clr = chg>0?'var(--rl)':chg<0?'var(--gl)':'var(--t3)'
    const arr = chg>0?'↑':chg<0?'↓':'→'
    return `<span style="color:${clr};white-space:nowrap">${shortName[nm]||nm} <b>${info.price||'—'}</b> ${arr}${Math.abs(chg).toFixed(2)}%</span>`
  }).join('<span style="color:var(--b1)">│</span>') || '<span style="color:var(--t4)">指数数据获取中...</span>'
  const idxEl = document.getElementById('mktIndices')
  if(idxEl) idxEl.innerHTML=idxHtml
  const idxElM = document.getElementById('mktIndicesMon')
  if(idxElM) idxElM.innerHTML=idxHtml
}
async function loadMarketIndices(){
  if(document.hidden || !_isMarketContextPage() || _mktIndicesInflight) return
  _mktIndicesInflight = true
  try{
    const r = await api('/market/indices')
    if(r?.ok) _renderMarketIndices(r.indices||{})
  }finally{
    _mktIndicesInflight = false
  }
}
async function loadMarketContext(force=false){
  if(_mktInflight) return
  _mktInflight = true
  // 用户点 ⟳ 时给可见反馈
  if(force){
    document.querySelectorAll('[data-ui-click*="loadMarketContext(true)"]').forEach(b=>{
      b.dataset._oldText = b.innerHTML
      b.innerHTML = '刷新中...'
      b.disabled = true
    })
  }
  try{
    const r = await api('/market/context'+(force?'?refresh=1':''))
    if(!r?.ok){
      if(force) console.warn('[市场环境] 接口返回 ok=false', r)
      return
    }
    const strength = r.strength ?? 50
    const trend    = r.trend || '—'
    const trendCls = strength>=70?'mkt-bull':strength>=45?'mkt-neutral':'mkt-bear'
    const barClr   = strength>=70?'#238636':strength>=45?'#1f6feb':'#da3633'

    // ── 策略中心横幅 ─────────────────────────────
    const tEl = document.getElementById('mktTrend')
    if(tEl){ tEl.textContent=trend; tEl.className='mkt-trend '+trendCls }
    const sEl = document.getElementById('mktStrengthNum')
    if(sEl) sEl.textContent=strength
    const bEl = document.getElementById('mktBarFill')
    if(bEl){ bEl.style.width=strength+'%'; bEl.style.background=barClr }
    const aEl = document.getElementById('mktAdvice')
    if(aEl) aEl.textContent=r.advice||''

    // ── 监控中心横幅 ─────────────────────────────
    const tElM = document.getElementById('mktTrendMon')
    if(tElM){ tElM.textContent=trend; tElM.className='mkt-trend '+trendCls }
    const sElM = document.getElementById('mktStrengthNumMon')
    if(sElM) sElM.textContent=strength
    const bElM = document.getElementById('mktBarFillMon')
    if(bElM){ bElM.style.width=strength+'%'; bElM.style.background=barClr }
    const aElM = document.getElementById('mktAdviceMon')
    if(aElM) aElM.textContent=r.advice||''

    if(r.refreshing && !_mktFollowupTimer){
      _mktFollowupTimer = setTimeout(()=>{
        _mktFollowupTimer = null
        loadMarketContext(false)
      }, 1000)
    }

  }catch(e){ console.warn('市场环境加载失败:',e) }
  finally{
    if(force){
      document.querySelectorAll('[data-ui-click*="loadMarketContext(true)"]').forEach(b=>{
        if(b.dataset._oldText){ b.innerHTML = b.dataset._oldText; delete b.dataset._oldText }
        b.disabled = false
      })
    }
    _mktInflight = false
  }

  // 仅在使用市场环境的页面刷新，后台页签不穿透服务端缓存。
  if(!_mktRefreshTimer){
    _mktRefreshTimer = setInterval(()=>{
      if(!document.hidden && _isMarketContextPage()) loadMarketContext(false)
    }, 60000)
  }
  if(!_mktIndicesTimer){
    loadMarketIndices()
    _mktIndicesTimer = setInterval(loadMarketIndices, 5000)
  }
}

async function runAI(){
  const code = document.getElementById('aiCode').value.trim().toUpperCase()
  if(!code){ alert('请输入股票代码'); return }

  const btn = document.getElementById('aiBtn')
  btn.disabled = true; btn.textContent = '⟳ 分析中...'
  document.getElementById('aiStatusLine').textContent = `正在分析 ${code}...`
  document.getElementById('aiResult').innerHTML = `
    <div class="ai-ph">
      <div class="spin" style="font-size:32px;opacity:.5">⟳</div>
      <p>六维评分 + AI 多智能体分析中...</p>
      <span>技术师 → 多方研究员 → 空方研究员 → 风险师 → 决策官<br>请稍候约15-45秒</span>
    </div>`

  // 异步模式：先获取 task_id，再轮询进度和结果
  const initR = await api('/ai/analyze_async','POST',{code})
  if(!initR || !initR.ok){
    btn.disabled = false; btn.textContent = '🤖 开始分析'
    const err = initR?.error || '分析失败，请检查AI设置'
    document.getElementById('aiStatusLine').textContent = '❌ ' + err
    document.getElementById('aiResult').innerHTML = `
      <div style="padding:15px">
        <div style="background:#2d1a1e;border:1px solid var(--red);border-radius:8px;padding:13px">
          <div style="color:var(--rl);font-weight:700;margin-bottom:6px">❌ AI分析失败</div>
          <div style="color:#f0a0a0;font-size:13px">${esc(err)}</div>
          <div style="margin-top:10px;font-size:12px;color:var(--t3)">
            请前往「⚙ 设置」标签页配置 API Key 并启用 AI 功能
          </div>
        </div>
      </div>`
    return
  }
  const aiTaskId = initR.task_id
  document.getElementById('aiStatusLine').textContent = `正在分析 ${code}...（后台处理中）`
  // 轮询进度
  let r = null
  while(true){
    await new Promise(res => setTimeout(res, 2000))
    const prog = await api('/ai/analyze_progress/'+aiTaskId)
    if(!prog) continue
    if(prog.status === 'error'){
      btn.disabled = false; btn.textContent = '🤖 开始分析'
      const err = prog.error || '分析失败'
      document.getElementById('aiStatusLine').textContent = '❌ ' + err
      document.getElementById('aiResult').innerHTML = `
        <div style="padding:15px">
          <div style="background:#2d1a1e;border:1px solid var(--red);border-radius:8px;padding:13px">
            <div style="color:var(--rl);font-weight:700;margin-bottom:6px">❌ AI分析失败</div>
            <div style="color:#f0a0a0;font-size:13px">${esc(err)}</div>
          </div>
        </div>`
      return
    }
    if(prog.status === 'done'){
      r = await api('/ai/analyze_result/'+aiTaskId)
      break
    }
    // 还在跑，更新提示
    document.getElementById('aiStatusLine').textContent = `正在分析 ${code}...（AI 多智能体推理中）`
  }
  btn.disabled = false; btn.textContent = '🤖 开始分析'

  if(!r || !r.ok){
    const err = r?.error || '分析失败，请检查AI设置'
    document.getElementById('aiStatusLine').textContent = '❌ ' + err
    document.getElementById('aiResult').innerHTML = `
      <div style="padding:15px">
        <div style="background:#2d1a1e;border:1px solid var(--red);border-radius:8px;padding:13px">
          <div style="color:var(--rl);font-weight:700;margin-bottom:6px">❌ AI分析失败</div>
          <div style="color:#f0a0a0;font-size:13px">${esc(err)}</div>
          <div style="margin-top:10px;font-size:12px;color:var(--t3)">
            请前往「⚙ 设置」标签页配置 API Key 并启用 AI 功能
          </div>
        </div>
      </div>`
    return
  }

  const priceStr = r.realtime_price ? `  实时 ¥${r.realtime_price}` : ''
  document.getElementById('aiStatusLine').textContent =
    `✅ ${r.name||r.code}(${r.code}) 分析完成 — ${r.summary?.action || r.score?.signal || '—'}${priceStr}`
  renderAIResult(r)
}

function renderAIResult(r){
  const score     = r.score       || {}
  const s         = r.summary     || {}
  const plan      = r.decision_plan || {}
  const evidence  = r.evidence || {}
  const ind       = r.indicators  || score.indicators || {}
  const dims      = score.dimensions || {}
  const act       = plan.action || s.action || '—'
  const aiEnabled = r.ai_enabled  !== false

  // 决策颜色映射
  const actMap = {
    '强烈买入':{cls:'ai-sum-buy',   ic:'ai-act-buy',    emoji:'🚀'},
    '买入':    {cls:'ai-sum-buy',   ic:'ai-act-buy',    emoji:'🟢'},
    '持有':    {cls:'ai-sum-watch', ic:'ai-act-watch',  emoji:'🟡'},
    '观望':    {cls:'ai-sum-watch', ic:'ai-act-watch',  emoji:'🟡'},
    '减仓':    {cls:'ai-sum-reduce',ic:'ai-act-reduce', emoji:'🟠'},
    '卖出':    {cls:'ai-sum-sell',  ic:'ai-act-sell',   emoji:'🔴'},
  }
  const am   = actMap[act] || {cls:'ai-sum-watch',ic:'ai-act-watch',emoji:'⚪'}
  const conf = plan.confidence ?? s.confidence ?? 0

  const fmtPrice = v => { const n=Number(v); return Number.isFinite(n) && n>0 ? `¥${n.toFixed(2)}` : '—' }
  const fmtPct = v => { const n=Number(v); return Number.isFinite(n) ? `${Math.round(n*100)}%` : '—' }
  const actionTone = act==='卖出'||act==='减仓' ? 'sell' : (act==='强烈买入'||act==='买入' ? 'buy' : 'watch')
  const listLines = arr => (arr||[]).slice(0,4).map(x=>`<div>${esc(x)}</div>`).join('') || '<div>暂无</div>'

  // 六维维度颜色
  const dimColors = {
    trend:'#3fb950', deviation:'#79c0ff', volume:'#e3b341',
    support:'#8250df', macd:'#1f6feb', rsi:'#f85149'
  }

  let html = ''

  // ══ 交易决策驾驶舱 ══════════════════════════════
  if(plan.action){
    const ez = plan.entry_zone || {}
    const tp = plan.take_profit || {}
    const evTech = evidence.technical || {}
    const evMarket = evidence.market || {}
    const evSent = evidence.sentiment || {}
    const evFund = evidence.fundamental || {}
    html += `<div class="ai-cockpit ${am.cls}">
      <div class="ai-cp-top">
        <div class="ai-cp-action">
          <div class="ai-cp-k">交易动作</div>
          <div class="ai-cp-v ${actionTone}">${am.emoji} ${esc(act)}</div>
          <div class="ai-cp-sub">${esc(plan.notes || '等待确认')}</div>
        </div>
        <div class="ai-cp-action">
          <div class="ai-cp-k">置信度</div>
          <div class="ai-cp-v">${conf || 0}<span style="font-size:11px;color:var(--t3);font-weight:600"> / 100</span></div>
          <div class="ai-cp-sub">量化分 ${score.total ?? '—'}</div>
        </div>
        <div class="ai-cp-action">
          <div class="ai-cp-k">建议仓位</div>
          <div class="ai-cp-v">${fmtPct(plan.position_pct)}</div>
          <div class="ai-cp-sub">单票上限按风控执行</div>
        </div>
      </div>
      <div class="ai-cp-grid">
        <div class="ai-cp-cell"><div class="k">入场区间</div><div class="v">${fmtPrice(ez.low)} - ${fmtPrice(ez.high)}</div></div>
        <div class="ai-cp-cell"><div class="k">止损</div><div class="v" style="color:#3fb950">${fmtPrice(plan.stop_loss)}</div></div>
        <div class="ai-cp-cell"><div class="k">止盈</div><div class="v" style="color:#f85149">${fmtPrice(tp.t1)} / ${fmtPrice(tp.t2)}</div></div>
        <div class="ai-cp-cell"><div class="k">持仓周期</div><div class="v">${esc(plan.hold_days || '—')}</div></div>
      </div>
      <div class="ai-cp-evidence">
        <div class="ai-ev"><div class="ai-ev-h">技术</div><div class="ai-ev-b">${esc((evTech.signal||score.signal||'—') + ' · ' + (evTech.trend||''))}</div></div>
        <div class="ai-ev"><div class="ai-ev-h">市场</div><div class="ai-ev-b">${esc((evMarket.trend||'未知') + ' · 强度' + (evMarket.strength ?? '—'))}</div></div>
        <div class="ai-ev"><div class="ai-ev-h">情绪</div><div class="ai-ev-b">${esc((evSent.label||'中性') + ' · ' + ((evSent.score||0)>0?'+':'') + (evSent.score ?? 0))}</div></div>
        <div class="ai-ev"><div class="ai-ev-h">基本面</div><div class="ai-ev-b">${esc(evFund.summary||'暂无可靠基本面数据')}</div></div>
      </div>
      <div class="ai-cp-lists">
        <div class="ai-cp-list"><div class="ai-cp-list-h">撤单 / 放弃条件</div>${listLines(plan.cancel_conditions)}</div>
        <div class="ai-cp-list"><div class="ai-cp-list-h">风险旗标</div>${listLines(plan.risk_flags)}</div>
      </div>
    </div>`
  }

  // ══ 六维评分仪表盘 ══════════════════════════════
  if(score.ok){
    const total    = score.total    || 0
    const sig      = score.signal   || '—'
    const sigColor = score.signal_color || '#8b949e'

    const levels = [
      {lbl:'卖出',   min:0,  color:'#da3633'},
      {lbl:'观望',   min:30, color:'#bd561d'},
      {lbl:'持有',   min:45, color:'#e3b341'},
      {lbl:'买入',   min:60, color:'#1f6feb'},
      {lbl:'强烈买入',min:75,color:'#238636'},
    ]

    html += `<div class="score-dashboard">
      <div class="score-header">
        <div class="score-circle" style="border-color:${sigColor}">
          <span class="sc-num" style="color:${sigColor}">${total}</span>
          <span class="sc-max">/100</span>
        </div>
        <div class="score-info">
          <div class="score-signal" style="color:${sigColor}">${sig}</div>
          <div style="font-size:12px;color:var(--t2);margin-bottom:4px">${esc(r.name||r.code||'')} (${r.code||''})</div>
          <div class="score-levels">
            ${levels.map(lv=>`<span class="score-level-dot ${total>=lv.min?'active':''}" style="color:${lv.color};border-color:${lv.color}">${lv.lbl}</span>`).join('')}
          </div>
        </div>
      </div>
      <div class="dim-bars">
        ${Object.entries(dims).map(([key,d])=>`
          <div class="dim-bar-item">
            <div class="dim-bar-label">
              <span>${esc(d.label||key)}</span>
              <span style="color:${dimColors[key]||'#79c0ff'};font-weight:800">${d.score||0}/${d.max||0}</span>
            </div>
            <div class="dim-bar-track">
              <div class="dim-bar-fill" style="width:${d.pct||0}%;background:${dimColors[key]||'#79c0ff'}"></div>
            </div>
            <div class="dim-bar-detail">${esc(d.detail||'')}</div>
          </div>`).join('')}
      </div>
      <div class="price-cards">
        <div class="price-card"><div class="price-card-lbl">建议入场</div><div class="price-card-val" style="color:var(--bl)">¥${score.entry_price||'—'}</div></div>
        <div class="price-card"><div class="price-card-lbl">目标 T1</div><div class="price-card-val" style="color:var(--gl)">¥${s.target1||score.target1||'—'}</div></div>
        <div class="price-card"><div class="price-card-lbl">目标 T2</div><div class="price-card-val" style="color:#3fb950">¥${s.target2||score.target2||'—'}</div></div>
        <div class="price-card"><div class="price-card-lbl">止损位</div><div class="price-card-val" style="color:var(--rl)">¥${s.stop_loss||score.stop_loss||'—'}</div></div>
      </div>
    </div>`

    // 催化剂 / 风险标签
    const cats  = s.catalysts  || score.catalysts  || []
    const risks = s.risks      || score.risks      || []
    if(cats.length || risks.length){
      html += `<div style="padding:0 15px 10px">
        ${cats.length?`<div style="margin-bottom:5px;font-size:11px;color:var(--gl);font-weight:600">▲ 催化因素</div>
          <div class="signal-tags">${cats.map(c=>`<span class="cat-tag">${esc(c)}</span>`).join('')}</div>`:''}
        ${risks.length?`<div style="margin:8px 0 5px;font-size:11px;color:var(--rl);font-weight:600">▼ 风险提示</div>
          <div class="signal-tags">${risks.map(c=>`<span class="risk-tag">${esc(c)}</span>`).join('')}</div>`:''}
      </div>`
    }
  }

  // ══ 技术指标 mini 卡（AI 回传 indicators 时）════
  if(ind && ind.price != null){
    const vr  = ind.vol_ratio  || 0
    const c5  = ind.chg_5d_pct || 0
    const c20 = ind.chg_20d_pct|| 0
    const mh  = ind.macd_hist  || 0
    html += `<div class="ind-grid">
      ${indCard('现价',    ind.price, '')}
      ${indCard('RSI14',   ind.rsi14, rsiLabel(ind.rsi14))}
      ${indCard('量比',    vr+'x',    vr>2?'放量':vr<0.5?'缩量':'')}
      ${indCard('5日涨跌', (c5>0?'+':'')+c5+'%',   '', c5>0?'pos':'neg')}
      ${indCard('20日涨跌',(c20>0?'+':'')+c20+'%', '', c20>0?'pos':'neg')}
      ${indCard('MACD柱',  mh,        mh>0?'多头':'空头', mh>0?'pos':'neg')}
    </div>`
  }

  // ══ 决策摘要（AI 启用时）════════════════════════
  if(s.action){
    html += `<div class="ai-summary ${am.cls}">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <span style="font-size:22px">${am.emoji}</span>
        <div class="ai-action ${am.ic}">${act}</div>
        ${s.position?`<span style="font-size:12px;background:rgba(255,255,255,.08);padding:2px 8px;border-radius:4px;color:var(--t2)">${esc(s.position)}</span>`:''}
        <div style="margin-left:auto;font-size:12px;color:var(--t3)">置信度 ${conf}%</div>
      </div>
      <div class="ai-conf-bar"><div class="ai-conf-fill" style="width:${conf}%"></div></div>
      ${s.summary?`<div style="font-size:13px;color:var(--t2);margin-top:8px;line-height:1.5">${esc(s.summary)}</div>`:''}
      ${s.checklist?.length?`<ul class="checklist">${s.checklist.map(c=>`<li>${esc(c)}</li>`).join('')}</ul>`:''}
    </div>`
  }

  // ══ 决策稳定性警告 ══════════════════════════════
  if(r.stability && !r.stability.stable && r.stability.warn){
    html += `<div style="margin:0 15px 10px;background:#1e1000;border:1px solid #d29922;border-radius:8px;padding:12px 14px;font-size:12px;color:#e3b341;line-height:1.6">
      ${esc(r.stability.warn)}
    </div>`
  }

  // ══ 新闻情绪面板 ════════════════════════════════
  const newsArr = r.news||[]
  const sent = r.news_sentiment||{}
  if(newsArr.length){
    const sentColor = sent.score>0.2?'#3fb950':sent.score<-0.2?'#f85149':'#8b949e'
    const sentBg    = sent.score>0.2?'#0d2a1a':sent.score<-0.2?'#2d1a1e':'#161b22'
    let newsHtml = newsArr.slice(0,5).map(n=>{
      const meta = [n.source,n.time].filter(Boolean).join(' · ')
      return `<div style="padding:5px 0;border-bottom:1px solid var(--b1);font-size:12px">
        <div style="color:var(--t1);line-height:1.5">${esc(n.title)}</div>
        ${meta?`<div style="font-size:11px;color:var(--t4);margin-top:2px">${esc(meta)}</div>`:''}
      </div>`
    }).join('')
    const kwHtml = (sent.keywords||[]).length
      ? `<div style="margin-top:6px;font-size:11px">${sent.keywords.map(k=>`<span style="display:inline-block;margin:1px 3px 1px 0;padding:1px 5px;border-radius:3px;background:${k.startsWith('+')?'rgba(63,185,80,.15)':'rgba(248,81,73,.15)'};color:${k.startsWith('+')?'#3fb950':'#f85149'}">${esc(k)}</span>`).join('')}</div>`
      : ''
    html += `<div style="padding:0 15px 10px">
      <div style="font-size:11px;font-weight:700;color:var(--t2);letter-spacing:.6px;margin-bottom:7px">📰 近期新闻情绪</div>
      <div style="background:${sentBg};border:1px solid var(--b1);border-radius:8px;padding:10px 12px">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
          <span style="font-size:14px;font-weight:700;color:${sentColor}">${sent.label||'中性'}</span>
          <span style="font-size:12px;color:var(--t3)">情绪得分：<b style="color:${sentColor}">${(sent.score||0)>0?'+':''}${(sent.score||0).toFixed(2)}</b></span>
          <span style="font-size:11px;color:var(--t4)">${newsArr.length} 条新闻</span>
        </div>
        ${kwHtml}
        <div style="margin-top:8px">${newsHtml}</div>
      </div>
    </div>`
  }

  // ══ 多空辩论面板════════════════════════════════
  if(r.bull_report || r.bear_report){
    html += `<div style="padding:0 15px 10px">
      <div style="font-size:11px;font-weight:700;color:var(--t2);text-transform:uppercase;letter-spacing:.6px;margin-bottom:7px">多空辩论</div>
      <div class="agent-debate">
        <div class="bull-box">
          <div class="debate-hd" style="color:#3fb950">📈 多方研究员</div>
          <div class="debate-body">${esc(r.bull_report)}</div>
        </div>
        <div class="bear-box">
          <div class="debate-hd" style="color:#f85149">📉 空方研究员</div>
          <div class="debate-body">${esc(r.bear_report)}</div>
        </div>
      </div>
    </div>`
  }

  // ══ 智能体详情折叠卡════════════════════════════
  html += `<div class="agent-steps">`
  const agents = [
    {key:'tech_report',  icon:'📊', name:'技术分析师'},
    {key:'risk_report',  icon:'🛡', name:'风险管理师'},
    {key:'final_report', icon:'🎯', name:'首席决策官'},
  ]
  agents.forEach(ag=>{
    const txt = r[ag.key] || ''
    if(!txt) return
    html += `<div class="agent-step">
      <div class="agent-step-hd" data-ui-click="toggleAgent(this)">
        <span class="agent-icon">${ag.icon}</span>
        <span class="agent-name">${ag.name}</span>
        <span class="agent-status ast-done">完成</span>
        <span style="font-size:11px;color:var(--t4);margin-left:6px">▼</span>
      </div>
      <div class="agent-body">${esc(txt)}</div>
    </div>`
  })

  // AI 调用失败（已启用但接口报错）
  if(aiEnabled && r.ai_error){
    html += `<div style="margin:0 15px 12px;background:#1e0a0a;border:1px solid #6e1c24;border-radius:8px;padding:14px 16px">
      <div style="font-size:13px;font-weight:700;color:#f85149;margin-bottom:6px">⚠️ AI深度分析 — 接口调用失败</div>
      <div style="font-size:12px;color:#ff8080;line-height:1.7;margin-bottom:10px;word-break:break-all">
        ${esc(r.ai_error)}
      </div>
      <div style="font-size:11px;color:#a05050;margin-bottom:10px;line-height:1.6">
        常见原因：Base URL 填写错误（需填 API 端点而非文档页面 URL）、API Key 无效或余额不足、模型名称不存在。<br>
        点击「测试连接」可查看详细错误信息。
      </div>
      <button class="btn btn-gray btn-sm" data-ui-click="switchPage('settings')">⚙ 前往AI设置 →</button>
    </div>`
  }

  // 未启用 AI 的提示卡
  if(!aiEnabled){
    html += `<div style="margin:0 15px 12px;background:#1e1600;border:1px solid #7d5e00;border-radius:8px;padding:14px 16px">
      <div style="font-size:13px;font-weight:700;color:#e3b341;margin-bottom:6px">🤖 AI深度分析 — 未启用</div>
      <div style="font-size:12px;color:#d4a843;line-height:1.7;margin-bottom:10px">
        ${esc(r.message||'以上为纯量化评分结果（无需AI）。')}<br>
        启用AI后可获得：<b>技术分析师 → 多方研究员 → 空方研究员 → 风险管理师 → 首席决策官</b> 六级深度分析。
      </div>
      <div style="font-size:11px;color:#a37a2a;margin-bottom:10px;line-height:1.6">
        📌 推荐使用 <b>DeepSeek</b> 或 <b>小米MiMo</b>（国内可用，性价比高）：<br>
        1. DeepSeek：访问 <b>platform.deepseek.com</b> 注册并获取 API Key<br>
        2. 小米MiMo：访问 <b>platform.xiaomimimo.com</b> 注册并获取 API Key<br>
        3. 点击下方「前往AI设置」选择提供商，填入 Key 并保存
      </div>
      <button class="btn btn-orange btn-sm" data-ui-click="switchPage('settings')">⚙ 前往AI设置 →</button>
    </div>`
  }

  html += `</div><div style="height:16px"></div>`

  document.getElementById('aiResult').innerHTML = html

  // 只有 AI 启用时才折叠/展开（否则"未启用"提示卡保持可见）
  if(aiEnabled){
    const steps = document.querySelectorAll('.agent-step')
    if(steps.length) toggleAgent(steps[steps.length-1].querySelector('.agent-step-hd'))
  }
}

function indCard(lbl, val, sub, cls=''){
  return `<div class="ind-card">
    <div class="ind-lbl">${lbl}</div>
    <div class="ind-val ${cls}">${val??'—'}</div>
    ${sub?`<div class="ind-sub">${sub}</div>`:''}
  </div>`
}
function rsiLabel(v){ if(!v) return ''; return v>70?'超买':v<30?'超卖':'' }

function toggleAgent(hd){
  const body = hd.nextElementSibling
  const arr  = hd.querySelector('span:last-child')
  if(!body) return
  const open = body.style.display !== 'none' && body.style.display !== ''
  // close all
  document.querySelectorAll('.agent-body').forEach(b=>b.style.display='none')
  document.querySelectorAll('.agent-step-hd span:last-child').forEach(a=>a.textContent='▼')
  if(!open){ body.style.display='block'; if(arr) arr.textContent='▲' }
}
// 回测执行与结果渲染函数已拆到 /static/app/js/app-backtest.js
async function api(path,method='GET',body=null){
  const hasBody = body !== null && body !== undefined
  const makeOpts = ()=>{
    const opts={method,credentials:'include',headers:{}}
    if(hasBody){
      opts.headers['Content-Type']='application/json'
      opts.body=JSON.stringify(body)
    }
    return opts
  }
  const opts=makeOpts()
  try{
    let r=await fetch(API+path,opts)
    if(r.status===404 && String(path||'').startsWith('/mobile_ths/') && location.port!=='5002'){
      const retryOpts=makeOpts()
      r=await fetch(MOBILE_API_FALLBACK+path,retryOpts)
    }
    const txt=await r.text()
    let data=null
    if(txt){ try{ data=JSON.parse(txt) }catch(e){ data={ok:false,error:txt.slice(0,240)} } }
    if(!r.ok) return {...(data||{}), ok:false, status:r.status, error:(data&&(data.error||data.msg))||('HTTP '+r.status)}
    return data || {ok:true}
  }catch(e){ console.warn('API失败:',path,e); return {ok:false,error:e&&e.message?e.message:String(e),network:true} }
}

// ══════════════════════════════════════════════════
//  K线弹窗
// ══════════════════════════════════════════════════
// K线弹窗、请求、盘口与绘图函数已拆到 /static/app/js/app-kline.js

// 模拟盘与账户设置条函数已拆到 /static/app/js/app-paper.js
