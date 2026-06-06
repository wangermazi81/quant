
// ══ 日报页 ══════════════════════════════════════════════════════

let _dailyInited = false

async function initDailyPage(){
  if(_dailyInited) return
  _dailyInited = true
  loadMarketReview()
}

// ── 大盘复盘 ───────────────────────────────────────────────────
async function loadMarketReview(){
  const box = document.getElementById('market-review-body')
  box.innerHTML = '<div class="empty-hint">加载中…（获取实时行情，请稍候）</div>'
  try {
    const ctrl = new AbortController()
    const tid  = setTimeout(()=>ctrl.abort(), 25000)
    const r    = await fetch('/api/market/review', {signal: ctrl.signal})
    clearTimeout(tid)
    const d    = await r.json()
    if(!d.ok) throw new Error(d.error || '请求失败')
    box.innerHTML = _renderMarketReview(d)
  } catch(e) {
    const msg = e.name==='AbortError' ? '请求超时（25s），请稍后重试' : e.message
    box.innerHTML = `<div class="empty-hint" style="color:#f85149">获取失败：${msg}</div>`
  }
}

function _renderMarketReview(d){
  let html = `<div style="font-size:12px;color:var(--t3);margin-bottom:12px">${d.date} ${d.time} 更新</div>`

  // 指数
  const indices = d.indices || {}
  if(Object.keys(indices).length){
    html += `<div class="daily-card" style="margin-bottom:12px">`
    html += `<div style="font-size:12px;color:var(--t3);margin-bottom:8px;font-weight:600">主要指数</div>`
    for(const [name, idx] of Object.entries(indices)){
      const up   = idx.change >= 0
      const chgTxt = (up ? '+' : '') + idx.change + '%'
      html += `<div class="mr-index-row">
        <span class="mr-idx-name">${name}</span>
        <span class="mr-idx-price">${idx.price}</span>
        <span class="mr-idx-chg ${up?'up':'dn'}">${chgTxt}</span>
      </div>`
    }
    html += `</div>`
  }

  // 市场宽度
  const b = d.breadth || {}
  if(b.total){
    html += `<div class="daily-card" style="margin-bottom:12px">`
    html += `<div style="font-size:12px;color:var(--t3);margin-bottom:8px;font-weight:600">市场概况</div>`
    html += `<div class="mr-breadth">
      <div class="mr-stat"><div class="mr-stat-val" style="color:#3fb950">${b.up||0}</div><div class="mr-stat-lbl">上涨</div></div>
      <div class="mr-stat"><div class="mr-stat-val" style="color:#f85149">${b.down||0}</div><div class="mr-stat-lbl">下跌</div></div>
      <div class="mr-stat"><div class="mr-stat-val" style="color:#3fb950">${b.limit_up||0}</div><div class="mr-stat-lbl">涨停</div></div>
      <div class="mr-stat"><div class="mr-stat-val" style="color:#f85149">${b.limit_dn||0}</div><div class="mr-stat-lbl">跌停</div></div>
    </div>
    <div style="font-size:11px;color:var(--t3);text-align:right">共 ${b.total} 只</div>`
    html += `</div>`
  }

  // 板块
  const sec = d.sectors || {}
  if((sec.top_gainers||[]).length || (sec.top_losers||[]).length){
    html += `<div class="daily-card">`
    html += `<div style="font-size:12px;color:var(--t3);margin-bottom:8px;font-weight:600">板块表现</div>`
    if((sec.top_gainers||[]).length){
      html += `<div style="margin-bottom:6px"><span style="font-size:12px;color:var(--t2)">领涨：</span>`
      sec.top_gainers.forEach(s => html += `<span class="mr-sector-tag up">${s}</span>`)
      html += `</div>`
    }
    if((sec.top_losers||[]).length){
      html += `<div><span style="font-size:12px;color:var(--t2)">领跌：</span>`
      sec.top_losers.forEach(s => html += `<span class="mr-sector-tag dn">${s}</span>`)
      html += `</div>`
    }
    html += `</div>`
  }

  if(!Object.keys(indices).length && !b.total)
    html += `<div class="empty-hint">暂无数据（TDX未连接且akshare请求失败）</div>`

  return html
}

// ── 决策仪表盘 ─────────────────────────────────────────────────
async function generateDashboard(){
  const raw    = document.getElementById('dash-codes-input').value
  const withAI = document.getElementById('dash-with-ai').checked
  const codes  = raw.split(/[\s,，]+/).map(c=>c.trim()).filter(c=>c)
  if(!codes.length){ alert('请输入股票代码'); return }
  if(codes.length > 20){ alert('最多20只股票'); return }

  const box = document.getElementById('dash-report-body')
  box.innerHTML = `<div class="empty-hint">生成中，共 ${codes.length} 只股票…</div>`

  try {
    const r = await api('/dashboard/generate', 'POST', {codes, with_ai: withAI})
    if(!r) throw new Error('请求失败（可能需要先登录）')
    if(!r.ok) throw new Error(r.error || '生成失败')
    box.innerHTML = _renderDashboard(r)
  } catch(e) {
    box.innerHTML = `<div class="empty-hint" style="color:#f85149">生成失败：${e.message}</div>`
  }
}

function dashFromQueue(){
  // 从策略扫描队列导入
  const items = document.querySelectorAll('.queue-code, [data-code]')
  const codes = []
  items.forEach(el => {
    const c = el.dataset.code || el.textContent.match(/\d{6}/)?.[0]
    if(c && !codes.includes(c)) codes.push(c)
  })
  if(codes.length){
    document.getElementById('dash-codes-input').value = codes.join(',')
  } else {
    // 也尝试从 STOCK_POOL 显示区读取
    const poolEl = document.getElementById('poolDisplay')
    if(poolEl){
      const m = poolEl.textContent.match(/\d{6}/g) || []
      document.getElementById('dash-codes-input').value = m.slice(0,20).join(',')
    }
  }
}

function _renderDashboard(d){
  const sigColor = {'强烈买入':'#3fb950','买入':'#58a6ff','持有':'#e3b341','观望':'#bd561d','卖出':'#f85149'}
  const sentColor = {'利好':'#3fb950','中性':'#e3b341','利空':'#f85149'}

  let html = `<div style="font-size:12px;color:var(--t3);margin-bottom:12px">${d.date} ${d.time} 生成</div>`

  // 汇总栏
  html += `<div class="dash-summary">
    <div class="dash-sum-item"><div class="dash-sum-val">${d.total}</div><div class="dash-sum-lbl">分析股票</div></div>
    <div style="width:1px;background:var(--b1);align-self:stretch"></div>
    <div class="dash-sum-item"><div class="dash-sum-val" style="color:#3fb950">${d.buy}</div><div class="dash-sum-lbl">买入</div></div>
    <div class="dash-sum-item"><div class="dash-sum-val" style="color:#e3b341">${d.hold||0}</div><div class="dash-sum-lbl">持有</div></div>
    <div class="dash-sum-item"><div class="dash-sum-val" style="color:#bd561d">${d.watch}</div><div class="dash-sum-lbl">观望</div></div>
    <div class="dash-sum-item"><div class="dash-sum-val" style="color:#f85149">${d.sell}</div><div class="dash-sum-lbl">卖出</div></div>
  </div>`

  // 结果摘要列表
  html += `<div class="daily-card" style="margin-bottom:14px">`
  html += `<div style="font-size:12px;color:var(--t3);margin-bottom:10px;font-weight:600">分析结果摘要</div>`
  ;(d.stocks||[]).forEach(s => {
    const sc   = sigColor[s.signal] || '#888'
    const sent = sentColor[s.sentiment] || '#888'
    html += `<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--b1)">
      <span style="font-size:8px;color:${sc}">●</span>
      <span style="font-size:14px;font-weight:600">${s.name}</span>
      <span style="font-size:12px;color:var(--t3)">(${s.code})</span>
      <span style="font-size:12px;color:${sc};margin-left:4px">${s.signal}</span>
      <span style="font-size:12px;color:var(--t3)">|</span>
      <span style="font-size:12px">评分 <b>${s.score||'N/A'}</b></span>
      <span style="font-size:12px;color:var(--t3)">|</span>
      <span style="font-size:12px;color:${sent}">${s.sentiment}</span>
    </div>`
  })
  html += `</div>`

  // 逐只详情卡片
  ;(d.stocks||[]).forEach((s,i) => {
    const sc   = sigColor[s.signal] || '#888'
    const sent = sentColor[s.sentiment] || '#888'
    html += `<div class="daily-card">
      <div class="daily-card-hd" onclick="toggleDcDetail('dc-${i}')">
        <span class="dc-name">${s.name}</span>
        <span class="dc-code">${s.code}</span>
        <span class="dc-signal" style="background:${sc}22;color:${sc}">${s.signal}</span>
        <span class="dc-sent" style="background:${sent}22;color:${sent}">${s.sentiment}</span>
        <span class="dc-score" style="color:${sc}">${s.score||'—'}</span>
        <span style="color:var(--t3);font-size:13px;margin-left:6px">▾</span>
      </div>`

    if(s.latest){
      html += `<div style="font-size:12px;color:var(--t3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        最新：${s.latest}</div>`
    }

    html += `<div class="dc-detail" id="dc-${i}">`

    if(s.ai_summary){
      html += `<div class="dc-section">
        <div class="dc-section-title">舆情情绪</div>
        <div class="dc-section-body">${s.ai_summary}</div>
      </div>`
    }
    if(s.ai_outlook){
      html += `<div class="dc-section">
        <div class="dc-section-title">业绩预期</div>
        <div class="dc-section-body">${s.ai_outlook}</div>
      </div>`
    }
    if((s.risks||[]).length){
      html += `<div class="dc-section">
        <div class="dc-section-title">⚠ 风险警报</div>
        <div class="dc-section-body">`
      s.risks.forEach((r,ri) => html += `<div>风险点${ri+1}：${r}</div>`)
      html += `</div></div>`
    }
    if((s.catalysts||[]).length){
      html += `<div class="dc-section">
        <div class="dc-section-title">✦ 利好催化</div>
        <div class="dc-section-body">`
      s.catalysts.forEach((c,ci) => html += `<div>利好${ci+1}：${c}</div>`)
      html += `</div></div>`
    }
    if((s.news||[]).length){
      html += `<div class="dc-section">
        <div class="dc-section-title">最新动态</div>`
      s.news.slice(0,4).forEach(n => {
        const link = n.url ? `<a href="${n.url}" target="_blank" style="color:inherit;text-decoration:none">${n.title}</a>` : n.title
        html += `<div class="dc-news-item">${link}
          <span style="color:var(--t3);margin-left:4px">[${n.source||''} ${n.time||''}]</span></div>`
      })
      html += `</div>`
    }
    html += `</div></div>`  // .dc-detail + .daily-card
  })

  return html
}

function toggleDcDetail(id){
  const el = document.getElementById(id)
  if(el) el.classList.toggle('open')
}
