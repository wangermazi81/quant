
// ══════════════════════════════════════════════════════
// 智能搜索页
// ══════════════════════════════════════════════════════
let _srchMode = 'news'
let _srchResults = []
let _srchSelIdx = -1
let _srchCurCode = ''
let _srchStatsLoaded = false

function setSrchMode(mode, btn){
  _srchMode = mode
  document.querySelectorAll('.srch-mode-btn').forEach(b=>b.classList.remove('active'))
  btn.classList.add('active')
}

function quickSearch(q){
  document.getElementById('srchInput').value = q
  doSearch()
}

async function doSearch(){
  const q = document.getElementById('srchInput').value.trim()
  if(!q){ return }
  const st = document.getElementById('srchStatus')
  const resEl = document.getElementById('srchResults')
  st.textContent = '🔍 搜索中...'
  resEl.innerHTML = '<div style="padding:24px 16px;text-align:center;color:var(--t3)"><span class="spin">⟳</span> 搜索中...</div>'

  // 判断是否是股票代码（6位数字）
  const isCode = /^\d{6}$/.test(q)

  try {
    let results = []
    let endpoint = ''
    let payload = {}

    if(isCode && _srchMode === 'stock'){
      // TwelveData 行情 + 综合信息
      const r = await api(`/search/stock_info/${q}`)
      if(r?.ok){
        _srchCurCode = q
        _renderStockDetail(r)
        st.textContent = `代码 ${q} 信息已加载`
        resEl.innerHTML = `<div style="padding:16px;font-size:13px;color:var(--t2)">✅ 已加载 ${q} 综合信息，查看右侧面板</div>`
        return
      }
    } else if(isCode && _srchMode === 'news'){
      // 个股新闻
      const r = await api(`/search/news/${q}`)
      if(r?.ok){
        results = r.results || []
        _srchCurCode = q
      }
    } else {
      // 通用搜索
      const r = await api('/search/web', 'POST', {
        query: q,
        topic: _srchMode,
        limit: 12,
      })
      if(r?.ok) results = r.results || []
    }

    _srchResults = results
    _srchSelIdx = -1
    _renderSrchList(results)
    st.textContent = results.length > 0
      ? `找到 ${results.length} 条结果`
      : '未找到结果，请尝试其他关键词'
  } catch(e){
    st.textContent = '搜索失败: ' + (e.message||e)
    resEl.innerHTML = '<div style="padding:16px;color:var(--rl)">搜索失败，请稍后重试</div>'
  }
}

function _renderSrchList(results){
  const el = document.getElementById('srchResults')
  if(!results.length){
    el.innerHTML = '<div style="padding:24px 16px;text-align:center;color:var(--t4)">无结果</div>'
    return
  }
  el.innerHTML = results.map((r,i)=>{
    const fromCls = r.from?.includes('tavily')?'tavily':r.from?.includes('serpapi')?'serpapi':'direct'
    const fromLbl = r.from?.includes('tavily')?'Tavily':r.from?.includes('serpapi')?'SerpAPI':'直抓'
    return `<div class="srch-result-item" onclick="_selectSrchItem(${i})" id="sri_${i}">
      <div style="font-size:13px;font-weight:600;color:var(--t1);margin-bottom:4px;line-height:1.4">${esc(r.title||'')}</div>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="font-size:11px;color:var(--t4)">${esc(r.source||r.from||'')}</span>
        ${r.time?`<span style="font-size:10px;color:var(--t4)">${esc(r.time)}</span>`:''}
        <span class="srch-badge ${fromCls}">${fromLbl}</span>
      </div>
      ${r.content?`<div style="font-size:11px;color:var(--t3);margin-top:4px;line-height:1.5">${esc(r.content.substring(0,100))}...</div>`:''}
    </div>`
  }).join('')
}

function _selectSrchItem(idx){
  _srchSelIdx = idx
  document.querySelectorAll('.srch-result-item').forEach((el,i)=>el.classList.toggle('active',i===idx))
  const item = _srchResults[idx]
  if(!item) return

  const titleEl = document.getElementById('srchDetailTitle')
  const metaEl  = document.getElementById('srchDetailMeta')
  const newsEl  = document.getElementById('srchNewsDetail')
  titleEl.textContent = item.title || '搜索结果'
  metaEl.textContent  = [item.source, item.time].filter(Boolean).join(' · ')

  newsEl.innerHTML = `
    <div class="srch-news-card">
      <div class="nc-title">${esc(item.title||'')}</div>
      <div class="nc-meta">
        来源：${esc(item.source||item.from||'—')}
        ${item.time?' · '+esc(item.time):''}
        ${item.url?` · <a href="${esc(item.url)}" target="_blank" style="color:var(--bl);font-size:11px">原文链接 ↗</a>`:''}
      </div>
      <div class="nc-body">${esc(item.content||'（无摘要）')}</div>
    </div>`

  // 如果是股票代码，显示分析按钮
  if(_srchCurCode){
    document.getElementById('srchAnalyzeBtn').style.display=''
    document.getElementById('srchAddQueueBtn').style.display=''
  }
}

function _renderStockDetail(r){
  const newsEl  = document.getElementById('srchNewsDetail')
  const quoteEl = document.getElementById('srchQuoteCard')
  const quoteCt = document.getElementById('srchQuoteContent')
  const titleEl = document.getElementById('srchDetailTitle')
  const metaEl  = document.getElementById('srchDetailMeta')

  titleEl.textContent = `${r.name||r.code} (${r.code})`
  metaEl.textContent  = `综合信息`
  document.getElementById('srchAnalyzeBtn').style.display=''
  document.getElementById('srchAddQueueBtn').style.display=''

  // TwelveData 报价
  const q = r.quote||{}
  if(q.price){
    quoteEl.style.display=''
    const chgColor = (q.change_pct||0)>=0?'var(--gl)':'var(--rl)'
    quoteCt.innerHTML = `
      <div style="display:flex;gap:20px;flex-wrap:wrap">
        <div><div style="font-size:24px;font-weight:700;color:var(--t1)">${q.price?.toFixed(2)||'—'}</div>
          <div style="font-size:13px;color:${chgColor}">${(q.change_pct||0)>=0?'+':''}${(q.change_pct||0).toFixed(2)}%</div></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:12px;color:var(--t2)">
          <span>开盘：${q.open?.toFixed(2)||'—'}</span>
          <span>最高：<b style="color:var(--rl)">${q.high?.toFixed(2)||'—'}</b></span>
          <span>昨收：${q.prev_close?.toFixed(2)||'—'}</span>
          <span>最低：<b style="color:var(--gl)">${q.low?.toFixed(2)||'—'}</b></span>
        </div>
        <div style="font-size:11px;color:var(--t4);align-self:flex-end">交易所：${q.exchange||'—'} · 来源：TwelveData</div>
      </div>`
  } else {
    quoteEl.style.display='none'
  }

  // 新闻
  const news = [...(r.news||[]), ...(r.web||[])]
  if(news.length){
    newsEl.innerHTML = `
      <div style="font-size:11px;font-weight:700;color:var(--t2);margin-bottom:10px">📰 相关新闻（${news.length}条）</div>
      ${news.map(n=>`<div class="srch-news-card">
        <div class="nc-title">${esc(n.title||'')}</div>
        <div class="nc-meta">${esc(n.source||n.from||'')}${n.time?' · '+esc(n.time):''}${n.url?` · <a href="${esc(n.url)}" target="_blank" style="color:var(--bl)">原文 ↗</a>`:''}</div>
        ${n.content?`<div class="nc-body">${esc(n.content)}</div>`:''}
      </div>`).join('')}`
  } else {
    newsEl.innerHTML = '<div style="color:var(--t4);font-size:13px;padding:16px 0">暂无相关新闻</div>'
  }
}

async function analyzeFromSearch(){
  if(!_srchCurCode) return
  switchPage('monitor', document.querySelectorAll('.ntab')[0])
  // 填入分析代码框并触发分析
  await new Promise(r=>setTimeout(r,200))
  const inp = document.getElementById('aiCode')
  if(inp){ inp.value = _srchCurCode; doAnalysis() }
}

function addSearchToQueue(){
  if(!_srchCurCode) return
  api('/queue/add','POST',{code:_srchCurCode, name:_srchCurCode, price:0, reason:'从搜索页加入'})
    .then(r=>{ if(r?.ok) alert(`✅ ${_srchCurCode} 已加入买入队列`); else alert(r?.msg||'操作失败') })
}

// ── 搜索服务状态 ──────────────────────────────────────
let _srchStats = {}

async function refreshSrchStats(){
  const r = await api('/search/stats')
  if(!r?.ok) return
  _srchStats = r
  _renderSrchServiceBadges(r)
}

function _renderSrchServiceBadges(stats){
  const el = document.getElementById('srchServiceBadges')
  if(!el) return
  const services = [
    { key:'tavily',     label:'Tavily',      color:'tavily' },
    { key:'serpapi',    label:'SerpAPI',     color:'serpapi' },
    { key:'twelvedata', label:'TwelveData',  color:'direct' },
  ]
  el.innerHTML = services.map(svc=>{
    const st = stats[svc.key]||{}
    const accs = (st.accounts||[])
    const totalRem = accs.reduce((s,a)=>s+a.remaining,0)
    const totalLim = accs.reduce((s,a)=>s+a.limit,0)
    const pct = totalLim>0?Math.round(totalRem/totalLim*100):0
    const cls = pct>30?'ok':pct>5?'warn':'off'
    return `<span class="svc-badge ${cls}" title="${totalRem}/${totalLim} 剩余" onclick="_showStatsPanel()">${svc.label} ${totalRem}</span>`
  }).join('')
}

function _showStatsPanel(){
  const panel = document.getElementById('srchStatsPanel')
  panel.style.display = panel.style.display==='none' ? '' : 'none'
  if(panel.style.display === '' && _srchStats){
    const ct = document.getElementById('srchStatsContent')
    ct.innerHTML = ['tavily','serpapi','twelvedata'].map(svc=>{
      const st = _srchStats[svc]||{}
      const accs = st.accounts||[]
      return `<div style="margin-bottom:12px">
        <div style="font-size:12px;font-weight:700;color:var(--t1);margin-bottom:6px">${svc.charAt(0).toUpperCase()+svc.slice(1)}</div>
        ${accs.map(a=>`
          <div style="margin-bottom:6px">
            <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--t2)">
              <span>${esc(a.label)}</span>
              <span>${a.used}/${a.limit} (${a.pct}%)</span>
            </div>
            <div class="usage-bar"><div class="usage-bar-fill" style="width:${a.pct}%;background:${a.pct>80?'var(--rl)':a.pct>50?'#e3b341':'var(--gl)'}"></div></div>
          </div>`).join('')}
      </div>`
    }).join('')
  }
}

async function initSearchPage(){
  if(!_srchStatsLoaded){
    _srchStatsLoaded = true
    await refreshSrchStats()
  }
}
