// K线弹窗、数据请求、盘口渲染与绘图模块：依赖 app-core.js 的 api/esc。
function closeKL(){ document.getElementById('klOverlay').classList.remove('show') }
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeKL() })

const KL_STATE = { code:'', rawCode:'', name:'', mode:'second', second:null, minute:null, daily:null, orderBook:null }
const KL_CLIENT_CACHE = new Map()
const KL_ORDER_CACHE = new Map()

function _plainKLineCode(code){
  let raw = String(code||'').trim().toUpperCase()
  if(raw.includes('.')) raw = raw.split('.')[0]
  if(['SZ','SH','BJ'].includes(raw.slice(0,2))) raw = raw.slice(2)
  const digits = raw.replace(/\D/g,'')
  return (digits ? digits.slice(-6) : raw.slice(-6)).padStart(6,'0')
}
function _klineApiPath(mode, code){
  const c = encodeURIComponent(_plainKLineCode(code))
  return mode==='second' ? '/kline/'+c+'/seconds' : (mode==='minute' ? '/kline/'+c+'/minute' : '/kline/'+c)
}
function _klineCacheTtl(mode){ return mode==='daily' ? 5*60*1000 : 45*1000 }
function _getKLineEntry(mode, code){
  const key = mode+':'+_plainKLineCode(code)
  const hit = KL_CLIENT_CACHE.get(key)
  if(hit?.resp && Date.now() - hit.ts < _klineCacheTtl(mode)) return hit
  if(hit?.promise) return hit
  return null
}
function _fetchKLineMode(mode, code){
  const plain = _plainKLineCode(code)
  const key = mode+':'+plain
  const hit = _getKLineEntry(mode, plain)
  if(hit?.resp) return Promise.resolve(hit.resp)
  if(hit?.promise) return hit.promise
  const promise = api(_klineApiPath(mode, plain)).then(resp=>{
    KL_CLIENT_CACHE.set(key, {resp, ts:Date.now()})
    return resp
  }).catch(e=>{
    KL_CLIENT_CACHE.delete(key)
    throw e
  })
  KL_CLIENT_CACHE.set(key, {promise, ts:Date.now()})
  return promise
}
function _prefetchKLineBundle(code){
  const plain = _plainKLineCode(code)
  _fetchKLineMode('daily', plain).catch(()=>null)
}
function _withTimeout(promise, ms){
  if(!ms) return promise
  return Promise.race([promise, new Promise(resolve=>setTimeout(()=>resolve(null), ms))])
}

function _setKLineTabs(mode){
  document.getElementById('klTabSecond')?.classList.toggle('active', mode==='second')
  document.getElementById('klTabMinute')?.classList.toggle('active', mode==='minute')
  document.getElementById('klTabDaily')?.classList.toggle('active', mode==='daily')
}
async function switchKLineMode(mode){
  await _renderKLineMode(mode, false)
}

async function _renderKLineMode(mode, allowFallback){
  const stateCode = KL_STATE.code
  KL_STATE.mode = mode
  _setKLineTabs(mode)
  const cvs = document.getElementById('klCanvas')
  const ctx = cvs.getContext('2d')
  ctx.clearRect(0,0,cvs.width,cvs.height)
  document.getElementById('klInfo').innerHTML = ''
  const loadingText = mode==='second' ? '秒级加载中...' : (mode==='minute' ? '分时加载中...' : '日K加载中...')
  document.getElementById('klSrc').textContent = loadingText

  let resp = KL_STATE[mode]
  if(!resp){
    const waitMs = allowFallback && mode==='second' ? 5500 : (allowFallback && mode==='minute' ? 2500 : 0)
    resp = await _withTimeout(_fetchKLineMode(mode, stateCode), waitMs)
    if(KL_STATE.code!==stateCode) return
    if(resp) KL_STATE[mode] = resp
  }
  if(KL_STATE.code!==stateCode || KL_STATE.mode!==mode) return

  if(!resp?.ok || !resp.data?.length){
    if(mode==='second' && allowFallback) return _renderKLineMode('minute', true)
    if(mode==='minute' && allowFallback) return _renderKLineMode('daily', false)
    document.getElementById('klSrc').textContent=''
    const label = mode==='second' ? '秒级 Tick' : (mode==='minute' ? '分时数据' : '历史数据')
    document.getElementById('klInfo').innerHTML = '<span style="color:var(--rl)">暂无'+label+'</span>'
    return
  }

  const _klSrc = document.getElementById('klSrc')
  if(mode==='second'){
    _klSrc.textContent = resp.source==='tushare_ticks' ? 'Tushare 秒级' : '秒级成交'
    drawSecondLine(cvs, resp.data)
    return
  }
  if(mode==='minute'){
    _klSrc.textContent = resp.source==='tushare_minute' ? 'Tushare 分时' : '分钟分时'
    drawMinuteLine(cvs, resp.data)
    return
  }
  if(resp.stale){
    _klSrc.innerHTML = '<span style="color:var(--rl)" title="'+(resp.msg||'本地缓存可能过期').replace(/"/g,'')+'">本地缓存·可能过期</span>'
  } else {
    _klSrc.textContent = resp.source==='tdx' ? 'TDX实时' : '本地数据'
  }
  drawKLine(cvs, resp.data)
}
async function showKLine(code, name){
  const ov = document.getElementById('klOverlay')
  const plain = _plainKLineCode(code)
  document.getElementById('klTitle').textContent = `${plain}  ${name||plain}`
  document.getElementById('klSrc').textContent = '加载中...'
  document.getElementById('klInfo').innerHTML = ''
  const book = document.getElementById('klBook')
  if(book) book.innerHTML = '<div class="kl-book-empty">五档盘口加载中...</div>'
  const cvs = document.getElementById('klCanvas')
  const ctx  = cvs.getContext('2d')
  ctx.clearRect(0,0,cvs.width,cvs.height)
  KL_STATE.code = plain
  KL_STATE.rawCode = String(code||'')
  KL_STATE.name = name||plain
  KL_STATE.mode = 'second'
  KL_STATE.second = null
  KL_STATE.minute = null
  KL_STATE.daily = null
  KL_STATE.orderBook = null
  _setKLineTabs('second')
  ov.classList.add('show')
  await _renderKLineMode('second', true)
  _prefetchKLineBundle(plain)
  _renderOrderBook(plain)
}
function _normDate(d){
  // 统一把各种日期格式转成 "YYYY-MM-DD" 字符串
  if(!d) return ''
  const s = String(d)
  // 已经是 YYYY-MM-DD
  if(/^\d{4}-\d{2}-\d{2}$/.test(s)) return s
  // ISO 格式 "YYYY-MM-DDTHH:MM:SS"
  if(/^\d{4}-\d{2}-\d{2}T/.test(s)) return s.slice(0,10)
  // Flask 旧版 HTTP 日期 "Wed, 31 Dec 2025 00:00:00 GMT"
  try{
    const dt = new Date(s)
    if(!isNaN(dt)){
      const y = dt.getFullYear()
      const m = String(dt.getMonth()+1).padStart(2,'0')
      const day = String(dt.getDate()).padStart(2,'0')
      return `${y}-${m}-${day}`
    }
  }catch(e){}
  return s.slice(0,10)
}

function _fmtMinuteLabel(d){
  const raw = String(d.datetime || d.date || '')
  if(/^\d{2}:\d{2}/.test(raw)) return raw.slice(0,5)
  if(raw.length>=16) return raw.slice(11,16)
  return raw.slice(0,5)
}


function _fmtSecondLabel(d){
  const raw = String(d.datetime || d.date || '')
  if(/^\d{2}:\d{2}:\d{2}/.test(raw)) return raw.slice(0,8)
  if(raw.length>=19) return raw.slice(11,19)
  return raw.slice(0,8)
}
function _fmtVol(v){
  const n=+(v||0)
  if(!Number.isFinite(n)) return '--'
  if(Math.abs(n)>=100000000) return (n/100000000).toFixed(2)+'?'
  if(Math.abs(n)>=10000) return (n/10000).toFixed(1)+'?'
  return Math.round(n).toString()
}
function _fmtPx(v){ const n=+v; return Number.isFinite(n)&&n>0 ? n.toFixed(2) : '--' }
async function _renderOrderBook(code){
  const el=document.getElementById('klBook')
  if(!el) return
  el.innerHTML='<div class="kl-book-empty">五档盘口加载中...</div>'
  const stateCode=_plainKLineCode(code)
  const hit=KL_ORDER_CACHE.get(stateCode)
  let resp = hit && Date.now()-hit.ts<5000 ? hit.resp : null
  if(!resp){
    resp=await api('/quote/'+encodeURIComponent(stateCode)+'/order_book')
    KL_ORDER_CACHE.set(stateCode,{resp,ts:Date.now()})
  }
  if(KL_STATE.code!==stateCode) return
  KL_STATE.orderBook=resp
  if(!resp?.ok || !resp.data){
    el.innerHTML='<div class="kl-book-empty">暂无实时五档盘口</div>'
    return
  }
  const d=resp.data
  const ch=+(d.change_pct||0)
  const up=ch>=0
  const color=up?'#ef4444':'#22c55e'
  const asks=[...(d.asks||[])].sort((a,b)=>(b.level||0)-(a.level||0))
  const bids=[...(d.bids||[])].sort((a,b)=>(a.level||0)-(b.level||0))
  const sideHtml=(rows,type)=>rows.map(r=>`<div class="kl-book-row ${type}"><span class="lv">${type==='ask'?'卖':'买'}${r.level}</span><span class="px">${_fmtPx(r.price)}</span><span class="vol">${_fmtVol(r.volume)}</span></div>`).join('')
  el.innerHTML=`
    <div class="kl-book-top">
      <div><div class="kl-book-name">${d.name||KL_STATE.name||d.code||''}</div><div class="kl-book-price" style="color:${color}">${_fmtPx(d.price)}</div></div>
      <div class="kl-book-meta"><div style="color:${color};font-weight:700">${ch>=0?'+':''}${ch.toFixed(2)}%</div><div>${String(d.trade_time||'').slice(11,19)}</div></div>
    </div>
    <div class="kl-book-grid">
      <div class="kl-book-side"><h4>卖盘</h4>${sideHtml(asks,'ask')}</div>
      <div class="kl-book-side"><h4>买盘</h4>${sideHtml(bids,'bid')}</div>
    </div>`
}

function drawSecondLine(canvas, rawData){
  const data = [...rawData]
    .map(d=>({...d, price:+d.price, volume:+(d.volume||0), prev_price:+(d.prev_price||0)}))
    .filter(d=>Number.isFinite(d.price) && d.price>0)
    .sort((a,b)=>String(a.datetime||a.date).localeCompare(String(b.datetime||b.date)))
  const n = data.length; if(!n) return

  const rect = canvas.getBoundingClientRect()
  const dpr = window.devicePixelRatio||1
  canvas.width = rect.width*dpr
  canvas.height = rect.height*dpr
  const ctx = canvas.getContext('2d')
  ctx.scale(dpr,dpr)
  const RW=rect.width, RH=rect.height
  const PAD={t:18,r:62,b:42,l:12}
  const VOL_H=Math.floor(RH*.18)
  const LH=RH-PAD.t-PAD.b-VOL_H-10
  const CW=RW-PAD.l-PAD.r
  const prices=data.map(d=>d.price)
  const vols=data.map(d=>d.volume||0)
  const firstRef=data.find(d=>Number.isFinite(d.prev_price)&&d.prev_price>0)?.prev_price || prices[0]
  const maxP=Math.max(...prices, firstRef)*1.001
  const minP=Math.min(...prices, firstRef)*0.999
  const pRng=maxP-minP||1
  const maxV=Math.max(...vols)||1
  const x=i=>PAD.l + (n===1 ? CW/2 : CW*i/(n-1))
  const py=p=>PAD.t + LH*(1-(p-minP)/pRng)
  const vy=v=>PAD.t+LH+10+VOL_H*(1-v/maxV)

  ctx.fillStyle='#0d1117'; ctx.fillRect(0,0,RW,RH)
  ctx.strokeStyle='#1e2530'; ctx.lineWidth=1
  for(let j=0;j<=4;j++){
    const y=PAD.t+LH/4*j
    ctx.beginPath(); ctx.moveTo(PAD.l,y); ctx.lineTo(PAD.l+CW,y); ctx.stroke()
    const price=maxP-(pRng/4)*j
    ctx.fillStyle='#6e7681'; ctx.font='10px monospace'; ctx.textAlign='left'
    ctx.fillText(price.toFixed(2), PAD.l+CW+5, y+4)
  }
  const refY=py(firstRef)
  ctx.setLineDash([4,4]); ctx.strokeStyle='rgba(148,163,184,.55)'
  ctx.beginPath(); ctx.moveTo(PAD.l,refY); ctx.lineTo(PAD.l+CW,refY); ctx.stroke(); ctx.setLineDash([])

  const grad=ctx.createLinearGradient(0,PAD.t,0,PAD.t+LH)
  grad.addColorStop(0,'rgba(20,184,166,.25)')
  grad.addColorStop(1,'rgba(20,184,166,0)')
  ctx.beginPath(); data.forEach((d,i)=>{ const xx=x(i), yy=py(d.price); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy) })
  ctx.lineTo(x(n-1), PAD.t+LH); ctx.lineTo(x(0), PAD.t+LH); ctx.closePath(); ctx.fillStyle=grad; ctx.fill()

  ctx.strokeStyle='#14b8a6'; ctx.lineWidth=1.8; ctx.beginPath()
  data.forEach((d,i)=>{ const xx=x(i), yy=py(d.price); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy) })
  ctx.stroke()
  const last=data[n-1]
    if(n===1){
    const yy=py(last.price)
    ctx.strokeStyle='rgba(20,184,166,.45)'; ctx.lineWidth=1.2; ctx.beginPath(); ctx.moveTo(PAD.l,yy); ctx.lineTo(PAD.l+CW,yy); ctx.stroke()
    ctx.fillStyle='rgba(20,184,166,.20)'; ctx.beginPath(); ctx.arc(x(n-1), yy, 10, 0, Math.PI*2); ctx.fill()
  }
  ctx.fillStyle='#14b8a6'; ctx.beginPath(); ctx.arc(x(n-1), py(last.price), 5, 0, Math.PI*2); ctx.fill()

  const barW=Math.max(1, Math.floor(CW/Math.max(n,1))*.58)
  vols.forEach((v,i)=>{
    const up=i===0 ? data[i].price>=firstRef : data[i].price>=data[i-1].price
    ctx.fillStyle=up?'rgba(239,68,68,.45)':'rgba(34,197,94,.45)'
    const top=vy(v), bot=PAD.t+LH+10+VOL_H
    ctx.fillRect(x(i)-barW/2, top, barW, bot-top)
  })

  ctx.strokeStyle='#21262d'; ctx.beginPath(); ctx.moveTo(PAD.l,PAD.t+LH+5); ctx.lineTo(PAD.l+CW,PAD.t+LH+5); ctx.stroke()
  ctx.fillStyle='#484f58'; ctx.font='9px monospace'; ctx.textAlign='center'
  const step=Math.max(1,Math.ceil(n/6))
  data.forEach((d,i)=>{ if(i%step===0 || i===n-1) ctx.fillText(_fmtSecondLabel(d), x(i), RH-PAD.b+14) })

  const change=last.price-firstRef
  const pct=firstRef ? (change/firstRef*100) : 0
  const color=change>=0?'#ef4444':'#22c55e'
  const hi=Math.max(...prices), lo=Math.min(...prices)
  document.getElementById('klInfo').innerHTML=`
    <span>最新 <b style="color:${color}">${last.price.toFixed(2)}</b></span>
    <span style="color:${color};font-weight:700">${change>=0?'+':''}${change.toFixed(2)} / ${pct>=0?'+':''}${pct.toFixed(2)}%</span>
    <span>参考 <b>${firstRef.toFixed(2)}</b></span>
    <span>高 <b style="color:#ef4444">${hi.toFixed(2)}</b></span>
    <span>低 <b style="color:#22c55e">${lo.toFixed(2)}</b></span>
    <span>成交 <b>${_fmtVol(last.volume)}</b></span>
    <span style="color:var(--t4);margin-left:8px">${String(last.datetime||last.date||'')}</span>
  `
}

function drawMinuteLine(canvas, rawData){
  const data = [...rawData]
    .map(d=>({...d, close:+d.close, open:+d.open, high:+d.high, low:+d.low, volume:+(d.volume||0)}))
    .filter(d=>Number.isFinite(d.close))
    .sort((a,b)=>String(a.datetime||a.date).localeCompare(String(b.datetime||b.date)))
  const n = data.length; if(!n) return

  const rect = canvas.getBoundingClientRect()
  const dpr = window.devicePixelRatio||1
  canvas.width = rect.width*dpr
  canvas.height = rect.height*dpr
  const ctx = canvas.getContext('2d')
  ctx.scale(dpr,dpr)
  const RW=rect.width, RH=rect.height
  const PAD={t:18,r:62,b:42,l:12}
  const VOL_H=Math.floor(RH*.18)
  const LH=RH-PAD.t-PAD.b-VOL_H-10
  const CW=RW-PAD.l-PAD.r
  const closes=data.map(d=>d.close)
  const highs=data.map(d=>Number.isFinite(d.high)?d.high:d.close)
  const lows=data.map(d=>Number.isFinite(d.low)?d.low:d.close)
  const vols=data.map(d=>d.volume||0)
  const ref=Number.isFinite(data[0].open)&&data[0].open>0 ? data[0].open : closes[0]
  const maxP=Math.max(...highs, ref)*1.001
  const minP=Math.min(...lows, ref)*0.999
  const pRng=maxP-minP||1
  const maxV=Math.max(...vols)||1
  const x=i=>PAD.l + (n===1 ? CW/2 : CW*i/(n-1))
  const py=p=>PAD.t + LH*(1-(p-minP)/pRng)
  const vy=v=>PAD.t+LH+10+VOL_H*(1-v/maxV)

  ctx.fillStyle='#0d1117'; ctx.fillRect(0,0,RW,RH)
  ctx.strokeStyle='#1e2530'; ctx.lineWidth=1
  for(let j=0;j<=4;j++){
    const y=PAD.t+LH/4*j
    ctx.beginPath(); ctx.moveTo(PAD.l,y); ctx.lineTo(PAD.l+CW,y); ctx.stroke()
    const price=maxP-(pRng/4)*j
    ctx.fillStyle='#6e7681'; ctx.font='10px monospace'; ctx.textAlign='left'
    ctx.fillText(price.toFixed(2), PAD.l+CW+5, y+4)
  }
  const refY=py(ref)
  ctx.setLineDash([4,4]); ctx.strokeStyle='rgba(148,163,184,.55)'
  ctx.beginPath(); ctx.moveTo(PAD.l,refY); ctx.lineTo(PAD.l+CW,refY); ctx.stroke(); ctx.setLineDash([])

  const grad=ctx.createLinearGradient(0,PAD.t,0,PAD.t+LH)
  grad.addColorStop(0,'rgba(59,130,246,.28)')
  grad.addColorStop(1,'rgba(59,130,246,0)')
  ctx.beginPath(); data.forEach((d,i)=>{ const xx=x(i), yy=py(d.close); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy) })
  ctx.lineTo(x(n-1), PAD.t+LH); ctx.lineTo(x(0), PAD.t+LH); ctx.closePath(); ctx.fillStyle=grad; ctx.fill()

  ctx.strokeStyle='#58a6ff'; ctx.lineWidth=1.8; ctx.beginPath()
  data.forEach((d,i)=>{ const xx=x(i), yy=py(d.close); i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy) })
  ctx.stroke()
  const last=data[n-1]
    if(n===1){
    const yy=py(last.close)
    ctx.strokeStyle='rgba(88,166,255,.45)'; ctx.lineWidth=1.2; ctx.beginPath(); ctx.moveTo(PAD.l,yy); ctx.lineTo(PAD.l+CW,yy); ctx.stroke()
    ctx.fillStyle='rgba(88,166,255,.20)'; ctx.beginPath(); ctx.arc(x(n-1), yy, 10, 0, Math.PI*2); ctx.fill()
  }
  ctx.fillStyle='#58a6ff'; ctx.beginPath(); ctx.arc(x(n-1), py(last.close), 5, 0, Math.PI*2); ctx.fill()

  const barW=Math.max(1, Math.floor(CW/Math.max(n,1))*.58)
  vols.forEach((v,i)=>{
    const up=i===0 ? data[i].close>=ref : data[i].close>=data[i-1].close
    ctx.fillStyle=up?'rgba(239,68,68,.45)':'rgba(34,197,94,.45)'
    const top=vy(v), bot=PAD.t+LH+10+VOL_H
    ctx.fillRect(x(i)-barW/2, top, barW, bot-top)
  })

  ctx.strokeStyle='#21262d'; ctx.beginPath(); ctx.moveTo(PAD.l,PAD.t+LH+5); ctx.lineTo(PAD.l+CW,PAD.t+LH+5); ctx.stroke()
  ctx.fillStyle='#484f58'; ctx.font='9px monospace'; ctx.textAlign='center'
  const step=Math.max(1,Math.ceil(n/6))
  data.forEach((d,i)=>{ if(i%step===0 || i===n-1) ctx.fillText(_fmtMinuteLabel(d), x(i), RH-PAD.b+14) })

  const change=last.close-ref
  const pct=ref ? (change/ref*100) : 0
  const color=change>=0?'#ef4444':'#22c55e'
  const hi=Math.max(...highs), lo=Math.min(...lows)
  document.getElementById('klInfo').innerHTML=`
    <span>最新 <b style="color:${color}">${last.close.toFixed(2)}</b></span>
    <span style="color:${color};font-weight:700">${change>=0?'+':''}${change.toFixed(2)} / ${pct>=0?'+':''}${pct.toFixed(2)}%</span>
    <span>均线参考 <b>${ref.toFixed(2)}</b></span>
    <span>高 <b style="color:#ef4444">${hi.toFixed(2)}</b></span>
    <span>低 <b style="color:#22c55e">${lo.toFixed(2)}</b></span>
    <span style="color:var(--t4);margin-left:8px">${String(last.datetime||last.date||'')}</span>
  `
}
function drawKLine(canvas, rawData){
  // 统一日期格式，按升序排列
  const data = [...rawData].map(d=>({...d, date:_normDate(d.date)})).sort((a,b)=>a.date<b.date?-1:1)
  const n = data.length; if(!n) return

  // 自适应 canvas 真实像素
  const rect = canvas.getBoundingClientRect()
  const dpr  = window.devicePixelRatio||1
  canvas.width  = rect.width  * dpr
  canvas.height = rect.height * dpr
  const W=canvas.width, H=canvas.height
  const ctx=canvas.getContext('2d')
  ctx.scale(dpr,dpr)
  const RW=rect.width, RH=rect.height

  const PAD={t:16,r:60,b:44,l:10}
  const VOL_H = Math.floor(RH*0.18)    // 成交量区高度
  const KH    = RH - PAD.t - PAD.b - VOL_H - 8  // K线区高度
  const CW    = RW - PAD.l - PAD.r     // 图表宽度

  // 数据范围
  const highs  = data.map(d=>+d.high)
  const lows   = data.map(d=>+d.low)
  const closes = data.map(d=>+d.close)
  const opens  = data.map(d=>+d.open)
  const vols   = data.map(d=>+d.volume)
  const maxP = Math.max(...highs)*1.001, minP = Math.min(...lows)*0.999
  const maxV = Math.max(...vols)||1
  const pRng = maxP-minP||1

  const barW = Math.max(1, Math.floor(CW/n)-1)
  const bx   = i => PAD.l + i*(CW/n) + (CW/n-barW)/2
  const py   = p => PAD.t + KH*(1-(p-minP)/pRng)
  const vy   = v => PAD.t + KH + 8 + VOL_H*(1-v/maxV)

  // ── 背景
  ctx.fillStyle='#0d1117'; ctx.fillRect(0,0,RW,RH)

  // ── 网格
  ctx.strokeStyle='#1e2530'; ctx.lineWidth=1
  for(let j=0;j<=4;j++){
    const y=PAD.t+KH/4*j
    ctx.beginPath(); ctx.moveTo(PAD.l,y); ctx.lineTo(PAD.l+CW,y); ctx.stroke()
    // 右侧价格标签
    const price=maxP-(pRng/4)*j
    ctx.fillStyle='#6e7681'; ctx.font=`${10/dpr*dpr}px monospace`
    ctx.textAlign='left'
    ctx.fillText(price.toFixed(2), PAD.l+CW+4, y+4)
  }
  // 成交量区分隔
  ctx.strokeStyle='#21262d'
  ctx.beginPath(); ctx.moveTo(PAD.l,PAD.t+KH+4); ctx.lineTo(PAD.l+CW,PAD.t+KH+4); ctx.stroke()

  // ── MA 线
  const ma=(arr,k)=>arr.map((_,i)=>{
    if(i<k-1)return null
    return arr.slice(i-k+1,i+1).reduce((s,v)=>s+v,0)/k
  })
  const ma5=ma(closes,5), ma10=ma(closes,10), ma20=ma(closes,20)
  const drawMA=(arr,color)=>{
    ctx.strokeStyle=color; ctx.lineWidth=1; ctx.beginPath()
    let s=false
    arr.forEach((v,i)=>{
      if(v===null)return
      const x=bx(i)+barW/2, y=py(v)
      s?(ctx.lineTo(x,y)):(ctx.moveTo(x,y),s=true)
    })
    ctx.stroke()
  }
  drawMA(ma5,'#3b82f6'); drawMA(ma10,'#f59e0b'); drawMA(ma20,'#a78bfa')

  // ── K线蜡烛
  data.forEach((d,i)=>{
    const o=+d.open,c=+d.close,h=+d.high,l=+d.low
    const up=c>=o
    const clr=up?'#ef4444':'#22c55e'
    ctx.strokeStyle=clr; ctx.fillStyle=clr; ctx.lineWidth=1
    // 影线
    ctx.beginPath(); ctx.moveTo(bx(i)+barW/2,py(h)); ctx.lineTo(bx(i)+barW/2,py(l)); ctx.stroke()
    // 实体
    const y1=py(Math.max(o,c)), bh=Math.max(1,py(Math.min(o,c))-y1)
    if(up) ctx.fillRect(bx(i),y1,barW,bh)
    else   ctx.strokeRect(bx(i),y1,barW,bh)
  })

  // ── 成交量
  vols.forEach((v,i)=>{
    const up=(+data[i].close)>=(+data[i].open)
    ctx.fillStyle=up?'rgba(239,68,68,.55)':'rgba(34,197,94,.55)'
    const top=vy(v), bot=PAD.t+KH+8+VOL_H
    ctx.fillRect(bx(i),top,barW,bot-top)
  })

  // ── X轴日期
  ctx.fillStyle='#484f58'; ctx.font='9px monospace'; ctx.textAlign='center'
  const step=Math.max(1,Math.ceil(n/8))
  data.forEach((d,i)=>{
    if(i%step===0) ctx.fillText((d.date||'').slice(5), bx(i)+barW/2, RH-PAD.b+14)
  })

  // ── 最新日 OHLC 信息 + 图例
  const last=data[n-1], prev=data[n-2]||last
  const pct=((+last.close-+prev.close)/+prev.close*100).toFixed(2)
  const pColor=(+last.close)>=(+prev.close)?'#ef4444':'#22c55e'
  document.getElementById('klInfo').innerHTML=`
    <span style="color:#3b82f6">MA5 ${(ma5[n-1]||0).toFixed(2)}</span>
    <span style="color:#f59e0b">MA10 ${(ma10[n-1]||0).toFixed(2)}</span>
    <span style="color:#a78bfa">MA20 ${(ma20[n-1]||0).toFixed(2)}</span>
    <span style="margin-left:8px">开 <b>${(+last.open).toFixed(2)}</b></span>
    <span>高 <b style="color:#ef4444">${(+last.high).toFixed(2)}</b></span>
    <span>低 <b style="color:#22c55e">${(+last.low).toFixed(2)}</b></span>
    <span>收 <b>${(+last.close).toFixed(2)}</b></span>
    <span style="color:${pColor};font-weight:700">${pct>=0?'+':''}${pct}%</span>
    <span style="color:var(--t4);margin-left:8px">${_normDate(last.date)}</span>
  `
}
