// K线绘图模块：供 app-core.js 的 showKLine/switchKLineMode 调用。
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
