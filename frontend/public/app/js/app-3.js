
// ══ 组合优化 ═════════════════════════════════════════════════
const OPT_MODES = {
  equal_weight: { label:'等权重', icon:'⚖', desc:'每只标的等额持仓，最简单直接' },
  max_sharpe:   { label:'最大夏普', icon:'📈', desc:'历史数据最优风险收益比（需30日+数据）' },
  min_variance: { label:'最小波动', icon:'🛡', desc:'最小化组合波动率，稳健型' },
  risk_parity:  { label:'风险平价', icon:'⚡', desc:'各标的风险贡献相等（桥水风格）' },
}
let _optMode = 'equal_weight'

function renderOptModeGrid(){
  const box = document.getElementById('optModeGrid')
  box.innerHTML = Object.entries(OPT_MODES).map(([k,v])=>`
    <div onclick="selectOptMode('${k}')" style="
      padding:9px 10px;border-radius:8px;cursor:pointer;border:1px solid ${k===_optMode?'var(--blue)':'var(--b1)'};
      background:${k===_optMode?'rgba(31,111,235,.12)':'var(--card)'};transition:.15s">
      <div style="font-size:13px;margin-bottom:2px">${v.icon} <b style="color:${k===_optMode?'var(--bl)':'var(--t1)'}">${v.label}</b></div>
      <div style="font-size:10px;color:var(--t3)">${v.desc}</div>
    </div>`).join('')
}

function selectOptMode(m){ _optMode=m; renderOptModeGrid() }

function openPortfolioOpt(){
  if(!scResults||!scResults.length){ alert('请先运行选股，获得结果后再优化'); return }
  renderOptModeGrid()
  document.getElementById('optResult').innerHTML=''
  document.getElementById('optStatus').textContent=`待优化标的：${scResults.length} 只`
  document.getElementById('optOverlay').classList.add('show')
}
function closeOpt(){ document.getElementById('optOverlay').classList.remove('show') }

async function runPortfolioOpt(){
  const codes = scResults.map(r=>r.code).filter(Boolean)
  if(codes.length<2){ alert('至少需要2只股票'); return }
  const capital = parseFloat(document.getElementById('optCapital').value)||100000
  const st = document.getElementById('optStatus')
  const res = document.getElementById('optResult')
  st.style.color='var(--bl)'; st.textContent='⟳ 计算中，正在获取历史数据...'
  res.innerHTML=''
  const r = await api('/portfolio/optimize','POST',{codes, mode:_optMode, capital})
  if(!r||!r.ok){
    st.style.color='var(--rl)'; st.textContent='❌ '+(r?.error||'优化失败')
    return
  }
  const weights   = r.weights || {}
  // 兼容两种后端格式：list of {code,weight,price,shares,budget} 或 dict {code: shares}
  let positions = []
  if (Array.isArray(r.positions)) {
    positions = r.positions
  } else if (r.positions && typeof r.positions === 'object') {
    positions = Object.entries(r.positions).map(([code, shares])=>({code, shares}))
  }
  // 后端字段：expected_return / expected_volatility 已经是百分数（× 100 round 过的）
  const expRet = r.expected_return
  const expVol = r.expected_volatility
  const sharpe = (expVol && expVol > 0 && expRet != null) ? (expRet / expVol) : 0
  const info = `预期年化: ${expRet != null ? expRet.toFixed(2)+'%' : '—'}  年化波动: ${expVol != null ? expVol.toFixed(2)+'%' : '—'}  夏普: ${sharpe.toFixed(2)}`
  st.style.color='var(--gl)'; st.textContent=`✅ 优化完成（${OPT_MODES[_optMode]?.label||_optMode}）  ${info}`

  // 渲染持仓表格 — 按权重降序
  const rows = positions
    .slice()
    .sort((a,b)=>(b.weight||0)-(a.weight||0))
    .map(p=>{
      const code   = p.code
      const stk    = scResults.find(s=>s.code===code) || {}
      // weight: 后端是小数(0~1)，× 100 显示百分比
      const w      = ((p.weight != null ? p.weight : weights[code] || 0)*100).toFixed(1)
      // price 优先取后端，没有就用实时选股价
      const price  = (+p.price > 0 ? +p.price : +stk.price) || 0
      const shares = +p.shares || 0
      const val    = price * shares
      const name   = stk.name || p.name || code
      return `<tr>
        <td style="font-family:monospace;font-weight:700;color:var(--t1)">${code}</td>
        <td style="color:var(--t2)">${name}</td>
        <td style="text-align:right;color:var(--bl)">${w}%</td>
        <td style="text-align:right">${price.toFixed(2)}</td>
        <td style="text-align:right;font-weight:700">${shares}</td>
        <td style="text-align:right;color:var(--gl)">¥${Number(val.toFixed(0)).toLocaleString()}</td>
      </tr>`
    }).join('')

  res.innerHTML=`<table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead><tr style="color:var(--t3);font-size:11px">
      <th style="text-align:left;padding:5px 6px">代码</th>
      <th style="text-align:left;padding:5px 6px">名称</th>
      <th style="text-align:right;padding:5px 6px">权重</th>
      <th style="text-align:right;padding:5px 6px">现价</th>
      <th style="text-align:right;padding:5px 6px">股数</th>
      <th style="text-align:right;padding:5px 6px">市值</th>
    </tr></thead>
    <tbody>${rows}</tbody>
    <tfoot><tr style="border-top:1px solid var(--b1);font-size:11px;color:var(--t3)">
      <td colspan="5" style="padding:5px 6px">总计投入</td>
      <td style="text-align:right;padding:5px 6px;color:var(--t2);font-weight:700">
        ¥${positions.reduce((s,p)=>{
          const price = (+p.price > 0 ? +p.price : +(scResults.find(r=>r.code===p.code)||{}).price) || 0
          return s + price * (+p.shares||0)
        },0).toLocaleString('zh-CN',{maximumFractionDigits:0})}
      </td>
    </tr></tfoot>
  </table>`
}
document.addEventListener('keydown', e=>{ if(e.key==='Escape'&&document.getElementById('optOverlay').classList.contains('show')) closeOpt() })
