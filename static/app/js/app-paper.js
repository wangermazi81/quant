// 模拟盘与账户设置条模块：依赖 app-core.js 的 api/toast/状态函数。
// ═══════════════════════════════════════════════════
// 模拟盘手动买入弹窗
// ═══════════════════════════════════════════════════
function openPaperBuy(code='', name='', price=''){
  document.getElementById('pbCode').value  = code
  document.getElementById('pbName').value  = name
  document.getElementById('pbPrice').value = price
  document.getElementById('pbShares').value= ''
  document.getElementById('pbPreview').textContent = ''
  document.getElementById('pbSubmitBtn').disabled = false
  document.getElementById('paperBuyOverlay').style.display='flex'
  setTimeout(()=>document.getElementById('pbCode').focus(), 100)
  if(price) pbCalcShares()
}
function closePaperBuy(){
  document.getElementById('paperBuyOverlay').style.display='none'
}

async function pbAutoFill(){
  const code = document.getElementById('pbCode').value.trim()
  if(code.length !== 6) return
  // 尝试获取实时价格和名称
  try{
    const r = await api('/kline/'+code)
    if(r?.ok && r.name){
      document.getElementById('pbName').value = r.name
    }
    // 尝试从 AI 分析获取实时价
    const qs = await api('/paper/positions')
    // 从监控池查找
    const poolItem = (_lastPool||[]).find(x=>x.code===code)
    if(poolItem){
      if(!document.getElementById('pbName').value) document.getElementById('pbName').value = poolItem.name||''
      if(!document.getElementById('pbPrice').value) document.getElementById('pbPrice').value = poolItem.price||''
      pbCalcShares()
    }
  }catch(e){}
}

function pbCalcShares(){
  const price = parseFloat(document.getElementById('pbPrice').value)
  if(!price || price<=0){ document.getElementById('pbPreview').textContent=''; return }
  // 从状态读取资金和仓位比例
  const capEl = document.getElementById('nCapital')
  const capStr = capEl?.textContent?.replace(/[¥,]/g,'') || '1000000'
  const cap = parseFloat(capStr) || 1000000
  const ratio = 0.20  // 默认20%仓位
  const budget = cap * ratio
  const shares = Math.max(100, Math.floor(budget / price / 100) * 100)
  document.getElementById('pbShares').value = shares
  const total = (price * shares).toLocaleString('zh-CN', {maximumFractionDigits:0})
  document.getElementById('pbPreview').textContent =
    `预计花费 ¥${total}（${shares}股 × ¥${price}）+ 佣金`
}

async function doPaperBuy(){
  const code   = document.getElementById('pbCode').value.trim()
  const name   = document.getElementById('pbName').value.trim() || code
  const priceV = document.getElementById('pbPrice').value
  const sharesV= document.getElementById('pbShares').value

  if(!code || code.length!==6){ alert('请输入6位股票代码'); return }

  let price = parseFloat(priceV)
  // 若未填价格，先从监控池/选股结果拿实时价
  if(!price){
    const poolItem = (_lastPool||[]).find(x=>x.code===code)
    if(poolItem?.price) price = poolItem.price
    if(!price){ alert('请填写买入价格（或先在监控中心启动扫描获取实时行情）'); return }
  }

  const shares = parseInt(sharesV) || 0

  const btn = document.getElementById('pbSubmitBtn')
  btn.disabled = true; btn.textContent = '提交中...'

  const r = await api('/paper/buy','POST',{ code, name, price, shares })
  btn.disabled = false; btn.textContent = '✓ 确认买入'

  if(r?.ok){
    closePaperBuy()
    alert(`✅ 模拟买入成功！\n${code} ${name}\n${r.shares}股 @ ¥${price}`)
    loadPaperPage(); fetchFunds()
  } else {
    alert('❌ 买入失败：' + (r?.msg || '未知错误'))
  }
}

// ═══════════════════════════════════════════════════
// 模拟盘 (Paper Trading) 功能
// ═══════════════════════════════════════════════════
let _equityChart = null
let _paperLoaded = false

async function loadPaperPage(){
  _paperLoaded = true
  await Promise.all([loadPaperStats(), loadPaperPositions(), loadPaperTrades(), loadPaperEquity()])
}

async function loadPaperStats(){
  const d = await api('/paper/stats'); if(!d) return
  const fmt = n => (n==null?'—':isNaN(n)?n:n)
  const pct = n => n==null?'—':`${n>=0?'+':''}${(+n).toFixed(2)}%`
  const money = n => n==null?'—':'¥'+(+n).toLocaleString('zh-CN',{maximumFractionDigits:0})

  document.getElementById('ps-equity').textContent   = money(d.equity)
  const ret = d.total_return||0
  document.getElementById('ps-equity').style.color   = ret>=0?'var(--rl)':'var(--gl)'
  document.getElementById('ps-return').textContent   = `总收益 ${pct(d.total_return)}`
  document.getElementById('ps-cash').textContent     = money(d.cash)
  document.getElementById('ps-mval').textContent     = `持仓市值 ${money(d.market_value)}`
  document.getElementById('ps-winrate').textContent  = d.win_rate!=null?`${(+d.win_rate).toFixed(1)}%`:'—'
  document.getElementById('ps-trades').textContent   = `共 ${d.trade_count||0} 笔成交`
  document.getElementById('ps-pf').textContent       = d.profit_factor!=null?(+d.profit_factor).toFixed(2):'—'
  document.getElementById('ps-avgpnl').textContent   = d.avg_pnl!=null?`均盈亏 ¥${(+d.avg_pnl).toFixed(0)}`:'—'
  const dd = d.max_drawdown||0
  document.getElementById('ps-dd').textContent       = `${(+dd).toFixed(2)}%`
  document.getElementById('ps-dd').style.color       = dd<0?'var(--rl)':'var(--t2)'
  const sharpe = d.sharpe!=null?(+d.sharpe).toFixed(2):'—'
  document.getElementById('ps-sharpe').textContent   = `Sharpe ${sharpe}`
  document.getElementById('ps-poscount').textContent = `${d.position_count||0}只`
}

async function loadPaperPositions(){
  const rows = await api('/paper/positions'); if(!Array.isArray(rows)) return
  const el = document.getElementById('paperPositionsBody')
  if(!rows.length){ el.innerHTML='<div class="empty">暂无持仓</div>'; return }
  el.innerHTML = `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>股数</th>
        <th>成本</th><th>现价</th><th>盈亏%</th><th>持仓天</th><th>操作</th>
      </tr></thead>
      <tbody>${rows.map(r=>{
        const up = r.pnl_pct>=0
        const nm = (r.name||r.code).replace(/'/g,"\\'")
        // 整行可点击查看 K 线图（除操作列）
        return `<tr style="cursor:pointer" data-ui-click="showKLine('${r.code}','${nm}')">
          <td><b>${r.code}</b></td>
          <td style="color:var(--t2)">${r.name||r.code}</td>
          <td>${r.shares}</td>
          <td style="color:var(--t3)">${(+r.cost_price).toFixed(2)}</td>
          <td><b>${(+r.cur_price).toFixed(2)}</b></td>
          <td style="color:${up?'var(--rl)':'var(--gl)'};font-weight:700">${up?'+':''}${(+r.pnl_pct).toFixed(2)}%</td>
          <td style="color:var(--t3)">${r.hold_days||0}天</td>
          <td data-ui-click="event.stopPropagation()"><button class="btn btn-red btn-xs" data-ui-click="paperSell('${r.code}','${nm}',${r.cur_price})">卖出</button></td>
        </tr>`}).join('')}
      </tbody>
    </table>`
}

async function loadPaperTrades(){
  const rows = await api('/paper/trades'); if(!Array.isArray(rows)) return
  const el = document.getElementById('paperTradesBody')
  if(!rows.length){ el.innerHTML='<div class="empty">暂无成交</div>'; return }
  // 只显示已平仓（sell_time 不为空）的记录
  const closed = rows.filter(r=>r.sell_time)
  if(!closed.length){ el.innerHTML='<div class="empty">暂无已平仓成交</div>'; return }
  el.innerHTML = `
    <table>
      <thead><tr>
        <th>代码</th><th>名称</th><th>买入价</th><th>卖出价</th>
        <th>股数</th><th>盈亏</th><th>盈亏%</th><th>持仓</th><th>原因</th>
      </tr></thead>
      <tbody>${closed.map(r=>{
        const up=(r.pnl||0)>=0
        return `<tr>
          <td><b>${r.code}</b></td>
          <td style="color:var(--t2)">${r.name||''}</td>
          <td style="color:var(--t3)">${(+r.buy_price).toFixed(2)}</td>
          <td>${r.sell_price?(+r.sell_price).toFixed(2):'—'}</td>
          <td>${r.shares}</td>
          <td style="color:${up?'var(--rl)':'var(--gl)'};font-weight:700">${up?'+':''}¥${(+(r.pnl||0)).toFixed(0)}</td>
          <td style="color:${up?'var(--rl)':'var(--gl)'}">${up?'+':''}${(+(r.pnl_pct||0)).toFixed(2)}%</td>
          <td style="color:var(--t3)">${r.hold_days||0}天</td>
          <td style="color:var(--t4);font-size:11px">${r.sell_reason||''}</td>
        </tr>`}).join('')}
      </tbody>
    </table>`
}

async function loadPaperOrders(){
  const rows = await api('/paper/orders'); if(!Array.isArray(rows)) return
  const el = document.getElementById('paperOrdersBody')
  if(!rows.length){ el.innerHTML='<div class="empty">暂无委托</div>'; return }
  el.innerHTML = `
    <table>
      <thead><tr>
        <th>时间</th><th>代码</th><th>方向</th><th>信号价</th>
        <th>成交价</th><th>股数</th><th>佣金</th><th>状态</th>
      </tr></thead>
      <tbody>${rows.map(r=>`<tr>
        <td style="color:var(--t3);font-size:11px">${r.order_time?.slice(11,19)||''}</td>
        <td><b>${r.code}</b> <span style="color:var(--t3)">${r.name||''}</span></td>
        <td style="color:${r.direction==='buy'?'var(--rl)':'var(--gl)'}">
          ${r.direction==='buy'?'买入':'卖出'}</td>
        <td style="color:var(--t3)">${(+r.signal_price).toFixed(2)}</td>
        <td><b>${(+r.exec_price).toFixed(2)}</b></td>
        <td>${r.shares}</td>
        <td style="color:var(--t3)">¥${(+(r.commission||0)).toFixed(1)}${r.tax?'+税¥'+(+r.tax).toFixed(1):''}</td>
        <td><span class="sbadge" style="background:${r.status==='filled'?'#1a4731':'#4a1a1a'};
          color:${r.status==='filled'?'var(--gl)':'var(--rl)'}">
          ${r.status==='filled'?'成交':'拒绝'}</span></td>
      </tr>`).join('')}
      </tbody>
    </table>`
}

async function loadPaperEquity(){
  const rows = await api('/paper/equity?days=90')
  if(!Array.isArray(rows)||!rows.length){
    // 重置后净值表为空：必须销毁旧图，否则画布上一直留着重置前的曲线
    if(_equityChart){ _equityChart.destroy(); _equityChart = null }
    const cv = document.getElementById('equityChart')
    if(cv){ const c = cv.getContext('2d'); c.clearRect(0,0,cv.width,cv.height) }
    return
  }
  const labels = rows.map(r=>r.snap_date?.slice(5))
  const data   = rows.map(r=>r.equity)
  const ctx    = document.getElementById('equityChart').getContext('2d')
  if(_equityChart) _equityChart.destroy()
  _equityChart = new Chart(ctx,{
    type:'line',
    data:{labels,datasets:[{
      label:'净资产',data,borderColor:'#FF7B93',backgroundColor:'rgba(255,123,147,0.12)',
      borderWidth:2,pointRadius:0,fill:true,tension:0.3
    }]},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:false},tooltip:{
        callbacks:{label:c=>'¥'+c.parsed.y.toLocaleString('zh-CN',{maximumFractionDigits:0})}
      }},
      scales:{
        x:{ticks:{color:'#9C8B7A',font:{size:10}},grid:{display:false},border:{display:false}},
        y:{ticks:{color:'#9C8B7A',font:{size:10},
             callback:v=>'¥'+(v/1000).toFixed(0)+'k'},grid:{display:false},border:{display:false}}
      }
    }
  })
}

function switchPTab(name, el){
  document.querySelectorAll('.ptab').forEach(t=>t.classList.remove('active'))
  el.classList.add('active')
  document.getElementById('paperTradesBody').style.display = name==='trades'?'':'none'
  document.getElementById('paperOrdersBody').style.display = name==='orders'?'':'none'
  if(name==='orders') loadPaperOrders()
}

async function paperSell(code, name, price){
  if(!confirm(`确认卖出模拟盘 ${code} ${name} @ ${(+price).toFixed(2)}？`)) return
  const r = await api('/paper/sell','POST',{code, price})
  if(r?.ok){
    const pnl = r.pnl||0
    alert(`卖出成功！盈亏: ${pnl>=0?'+':''}¥${pnl.toFixed(2)} (${(r.pnl_pct||0).toFixed(2)}%)`)
    loadPaperPage()
  } else alert('卖出失败: '+(r?.msg||''))
}

async function resetPaper(){
  const cap = prompt('重置模拟盘——输入初始资金（元）：','1000000')
  if(!cap||isNaN(+cap)||+cap<10000) return
  if(!confirm(`确认重置模拟盘？\n初始资金：¥${(+cap).toLocaleString()}\n\n⚠️ 所有成交、持仓、净值记录将被清空！`)) return
  const r = await api('/paper/reset','POST',{initial_capital:+cap})
  if(r?.ok){ alert('模拟盘已重置！'); loadPaperPage() }
  else alert('重置失败')
}


// ══ 布局折叠：执行队列的账户设置条（工单7b）══════════════════════
// 资金/仓位/护栏属于低频配置，默认收起给队列腾空间；用户选择记忆在 localStorage。
function toggleAcctBar(force){
  const bar = document.getElementById('acctBar');
  const btn = document.getElementById('acctBarBtn');
  if(!bar) return;
  const show = (force !== undefined) ? force : bar.style.display === 'none';
  bar.style.display = show ? 'flex' : 'none';
  if(btn) btn.classList.toggle('btn-blue', show);
  try{ localStorage.setItem('jw_acct_bar', show ? '1' : '0'); }catch(e){}
}
try{ toggleAcctBar(localStorage.getItem('jw_acct_bar') === '1'); }catch(e){ }
