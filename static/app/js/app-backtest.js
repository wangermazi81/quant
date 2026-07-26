// 回测执行与结果渲染模块：依赖 app-core.js 的 api/esc 和页面 DOM。
async function runBt(){
  if(_curMode==='factor'){
    alert('因子库不支持回测。\n\n请切换到策略库，选择一个策略再运行回测。\n（因子用于选股，策略用于模拟交易回测）')
    return
  }
  if(!curStrat){alert('请先选择或保存策略');return}
  const btn=document.getElementById('btBtn')
  const cancelBtn=document.getElementById('btCancelBtn')
  btn.disabled=true; btn.textContent='⟳ 回测中...'
  if(cancelBtn) cancelBtn.style.display='inline-flex'

  const codesVal   = document.getElementById('btCodes').value.trim()
  const isFullMkt  = !codesVal
  const capital    = +document.getElementById('btCap').value

  document.getElementById('btSt').textContent = isFullMkt
    ? '全市场模式：正在加载数据（约60-120秒）...'
    : 'Cerebro 逐日模拟中，请稍候...'

  // 销毁旧图表实例（必须在 innerHTML 替换前，否则 Chart.js 内存泄漏）
  Object.values(charts).forEach(c=>{try{c.destroy()}catch(e){}})
  charts={}

  // 占位 UI：全市场模式显示进度条
  document.getElementById('btResult').innerHTML = isFullMkt
    ? `<div class="bt-ph">
        <span class="spin" style="font-size:34px;opacity:.5">⟳</span>
        <p>全市场回测中...</p>
        <span style="font-size:12px;color:var(--t4)">加载本地CSV + TDX活跃标的 → 逐日策略模拟</span>
        <div style="margin-top:16px;width:300px">
          <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t3);margin-bottom:4px">
            <span id="btProgStage">初始化...</span>
            <span id="btProgPct">0%</span>
          </div>
          <div style="height:8px;background:var(--b1);border-radius:4px;overflow:hidden">
            <div id="btProgFill" style="height:100%;width:0%;background:linear-gradient(90deg,var(--purple),var(--blue));border-radius:4px;transition:width .4s ease"></div>
          </div>
        </div>
      </div>`
    : `<div class="bt-ph"><span class="spin" style="font-size:34px;opacity:.5">⟳</span><p>Cerebro 逐日回测中...</p><span style="font-size:12px;color:var(--t4)">订单次日开盘成交 · 双边佣金计算中</span></div>`

  const payload={
    filename:      curStrat,
    codes:         codesVal || null,
    start_date:    document.getElementById('btS').value,
    end_date:      document.getElementById('btE').value,
    capital:       capital,
    max_positions: +document.getElementById('btMaxP').value,
    stop_loss:     +document.getElementById('btSL').value/100,
    take_profit:   +document.getElementById('btTP').value/100,
  }

  let r=null
  try{ r=await api('/backtest','POST',payload) }catch(e){
    btn.disabled=false; btn.textContent='▶ 运行回测'
    document.getElementById('btSt').textContent='请求失败'
    document.getElementById('btResult').innerHTML=errBox('网络错误: '+e.message)
    return
  }

  // 全市场异步模式：轮询进度
  if(r?.async && r?.task_id){
    await _pollBtTask(r.task_id, btn, capital)
    return
  }

  // 同步模式（指定股票）
  btn.disabled=false; btn.textContent='▶ 运行回测'
  if(cancelBtn) cancelBtn.style.display='none'
  if(!r?.ok){
    document.getElementById('btSt').textContent='回测失败'
    document.getElementById('btResult').innerHTML=errBox(r?.error||'未知错误', r?.traceback, r?.logs)
    return
  }
  _btRenderDone(r, capital)
}

// 轮询异步回测任务进度
let _currentBtTaskId = null

async function cancelBt(){
  if(!_currentBtTaskId) return
  const btn = document.getElementById('btBtn')
  const cancelBtn = document.getElementById('btCancelBtn')
  cancelBtn.disabled = true; cancelBtn.textContent = '终止中...'
  await api('/backtest/cancel/'+_currentBtTaskId, 'POST', {})
  document.getElementById('btSt').textContent = '正在终止回测...'
}

async function _pollBtTask(taskId, btn, capital){
  _currentBtTaskId = taskId
  const cancelBtn = document.getElementById('btCancelBtn')
  let errCount=0
  while(true){
    await new Promise(res=>setTimeout(res, 1200))
    let t
    try{
      t = await api('/backtest/task/'+taskId)
      errCount = 0
    }catch(e){
      errCount++
      if(errCount>5){
        btn.disabled=false; btn.textContent='▶ 运行回测'
        if(cancelBtn){ cancelBtn.style.display='none'; cancelBtn.disabled=false; cancelBtn.textContent='⏹ 终止回测' }
        _currentBtTaskId = null
        document.getElementById('btSt').textContent='轮询超时'
        return
      }
      continue
    }

    // 更新进度条
    const prog = t?.progress||{}
    const fill = document.getElementById('btProgFill')
    const stg  = document.getElementById('btProgStage')
    const pct  = document.getElementById('btProgPct')
    const stEl = document.getElementById('btSt')
    if(fill && prog.total>0){
      const p = Math.max(0, Math.min(100, Math.round(prog.current/prog.total*100)))
      fill.style.width = p+'%'
      if(pct) pct.textContent = p+'%'
    }
    if(stg) stg.textContent = prog.stage||''
    if(stEl) stEl.textContent = `全市场回测中... ${prog.stage||''}`

    const _resetBtUI = ()=>{
      btn.disabled=false; btn.textContent='▶ 运行回测'
      if(cancelBtn){ cancelBtn.style.display='none'; cancelBtn.disabled=false; cancelBtn.textContent='⏹ 终止回测' }
      _currentBtTaskId = null
    }

    if(t?.status==='canceled'){
      _resetBtUI()
      document.getElementById('btSt').textContent='回测已终止'
      document.getElementById('btResult').innerHTML=`<div class="bt-ph"><span style="font-size:32px;opacity:.5">⏹</span><p style="color:var(--yl)">回测已被用户终止</p></div>`
      return
    }
    if(t?.status==='done' && t?.result){
      _resetBtUI()
      const r = t.result
      if(!r?.ok){
        document.getElementById('btSt').textContent='回测失败'
        document.getElementById('btResult').innerHTML=errBox(r?.error||'未知错误', r?.traceback, r?.logs)
        return
      }
      _btRenderDone(r, capital)
      return
    }
    if(t?.status==='error'){
      _resetBtUI()
      document.getElementById('btSt').textContent='回测失败'
      const r = t?.result||{}
      document.getElementById('btResult').innerHTML=errBox(r?.error||'回测异常', r?.traceback, r?.logs)
      return
    }
  }
}

// 渲染回测结果（同步/异步共用）
function _btRenderDone(r, capital){
  const sells  = (r.trades||[]).filter(t=>t.action==='sell')
  const src    = r.data_sources||{}
  const lc     = Object.values(src).filter(v=>v==='local').length
  const tdxCnt = Object.keys(src).length - lc
  const univ   = r.universe_size || Object.keys(src).length
  const fmTag  = r.full_market ? ` 全市场${univ}只` : ''
  document.getElementById('dsSt').textContent =
    `本地:${lc} TDX:${tdxCnt}${fmTag}`
  document.getElementById('btSt').textContent =
    `完成${fmTag} · ${sells.length}笔交易 · ${r.metrics?.open_positions||0}持仓 · 收益${r.metrics?.total_return||0}%`
  renderBtResults(r, capital)
}

function errBox(msg, tb='', logs=[]){
  return `<div style="padding:15px">
    <div style="background:#2d1a1e;border:1px solid var(--red);border-radius:8px;padding:13px;margin-bottom:10px">
      <div style="color:var(--rl);font-weight:700;font-size:14px;margin-bottom:5px">❌ 回测失败</div>
      <div style="color:#f0a0a0;font-size:13px">${esc(msg)}</div>
      ${tb?`<pre style="margin-top:8px;font-size:10px;color:var(--t3);overflow-x:auto;white-space:pre-wrap">${esc(tb)}</pre>`:''}
    </div>
    ${logs?.length?logBoxHtml(logs):''}
  </div>`
}

// ════════════════════════════════════════════════
// renderBtResults — Backtrader 风格结果渲染
// 各步 try-catch 隔离，任何一步失败不影响其他
// ════════════════════════════════════════════════
function renderBtResults(d, capital){
  const m    = d.metrics        || {}
  const eq   = d.equity_curve   || []
  const dd   = d.drawdown_series|| []
  const trades = d.trades       || []
  const sells  = trades.filter(t=>t.action==='sell')
  const buys   = trades.filter(t=>t.action==='buy')
  const wins   = sells.filter(t=>t.pnl_pct>0)
  const pnls   = d.pnl_dist     || []
  const hdays  = d.hold_days_dist|| sells.map(t=>t.hold_days||0)
  const logs   = d.logs         || []
  const names  = d.stock_names  || {}
  const codes  = Object.keys(d.data_sources||{})

  const sign = v => v==null?'—':(+v>0?'+':'')+v
  const cls  = v => +v>0?'pos':+v<0?'neg':'neu'
  const mcc  = v => +v>0?'pos-c':+v<0?'neg-c':'neu-c'
  const fmt  = (v,d=2) => v==null?'—':(+v).toFixed(d)

  // 警告
  const warns=[]
  if(buys.length>0 && sells.length===0)
    warns.push('所有买入持仓持有至回测结束，未触发止盈/止损。收益为期末浮盈，胜率/盈亏比统计为空。可扩大时间范围或调高止盈%。')
  if(buys.length===0)
    warns.push('策略在回测期间未产生任何买入信号。请检查：① 策略条件是否过严；② 数据是否覆盖回测区间；③ 资金是否充足（单槽资金 ÷ 股价 ≥ 100股）。')
  if(m.open_positions>0)
    warns.push(`回测结束仍有 ${m.open_positions} 只未平仓，其浮盈已计入权益曲线但不计入胜率。`)

  // ── Step 1: 注入骨架 ──
  document.getElementById('btResult').innerHTML = `
    <div class="mc-wrap">
      <div class="mc-row" id="mc-row1"></div>
      <div class="mc-row" id="mc-row2"></div>
      <div class="mc-row" id="mc-row3"></div>
    </div>
    <div id="warns-zone"></div>
    <div class="charts-wrap">
      <div class="sec-hd">权益曲线 &amp; 风险</div>
      <div class="chart-full" id="eq-card">
        <div class="chart-full-ttl">
          <span style="color:#22c55e">▲</span> 策略权益曲线
        </div>
        <div id="eq-sub"></div>
        <div class="chart-wrap" style="height:160px"><canvas id="cEq"></canvas></div>
      </div>
      <div class="chart-2col">
        <div class="chart-card"><div class="chart-card-ttl"><span class="chart-card-dot" style="background:#f97316"></span>回撤曲线</div><div class="chart-wrap" style="height:120px"><canvas id="cDd"></canvas></div></div>
        <div class="chart-card"><div class="chart-card-ttl"><span class="chart-card-dot" style="background:#4ade80"></span>盈亏分布</div><div class="chart-wrap" style="height:120px"><canvas id="cPnl"></canvas></div></div>
        <div class="chart-card"><div class="chart-card-ttl"><span class="chart-card-dot" style="background:#a78bfa"></span>胜负比例</div><div class="chart-wrap" style="height:120px"><canvas id="cWL"></canvas></div></div>
        <div class="chart-card"><div class="chart-card-ttl"><span class="chart-card-dot" style="background:#60a5fa"></span>持仓天数</div><div class="chart-wrap" style="height:120px"><canvas id="cHold"></canvas></div></div>
      </div>
    </div>
    <div class="monthly-wrap" id="monthly-zone"></div>
    <div class="sector-wrap" id="sector-zone"></div>
    <div class="trades-wrap">
      <div class="sec-hd">交易明细</div>
      <div class="trades-card">
        <div class="trades-hd-bar">
          <span>共 ${trades.length} 笔（买 ${buys.length} / 卖 ${sells.length} / 持仓 ${m.open_positions||0}）</span>
          <span style="font-size:11px;color:var(--t4)">总佣金 ¥${(m.total_commission||0).toLocaleString()}</span>
        </div>
        <div class="trades-scroll"><table class="tr-tbl">
          <thead><tr><th>日期</th><th>代码</th><th>名称</th><th>方向</th><th>价格</th><th>数量</th><th>盈亏%</th><th>持天</th></tr></thead>
          <tbody id="tradesTbody"></tbody>
        </table></div>
      </div>
    </div>
    <div class="log-wrap">
      <div class="sec-hd">Cerebro 运行日志</div>
      <div class="log-card">
        <div class="log-hd" data-ui-click="toggleLog(this)">
          <span>共 ${logs.length} 条 — Backtrader 架构逐日模拟</span><span id="logToggle">▼ 展开</span>
        </div>
        <div id="btLogBody" style="display:none">${logBoxHtml(logs)}</div>
      </div>
    </div>`

  // ── Step 2: 指标卡第1行（核心收益）──
  try{
    document.getElementById('mc-row1').innerHTML=`
      <div class="mc ${mcc(m.total_return)}">
        <div class="mc-lbl">总收益率</div>
        <div class="mc-val ${cls(m.total_return)}">${sign(m.total_return)}%</div>
        <div class="mc-sub">期末 ¥${(m.final_equity||0).toLocaleString('zh-CN',{maximumFractionDigits:0})}</div>
      </div>
      <div class="mc ${mcc(m.annual_return)}">
        <div class="mc-lbl">年化收益</div>
        <div class="mc-val ${cls(m.annual_return)}">${sign(m.annual_return)}%</div>
        <div class="mc-sub">${m.total_days||0} 交易日</div>
      </div>
      <div class="mc neg-c">
        <div class="mc-lbl">最大回撤</div>
        <div class="mc-val neg">-${fmt(m.max_drawdown)}%</div>
        <div class="mc-sub">Calmar=${fmt(m.calmar)}</div>
      </div>`
  }catch(e){console.error('mc-row1:',e)}

  // ── Step 3: 指标卡第2行（风险调整）──
  try{
    document.getElementById('mc-row2').innerHTML=`
      <div class="mc ${(m.sharpe||0)>=1?'pos-c':(m.sharpe||0)<0?'neg-c':'neu-c'}">
        <div class="mc-lbl">Sharpe 比率</div>
        <div class="mc-val ${(m.sharpe||0)>=1?'pos':(m.sharpe||0)<0?'neg':'neu'}">${fmt(m.sharpe)}</div>
        <div class="mc-sub">无风险 2.5%/年</div>
      </div>
      <div class="mc ${(m.win_rate||0)>=50?'pos-c':'neg-c'}">
        <div class="mc-lbl">胜率</div>
        <div class="mc-val ${(m.win_rate||0)>=50?'pos':'neg'}">${fmt(m.win_rate,1)}%</div>
        <div class="mc-sub">${wins.length}盈/${sells.length-wins.length}亏/${sells.length}笔</div>
      </div>
      <div class="mc neu-c">
        <div class="mc-lbl">盈亏比</div>
        <div class="mc-val neu">${fmt(m.profit_factor)}</div>
        <div class="mc-sub">均盈+${fmt(m.avg_win)}% 均亏${fmt(m.avg_loss)}%</div>
      </div>`
  }catch(e){console.error('mc-row2:',e)}

  // ── Step 4: 指标卡第3行（TradeAnalyzer 扩展指标）──
  try{
    document.getElementById('mc-row3').innerHTML=`
      <div class="mc neu-c">
        <div class="mc-lbl">平均持仓</div>
        <div class="mc-val neu">${fmt(m.avg_hold_days,1)}<span style="font-size:13px;font-weight:400">天</span></div>
        <div class="mc-sub">连胜${m.max_win_streak||0}/连败${m.max_loss_streak||0}</div>
      </div>
      <div class="mc ${(m.total_pnl||0)>=0?'pos-c':'neg-c'}">
        <div class="mc-lbl">总盈亏额</div>
        <div class="mc-val ${(m.total_pnl||0)>=0?'pos':'neg'}">¥${((m.total_pnl||0)/1000).toFixed(1)}k</div>
        <div class="mc-sub">含佣金扣除</div>
      </div>
      <div class="mc warn-c">
        <div class="mc-lbl">总佣金</div>
        <div class="mc-val warn">¥${(m.total_commission||0).toLocaleString('zh-CN',{maximumFractionDigits:0})}</div>
        <div class="mc-sub">A股双边（买0.03%+卖0.13%）</div>
      </div>`
  }catch(e){console.error('mc-row3:',e)}

  // ── Step 5: 警告栏 ──
  try{
    document.getElementById('warns-zone').innerHTML=
      warns.map(w=>`<div class="warn-bar">⚠ ${w}</div>`).join('')
  }catch(e){console.error('warns:',e)}

  // ── Step 6: 权益曲线（深色，渐变填充）──
  try{
    const labels  = eq.map(e=>e.date)
    const eqData  = eq.map(e=>e.equity)
    const isPos   = (eqData[eqData.length-1]||capital) >= capital
    const lineCol = isPos ? '#22c55e' : '#ef4444'
    const final   = eqData[eqData.length-1] || capital
    const ret     = ((final-capital)/capital*100).toFixed(2)
    const retSign = ret >= 0 ? '+' : ''

    // 统计行
    document.getElementById('eq-sub').innerHTML = `
      <div class="eq-stat-row">
        <div class="eq-stat"><span class="eq-stat-lbl">初始</span><span class="eq-stat-val" style="color:var(--t2)">¥${capital.toLocaleString()}</span></div>
        <div class="eq-stat"><span class="eq-stat-lbl">期末</span><span class="eq-stat-val ${isPos?'pos':'neg'}">¥${final.toLocaleString('zh-CN',{maximumFractionDigits:0})}</span></div>
        <div class="eq-stat"><span class="eq-stat-lbl">收益</span><span class="eq-stat-val ${isPos?'pos':'neg'}">${retSign}${ret}%</span></div>
      </div>`

    const eqCanvas = document.getElementById('cEq')
    const eqCtx = eqCanvas.getContext('2d')
    const grad = eqCtx.createLinearGradient(0, 0, 0, 170)
    if(isPos){
      grad.addColorStop(0, 'rgba(34,197,94,0.3)')
      grad.addColorStop(0.6, 'rgba(34,197,94,0.06)')
      grad.addColorStop(1, 'rgba(34,197,94,0)')
    } else {
      grad.addColorStop(0, 'rgba(239,68,68,0.3)')
      grad.addColorStop(0.6, 'rgba(239,68,68,0.06)')
      grad.addColorStop(1, 'rgba(239,68,68,0)')
    }

    charts.eq = new Chart(eqCanvas,{
      type:'line',
      data:{labels, datasets:[
        {label:'策略权益', data:eqData, borderColor:lineCol, backgroundColor:grad,
         fill:true, borderWidth:2, pointRadius:0, tension:0.3, order:1},
        {label:'基准', data:eqData.map(()=>capital),
         borderColor:'#374151', borderDash:[5,5], fill:false, borderWidth:1, pointRadius:0, order:2}
      ]},
      options:{responsive:true, maintainAspectRatio:false, animation:false,
        interaction:{mode:'index', intersect:false},
        plugins:{
          legend:{display:false},
          tooltip:{
            backgroundColor:'#1e2736', borderColor:'#30363d', borderWidth:1,
            titleColor:'#94a3b8', bodyColor:'#e2e8f0', padding:10, cornerRadius:8,
            callbacks:{
              label:ctx=>{
                const v=ctx.parsed.y
                const pct=((v-capital)/capital*100).toFixed(2)
                return `  ${ctx.dataset.label}: ¥${v.toLocaleString('zh-CN',{maximumFractionDigits:0})}  (${pct>=0?'+':''}${pct}%)`
              }
            }
          }
        },
        scales:{
          x:{ticks:{maxTicksLimit:8,color:'#475569',font:{size:10}},
             grid:{color:'#161b22',drawBorder:false}},
          y:{ticks:{color:'#475569',font:{size:10},
                    callback:v=>'¥'+(v>=10000?(v/10000).toFixed(1)+'万':v)},
             grid:{color:'#161b22',drawBorder:false},
             position:'right'}
        }}
    })
  }catch(e){console.error('权益曲线:',e); showChartErr('eq-card','渲染失败: '+e.message)}

  // ── Step 7: 回撤曲线 ──
  try{
    const ddCanvas=document.getElementById('cDd')
    const ddCtx=ddCanvas.getContext('2d')
    const ddGrad=ddCtx.createLinearGradient(0,0,0,120)
    ddGrad.addColorStop(0,'rgba(249,115,22,0.4)')
    ddGrad.addColorStop(1,'rgba(249,115,22,0)')
    charts.dd = new Chart(ddCanvas,{
      type:'line',
      data:{labels:dd.map(e=>e.date), datasets:[
        {label:'回撤', data:dd.map(e=>-e.drawdown), borderColor:'#f97316',
         backgroundColor:ddGrad, fill:true, borderWidth:1.5, pointRadius:0, tension:0.2}
      ]},
      options:{responsive:true, maintainAspectRatio:false, animation:false,
        interaction:{mode:'index', intersect:false},
        plugins:{
          legend:{display:false},
          tooltip:{backgroundColor:'#1e2736',borderColor:'#30363d',borderWidth:1,
            titleColor:'#94a3b8',bodyColor:'#fca5a5',padding:8,cornerRadius:6,
            callbacks:{label:ctx=>`  最大回撤: ${Math.abs(ctx.parsed.y).toFixed(2)}%`}}
        },
        scales:{
          x:{ticks:{maxTicksLimit:5,color:'#475569',font:{size:9}},grid:{color:'#161b22',drawBorder:false}},
          y:{ticks:{color:'#475569',font:{size:9},callback:v=>v+'%'},grid:{color:'#161b22',drawBorder:false},
             position:'right'}
        }}
    })
  }catch(e){console.error('回撤:',e)}

  // ── Step 8: 盈亏分布 ──
  try{
    const bins=buildBins(pnls,-25,25,5)
    charts.pnl = new Chart(document.getElementById('cPnl'),{
      type:'bar',
      data:{labels:bins.map(b=>b.lbl), datasets:[{
        label:'笔数', data:bins.map(b=>b.cnt),
        backgroundColor:bins.map(b=>b.c>=0?'rgba(74,222,128,0.75)':'rgba(248,113,113,0.75)'),
        borderColor:bins.map(b=>b.c>=0?'#4ade80':'#f87171'),
        borderWidth:1, borderRadius:3
      }]},
      options:{responsive:true, maintainAspectRatio:false, animation:false,
        plugins:{
          legend:{display:false},
          tooltip:{backgroundColor:'#1e2736',borderColor:'#30363d',borderWidth:1,
            titleColor:'#94a3b8',bodyColor:'#e2e8f0',padding:8,cornerRadius:6}
        },
        scales:{
          x:{ticks:{color:'#475569',font:{size:9}},grid:{display:false}},
          y:{ticks:{color:'#475569',font:{size:9},precision:0},grid:{color:'#161b22',drawBorder:false}}
        }}
    })
  }catch(e){console.error('盈亏分布:',e)}

  // ── Step 9: 胜负饼图（中间显示胜率）──
  try{
    const loseN=sells.filter(t=>t.pnl_pct<0).length
    const evenN=sells.filter(t=>t.pnl_pct===0).length
    const total=sells.length||1
    charts.wl = new Chart(document.getElementById('cWL'),{
      type:'doughnut',
      data:{labels:['盈利','亏损','平局'],
            datasets:[{data:[wins.length,loseN,evenN],
                       backgroundColor:['rgba(74,222,128,0.85)','rgba(248,113,113,0.85)','rgba(100,116,139,0.6)'],
                       borderColor:['#22c55e','#ef4444','#475569'],
                       borderWidth:2, hoverBorderWidth:3}]},
      options:{responsive:true, maintainAspectRatio:false, animation:{duration:400},
        cutout:'62%',
        plugins:{
          legend:{position:'bottom',labels:{color:'#94a3b8',font:{size:10},boxWidth:10,padding:8}},
          tooltip:{backgroundColor:'#1e2736',borderColor:'#30363d',borderWidth:1,
            titleColor:'#94a3b8',bodyColor:'#e2e8f0',padding:8,cornerRadius:6,
            callbacks:{label:c=>`  ${c.label}: ${c.parsed}笔 (${(c.parsed/total*100).toFixed(1)}%)`}}
        }}
    })
  }catch(e){console.error('胜负:',e)}

  // ── Step 10: 持仓天数 ──
  try{
    const hbins=buildBins(hdays,0,60,5,true)
    const maxCnt=Math.max(...hbins.map(b=>b.cnt),1)
    charts.hold = new Chart(document.getElementById('cHold'),{
      type:'bar',
      data:{labels:hbins.map(b=>b.lbl), datasets:[{
        label:'笔数', data:hbins.map(b=>b.cnt),
        backgroundColor:hbins.map(b=>`rgba(96,165,250,${0.35+b.cnt/maxCnt*0.5})`),
        borderColor:'#60a5fa', borderWidth:1, borderRadius:3
      }]},
      options:{responsive:true, maintainAspectRatio:false, animation:false,
        plugins:{
          legend:{display:false},
          tooltip:{backgroundColor:'#1e2736',borderColor:'#30363d',borderWidth:1,
            titleColor:'#94a3b8',bodyColor:'#e2e8f0',padding:8,cornerRadius:6}
        },
        scales:{
          x:{ticks:{color:'#475569',font:{size:9}},grid:{display:false}},
          y:{ticks:{color:'#475569',font:{size:9},precision:0},grid:{color:'#161b22',drawBorder:false}}
        }}
    })
  }catch(e){console.error('持仓天数:',e)}

  // ── Step 11: 月度热力图 ──
  try{renderMonthly(d.monthly_returns||{})}catch(e){console.error('月度:',e)}

  // ── Step 12: 板块信息 ──
  try{renderSectors(codes,names)}catch(e){console.error('板块:',e)}

  // ── Step 13: 交易明细 ──
  try{
    const tbody=document.getElementById('tradesTbody')
    if(!trades.length){
      tbody.innerHTML='<tr><td colspan="8" style="text-align:center;color:var(--t4);padding:14px">无交易记录</td></tr>'
    } else {
      tbody.innerHTML=trades.map(t=>{
        const isBuy=t.action==='buy'
        const pc=t.pnl_pct>0?'pos':t.pnl_pct<0?'neg':''
        return `<tr>
          <td style="color:var(--t4)">${t.date}</td>
          <td style="font-weight:700">${t.code}</td>
          <td style="color:var(--t2)">${names[t.code]||''}</td>
          <td class="${isBuy?'td-buy':'td-sell'}">${isBuy?'买入':'卖出'}</td>
          <td>¥${t.price}</td>
          <td style="color:var(--t3)">${t.qty}股</td>
          <td class="${pc}">${isBuy?'—':(t.pnl_pct>0?'+':'')+t.pnl_pct+'%'}</td>
          <td style="color:var(--t4)">${t.hold_days||0}</td>
        </tr>`
      }).join('')
    }
  }catch(e){console.error('明细:',e)}
}

function showChartErr(cardId, msg){
  const card=document.getElementById(cardId)
  if(card) card.insertAdjacentHTML('beforeend',`<div class="chart-err">${msg}</div>`)
}

function renderMonthly(mr){
  const zone=document.getElementById('monthly-zone')
  const years=Object.keys(mr).sort()
  if(!years.length){zone.innerHTML='';return}
  const months=['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月']
  let html=`<div class="sec-hd">月度收益热力图</div>
    <div class="monthly-card"><div class="monthly-ttl">📅 月度收益率（%）</div>
    <table class="mt-tbl"><thead><tr><th style="text-align:left">年份</th>`
  months.forEach(m=>html+=`<th>${m}</th>`)
  html+='<th>全年</th></tr></thead><tbody>'
  years.forEach(yr=>{
    html+=`<tr><td class="mt-yr">${yr}</td>`
    let sum=0
    months.forEach((_,idx)=>{
      const m=String(idx+1)
      const v=mr[yr]?.[m]
      if(v!=null){
        const intensity=Math.min(Math.abs(v)/20,1)
        const bg=v>=0
          ?`rgba(34,197,94,${0.12+intensity*0.55})`
          :`rgba(239,68,68,${0.12+intensity*0.55})`
        const tc=Math.abs(v)>10?'#f1f5f9':v>=0?'#86efac':'#fca5a5'
        html+=`<td style="background:${bg};color:${tc}" title="${yr}年${m}月: ${v>=0?'+':''}${v}%">${v>=0?'+':''}${v}</td>`
        sum+=v
      } else html+=`<td style="color:#1e2736">·</td>`
    })
    const sc=sum>=0?'#86efac':'#fca5a5'
    html+=`<td style="color:${sc};font-weight:800;font-size:11px">${sum>=0?'+':''}${sum.toFixed(1)}</td></tr>`
  })
  html+='</tbody></table></div>'
  zone.innerHTML=html
}

function renderSectors(codes, names){
  const zone=document.getElementById('sector-zone')
  if(!codes.length){zone.innerHTML='';return}
  let html=`<div class="sec-hd">回测标的信息</div><div class="sector-card"><div class="sector-ttl">股票概念板块</div>`
  codes.forEach(code=>{
    const nm=names[code]||code
    html+=`<div class="sector-row">
      <span class="sector-code">${code}</span>
      <span class="sector-name">${nm}</span>
      <span class="sector-tags" id="st-${code}"><span class="loading-tag">查询中...</span></span>
    </div>`
  })
  html+='</div>'
  zone.innerHTML=html
  // 异步加载板块
  codes.forEach(code=>{
    fetch(`${API}/data/sector/${code}`)
      .then(r=>r.json())
      .then(d=>{
        const el=document.getElementById('st-'+code)
        if(!el) return
        let tags=''
        if(d.industry) tags+=`<span class="sector-ind">📊 ${d.industry}</span>`
        if(d.concepts?.length) tags+=d.concepts.map(c=>`<span class="sector-tag">${c}</span>`).join('')
        if(!tags) tags='<span class="loading-tag" style="color:var(--t4)">暂无板块信息</span>'
        el.innerHTML=tags
      }).catch(()=>{
        const el=document.getElementById('st-'+code)
        if(el) el.innerHTML='<span class="loading-tag" style="color:var(--t4)">查询失败</span>'
      })
  })
}

function buildBins(data, lo, hi, step, isDay=false){
  const bins=[]
  for(let i=lo;i<hi;i+=step){
    const cnt=data.filter(v=>v>=i&&v<i+step).length
    const lbl=isDay?`${i}~${i+step}天`:`${i>0?'+':''}${i}%`
    bins.push({lbl, c:i+step/2, cnt})
  }
  return bins
}

function logBoxHtml(logs){
  if(!logs?.length) return '<div class="empty" style="height:40px">无日志</div>'
  return '<div class="log-scroll">'+logs.map(l=>`
    <div class="log-line ll-${(l.level||'INFO').toLowerCase()}">
      <span class="ll-lv">[${l.level||'INFO'}]</span>
      <span class="ll-msg">${esc(l.msg)}</span>
    </div>`).join('')+'</div>'
}

function toggleLog(hd){
  const body=document.getElementById('btLogBody'), tog=document.getElementById('logToggle')
  if(!body) return
  if(body.style.display==='none'){body.style.display='';if(tog)tog.textContent='▲ 收起'}
  else{body.style.display='none';if(tog)tog.textContent='▼ 展开'}
}

function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') }
