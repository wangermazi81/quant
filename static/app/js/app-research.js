// ══ 投研中心：产业链 / 研报中心 / 智能搜索 三页签 ══════════════════

let _rsTab       = 'chain'
let _rsInited    = false
let _curConcept  = ''
let _repTimer    = null

function rsInit(){
  if(_rsInited) return
  _rsInited = true
  _rvBindEvents()
  chainSearch('')
  // 研报中心 5 分钟自动刷新（仅该页签激活时），保证始终是最新研报
  if(!_repTimer) _repTimer = setInterval(()=>{
    const pg = document.getElementById('page-search')
    if(pg && pg.classList.contains('active') && _rsTab === 'report') loadReports()
  }, 300000)
}

function setChainMobilePane(pane){
  const view = document.getElementById('rsView-chain')
  if(!view) return
  view.dataset.mobilePane = pane === 'detail' ? 'detail' : 'list'
  const target = document.getElementById(pane === 'detail' ? 'chainDetail' : 'chainList')
  if(target) target.scrollTop = 0
}

function setChainMobileSection(section){
  const detail = document.getElementById('chainDetail')
  if(!detail) return
  const next = section === 'stocks' ? 'stocks' : 'graph'
  detail.dataset.mobileSection = next
  detail.querySelectorAll('[data-chain-section-target]').forEach(button=>{
    const active = button.dataset.chainSectionTarget === next
    button.classList.toggle('active', active)
    button.setAttribute('aria-selected', active ? 'true' : 'false')
  })
  detail.scrollTop = 0
}

function _rvBindEvents(){
  const page = document.getElementById('page-search')
  if(!page || page.dataset.rvBound) return
  page.dataset.rvBound = '1'
  page.addEventListener('click', e=>{
    const concept = e.target.closest('[data-rv-concept]')
    if(concept){ openConcept(concept.dataset.rvConcept, concept); return }
    const kline = e.target.closest('[data-rv-kline]')
    if(kline){ showKLine(kline.dataset.rvCode, kline.dataset.rvName); return }
    const external = e.target.closest('[data-rv-url]')
    if(external && !e.target.closest('a')) window.open(external.dataset.rvUrl, '_blank', 'noopener')
  })
}

function rsSwitch(tab){
  _rsTab = tab
  document.querySelectorAll('.rs-tab').forEach(t=>t.classList.remove('active'))
  const btn = document.getElementById('rsTab-' + tab)
  if(btn) btn.classList.add('active')
  document.getElementById('rsView-chain').style.display  = tab==='chain'  ? 'flex' : 'none'
  document.getElementById('rsView-report').style.display = tab==='report' ? 'flex' : 'none'
  document.getElementById('rsView-search').style.display = tab==='search' ? 'flex' : 'none'
  if(tab === 'chain') setChainMobilePane('list')
  if(tab === 'search' && typeof setSearchMobilePane === 'function') setSearchMobilePane('results')
  if(tab === 'report') loadReports()
}

// ── 产业链：概念列表 ───────────────────────────────────────────
async function chainSearch(q){
  const hint = document.getElementById('chainListHint')
  const box  = document.getElementById('chainList')
  hint.textContent = '加载中…'
  try{
    const r = await fetch('/api/chain/concepts?q=' + encodeURIComponent(q||''))
    const d = await r.json()
    const list = d.concepts||[]
    hint.textContent = list.length ? `${q?'匹配':'热门'} ${list.length} 个概念` : '没找到，换个关键词试试'
    box.innerHTML = list.map(c=>`
      <div class="srch-result-item" data-rv-concept="${_rvAttr(c.name)}">
        <div style="display:flex;align-items:center;gap:8px">
          <b style="font-size:13px">${_rvEsc(c.name)}</b>
          ${c.chg!=null ? `<span style="margin-left:auto;font-weight:700;font-size:12px;color:${(c.chg||0)>=0?'#f85149':'#3fb950'}">${(c.chg||0)>=0?'+':''}${c.chg}%</span>` : ''}
        </div>
        ${c.up!=null ? `<div style="font-size:11px;color:var(--t3);margin-top:2px">上涨 ${c.up} · 下跌 ${c.down}</div>` : ''}
      </div>`).join('') || '<div style="padding:24px;text-align:center;color:var(--t4)">无结果</div>'
  }catch(e){
    hint.textContent = '加载失败：' + e.message
  }
}

// ── 产业链：概念详情(图谱 + 相关度成分股表) ────────────────────
const _REL_STYLE = {
  '核心': 'background:rgba(248,81,73,.15);color:#f85149;border:1px solid rgba(248,81,73,.4)',
  '重要': 'background:rgba(245,158,11,.15);color:#f59e0b;border:1px solid rgba(245,158,11,.4)',
  '一般': 'background:rgba(139,148,158,.12);color:var(--t3);border:1px solid var(--b1)',
}

async function openConcept(name, el){
  _curConcept = name
  setChainMobilePane('detail')
  document.querySelectorAll('#chainList .srch-result-item').forEach(i=>i.classList.remove('active'))
  if(el) el.classList.add('active')
  document.getElementById('chainTitle').textContent = '🧬 ' + name
  document.getElementById('chainMeta').textContent  = '加载中…'
  const gbtn = document.getElementById('chainGraphBtn')
  gbtn.style.display = 'none'
  const box = document.getElementById('chainDetail')
  box.innerHTML = '<div class="empty-hint"><span class="spin">⟳</span> 加载成分股…</div>'
  try{
    const r = await fetch('/api/chain/stocks?name=' + encodeURIComponent(name))
    const d = await r.json()
    if(!d.ok) throw new Error(d.error||'加载失败')
    document.getElementById('chainMeta').textContent =
      `${d.stocks.length} 只成分股 · 按涨跌幅排序${d.stale ? ' · ⚠ 行情为缓存(源站限流中)' : ''}`
    gbtn.style.display = ''
    gbtn.textContent   = d.graph_exists ? '🔄 重新生成产业链图谱' : '🧬 AI生成产业链图谱'

    const hasRel = d.stocks.some(s=>s.rel_level)
    const relNote = hasRel ? '（相关度由 AI 产业链图谱标注）'
      : d.graph_generating ? '（相关度图谱后台生成中，约1分钟后自动刷新…）'
      : '（生成产业链图谱后将标注每只股票与概念的相关度）'
    // 后台生成中 → 60秒后自动刷新一次（最多重试3次，避免无限轮询）
    if(d.graph_generating && !hasRel){
      window._chainRelRetry = window._chainRelRetry || {}
      const n = window._chainRelRetry[name] || 0
      if(n < 3){
        window._chainRelRetry[name] = n + 1
        setTimeout(()=>{ if(_curConcept === name) openConcept(name) }, 60000)
      }
    }
    const graphStatus = d.graph_exists
      ? '<div class="empty-hint"><span class="spin">\u27f3</span> \u52a0\u8f7d\u4ea7\u4e1a\u94fe\u56fe\u8c31\u2026</div>'
      : '<div class="empty-hint">\u5c1a\u672a\u751f\u6210\u4ea7\u4e1a\u94fe\u56fe\u8c31\uff0c\u53ef\u4f7f\u7528\u4e0a\u65b9\u6309\u94ae\u751f\u6210</div>'
    let html = `<div class="research-chain-section-tabs" role="tablist" aria-label="\u4ea7\u4e1a\u94fe\u8be6\u60c5\u5207\u6362">
      <button type="button" class="research-chain-section-tab" role="tab" data-chain-section-target="graph" data-ui-click="setChainMobileSection('graph')">\u4ea7\u4e1a\u94fe\u56fe\u8c31</button>
      <button type="button" class="research-chain-section-tab" role="tab" data-chain-section-target="stocks" data-ui-click="setChainMobileSection('stocks')">\u6210\u5206\u80a1 ${d.stocks.length}</button>
    </div>
    <section class="research-chain-section research-chain-graph-section" data-chain-section="graph"><div id="chainGraphBox">${graphStatus}</div></section>
    <section class="research-chain-section research-chain-stocks-section" data-chain-section="stocks">`
    html += `<div class="daily-card chain-stock-card"><div style="font-size:12px;color:var(--t3);font-weight:600;margin-bottom:8px">成分股${relNote}</div>
      <div class="research-table-scroll"><table class="chain-stock-table" style="width:100%;font-size:12px;border-collapse:collapse">
      <thead><tr style="color:var(--t3);text-align:left"><th style="padding:4px">代码</th><th>名称</th><th>相关度</th><th style="text-align:right">现价</th><th style="text-align:right">涨跌幅</th><th style="text-align:right">换手%</th><th style="text-align:right">市值(亿)</th><th></th></tr></thead><tbody>`
    d.stocks.forEach(s=>{
      const up  = (s.chg||0) >= 0
      const rel = s.rel_level
        ? `<span style="font-size:10px;padding:1px 7px;border-radius:9px;${_REL_STYLE[s.rel_level]||_REL_STYLE['一般']}" title="${_rvEsc(s.rel_reason||'')}">${s.rel_level}</span>`
        : '<span style="color:var(--t4);font-size:11px">—</span>'
      html += `<tr style="border-top:1px solid var(--b1)">
        <td data-label="代码" style="padding:5px 4px;font-family:monospace">${s.code}</td>
        <td data-label="名称"><b>${_rvEsc(s.name)}</b></td>
        <td data-label="相关度">${rel}</td>
        <td data-label="现价" style="text-align:right">${s.price ?? '—'}</td>
        <td data-label="涨跌幅" style="text-align:right;color:${up?'#f85149':'#3fb950'};font-weight:600">${up?'+':''}${s.chg}%</td>
        <td data-label="换手率" style="text-align:right">${s.turnover ?? '—'}</td>
        <td data-label="市值(亿)" style="text-align:right">${s.mcap ?? '—'}</td>
        <td data-label="操作" style="text-align:right"><button class="btn btn-gray btn-xs" data-rv-kline data-rv-code="${_rvAttr(s.code)}" data-rv-name="${_rvAttr(s.name)}">K线</button></td></tr>`
    })
    html += '</tbody></table></div></div></section>'
    box.innerHTML = html
    setChainMobileSection(d.graph_exists ? 'graph' : 'stocks')
    if(d.graph_exists) _loadChainGraph(name)
  }catch(e){
    document.getElementById('chainMeta').textContent = ''
    box.innerHTML = `<div class="empty-hint" style="color:#f85149">加载失败：${e.message}</div>`
  }
}

async function _loadChainGraph(name){
  try{
    const r = await fetch('/api/chain/graph?name=' + encodeURIComponent(name))
    const d = await r.json()
    if(d.ok) _renderChainGraph(d, false)
  }catch(e){}
}

async function genChainGraph(){
  const name = _curConcept
  if(!name) return
  const btn = document.getElementById('chainGraphBtn')
  if((document.getElementById('chainGraphBox')||{innerHTML:''}).innerHTML.trim()){
    if(!confirm('重新生成「' + name + '」产业链图谱？（消耗积分）')) return
  }
  btn.disabled = true; btn.textContent = '⏳ AI 生成中（约1分钟）…'
  try{
    const r = await api('/chain/graph', 'POST', {name})
    if(!r) throw new Error('请求失败（可能需要先登录）')
    if(!r.ok) throw new Error(r.error||'生成失败')
    _renderChainGraph(r, true)
    openConcept(name)            // 重新拉成分股以合并相关度标注
  }catch(e){ alert('生成失败：' + e.message) }
  btn.disabled = false; btn.textContent = '🔄 重新生成产业链图谱'
}

function _renderChainGraphCards(d, stageColor, lvColor){
  const stages = (d.segments||[]).map(seg=>{
    const color = stageColor[seg.stage] || '#58a6ff'
    const items = (seg.items||[]).filter(it=>it.segment || (it.stocks||[]).length)
    if(!items.length) return ''
    return `<section class="chain-stage-card" style="--chain-stage:${color}">
      <div class="chain-stage-head"><b>${_rvEsc(seg.stage)}</b>${seg.weight ? `<span>${_rvEsc(seg.weight)}</span>` : ''}</div>
      <div class="chain-stage-items">${items.map(it=>{
        const stocks = (it.stocks||[]).length ? it.stocks : [null]
        return `<article class="chain-segment-card">
          <div class="chain-segment-title">${_rvEsc(it.segment)||'\u672a\u5206\u7c7b'}</div>
          ${it.desc ? `<div class="chain-segment-desc">${_rvEsc(it.desc)}</div>` : ''}
          <div class="chain-segment-stocks">${stocks.map(stock=>{
            if(!stock) return '<div class="chain-stock-chip">\u6682\u65e0\u4e2a\u80a1</div>'
            const isObj = typeof stock === 'object'
            const name = isObj ? stock.name : String(stock).split('(')[0]
            const code = isObj ? (stock.code||'') : (String(stock).match(/\d{6}/)||[''])[0]
            const relation = isObj ? (stock.relation||'') : ''
            const level = isObj ? (stock.level||'') : ''
            return `<button type="button" class="chain-stock-chip" ${code?`data-rv-kline data-rv-code="${_rvAttr(code)}" data-rv-name="${_rvAttr(name)}"`:''}>
              <span class="chain-stock-chip-main"><b>${_rvEsc(name)}</b>${code?`<small>${code}</small>`:''}</span>
              <span class="chain-stock-chip-rel">${level?`<strong style="color:${lvColor[level]||'var(--t3)'}">${_rvEsc(level)}</strong>`:''}${_rvEsc(relation)||'\u2014'}</span>
            </button>`
          }).join('')}</div>
        </article>`
      }).join('')}</div>
    </section>`
  }).join('')
  return `<div class="chain-mobile-graph">${stages}</div>`
}

function _renderChainGraph(d, scroll){
  // 韭研产业库式分层表格：大类(占比) | 细分环节(说明) | 个股 | 相关性
  const box = document.getElementById('chainGraphBox')
  if(!box) return
  const stageColor = {'上游':'#58a6ff', '中游':'#f59e0b', '下游':'#3fb950'}
  const lvColor    = {'核心':'#f85149', '重要':'#f59e0b', '一般':'var(--t3)'}

  let h = `<div class="daily-card" style="margin-bottom:12px;border-color:rgba(88,166,255,.4);padding:14px">
    <div style="font-size:13px;font-weight:700;color:var(--bl);margin-bottom:6px">🧬 ${_rvEsc(d.name)} 产业链
      <span style="font-size:10px;color:var(--t3);font-weight:400;margin-left:8px">${d.generated_at||''} · AI整理仅供参考</span></div>
    ${d.summary ? `<div style="font-size:12px;color:var(--t2);margin-bottom:10px;line-height:1.6">${_rvEsc(d.summary)}</div>` : ''}
    <div class="chain-desktop-graph"><div class="research-table-scroll"><table class="chain-tbl"><thead><tr><th style="width:74px">环节</th><th style="width:120px">细分</th><th style="width:108px">个股</th><th>相关性</th></tr></thead><tbody>`

  ;(d.segments||[]).forEach(seg=>{
    const items = (seg.items||[]).filter(it=>it.segment || (it.stocks||[]).length)
    if(!items.length) return
    const c = stageColor[seg.stage] || '#58a6ff'
    const stageRows = items.reduce((n,it)=>n + Math.max(1,(it.stocks||[]).length), 0)
    let stageCell = `<td rowspan="${stageRows}" class="ct-stage" style="color:${c};border-left:3px solid ${c}">
      ${_rvEsc(seg.stage)}${seg.weight ? `<div class="ct-weight">${_rvEsc(seg.weight)}</div>` : ''}</td>`
    items.forEach(it=>{
      const stocks = (it.stocks||[]).length ? it.stocks : [null]
      let segCell = `<td rowspan="${stocks.length}" class="ct-seg"><b>${_rvEsc(it.segment)}</b>
        ${it.desc ? `<div class="ct-desc">${_rvEsc(it.desc)}</div>` : ''}</td>`
      stocks.forEach(s=>{
        h += '<tr>' + stageCell + segCell
        stageCell = ''; segCell = ''
        if(!s){ h += '<td class="ct-stock">—</td><td class="ct-rel">—</td></tr>'; return }
        const isObj = typeof s === 'object'
        const nm  = isObj ? s.name : String(s).split('(')[0]
        const cd  = isObj ? (s.code||'') : (String(s).match(/\d{6}/)||[''])[0]
        const rel = isObj ? (s.relation||'') : ''
        const lv  = isObj ? (s.level||'') : ''
        h += `<td class="ct-stock" ${cd?`data-rv-kline data-rv-code="${_rvAttr(cd)}" data-rv-name="${_rvAttr(nm)}" style="cursor:pointer" title="点击看K线"`:''}>
            <b>${_rvEsc(nm)}</b>${cd?`<div class="ct-code">${cd}</div>`:''}</td>
          <td class="ct-rel">${lv?`<span class="ct-lv" style="color:${lvColor[lv]||'var(--t3)'}">${lv}</span>`:''}${_rvEsc(rel)||'—'}</td></tr>`
      })
    })
  })
  h += '</tbody></table></div></div>' + _renderChainGraphCards(d, stageColor, lvColor) + '</div>'
  box.innerHTML = h
  if(scroll) box.scrollIntoView({behavior:'smooth'})
}

// ── 研报中心 ───────────────────────────────────────────────────
async function loadReports(){
  const qtype = document.getElementById('repType').value
  const q     = document.getElementById('repQ').value.trim()
  const hint  = document.getElementById('repHint')
  const box   = document.getElementById('repBody')
  hint.textContent = '查询中…'
  box.innerHTML = '<div class="empty-hint"><span class="spin">⟳</span> 加载研报…</div>'
  try{
    const r = await fetch(`/api/reports/latest?qtype=${qtype}&q=${encodeURIComponent(q)}`)
    const d = await r.json()
    if(!d.ok) throw new Error(d.error||'查询失败')
    const reps = d.reports||[]
    hint.textContent = reps.length ? `${reps.length} 篇${d.code ? ` · ${d.code}` : ''}` : '无结果'
    const ratingColor = {'买入':'#3fb950','增持':'#58a6ff','中性':'#e3b341','减持':'#f85149','卖出':'#f85149'}
    // 评级分布
    let html = ''
    const cnt = {}
    reps.forEach(x=>{ if(x.rating) cnt[x.rating] = (cnt[x.rating]||0) + 1 })
    if(Object.keys(cnt).length){
      html += `<div class="dash-summary" style="margin-bottom:12px">` + Object.entries(cnt).map(([k,v]) =>
        `<div class="dash-sum-item"><div class="dash-sum-val" style="color:${ratingColor[k]||'#888'}">${v}</div><div class="dash-sum-lbl">${k}</div></div>`).join('') + `</div>`
    }
    reps.forEach(x=>{
      const rc = ratingColor[x.rating] || '#888'
      const tag = x.stock
        ? `<span class="srch-badge">${_rvEsc(x.stock)} ${_rvEsc(x.code||'')}</span>`
        : x.industry ? `<span class="srch-badge">${_rvEsc(x.industry)}</span>` : ''
      const safeUrl = _rvSafeUrl(x.url)
      const titleText = _rvEsc(x.title)
      const reportLink = safeUrl
        ? `<a class="research-report-link" href="${_rvAttr(safeUrl)}" target="_blank" rel="noopener"><span class="nc-title research-report-title">${titleText}</span><span class="research-report-open">\u9605\u8bfb \u2197</span></a>`
        : `<div class="research-report-link research-report-link-static"><span class="nc-title research-report-title">${titleText}</span></div>`
      const meta = [
        x.date ? `<span>${_rvEsc(x.date)}</span>` : '',
        x.org ? `<span>${_rvEsc(x.org)}</span>` : '',
        x.rating ? `<strong class="research-report-rating" style="color:${rc}">${_rvEsc(x.rating)}</strong>` : '',
      ].filter(Boolean).join('')
      html += `<article class="srch-news-card research-report-card" style="cursor:${safeUrl?'pointer':'default'}" ${safeUrl?`data-rv-url="${_rvAttr(safeUrl)}"`:''}>
        ${reportLink}
        ${meta ? `<div class="nc-meta research-report-meta">${meta}</div>` : ''}
        ${tag ? `<div class="research-report-tags">${tag}</div>` : ''}
      </article>`
    })
    box.innerHTML = html || '<div class="empty-hint">暂无研报（源站可能限流，稍后重试）</div>'
  }catch(e){
    hint.textContent = ''
    box.innerHTML = `<div class="empty-hint" style="color:#f85149">查询失败：${e.message}</div>`
  }
}

// ══ 复盘页 ══════════════════════════════════════════════════════

let _reviewInited = false
let _newsTimer    = null

function initReviewPage(){
  if(_reviewInited) return
  _reviewInited = true
  loadReviewDates().then(()=>loadReview())
  loadNews()
  _loadNewsSources()
  // 新闻 30 秒自动刷新（仅复盘页处于激活时）
  if(!_newsTimer) _newsTimer = setInterval(()=>{
    const pg = document.getElementById('page-review')
    if(pg && pg.classList.contains('active')) loadNews(true)
  }, 30000)
  // 盘中每 2 分钟静默刷新复盘数据（涨停天梯/情绪跟着盘面走）
  if(!_rvLiveTimer) _rvLiveTimer = setInterval(()=>{
    const pg = document.getElementById('page-review')
    if(!pg || !pg.classList.contains('active')) return
    const sel   = document.getElementById('rv-date')
    const today = _normDate(new Date())
    if(sel && sel.value && sel.value !== today) return   // 看历史日期时不打扰
    const now = new Date(), day = now.getDay(), hm = now.getHours()*100 + now.getMinutes()
    if(day >= 1 && day <= 5 && hm >= 925 && hm <= 1505) loadReview(true, true)
  }, 120000)
}
let _rvLiveTimer = null

// ══════════════════════════════════════════════════════════════
// 龙虎榜 + 游资图谱
// ══════════════════════════════════════════════════════════════
let _lhbTab   = 'graph'   // 当前子 tab
let _lhbCache = null      // 当前加载的数据
let _lhbInited = false

function rvSwitchTab(tab){
  const isLhb = tab === 'lhb'
  document.getElementById('rv-main-tab').classList.toggle('active', !isLhb)
  document.getElementById('rv-lhb-tab').classList.toggle('active',  isLhb)
  document.getElementById('rv-review-ctrl').style.display = isLhb ? 'none'  : 'flex'
  document.getElementById('rv-lhb-ctrl').style.display    = isLhb ? 'flex'  : 'none'
  document.getElementById('rv-main').style.display        = isLhb ? 'none'  : ''
  document.getElementById('rv-lhb-area').style.display    = isLhb ? 'flex'  : 'none'
  if(isLhb && !_lhbInited){
    _lhbInited = true
    _initLhbDates().then(()=>loadLhb())
  }
}

function lhbSubTab(sub){
  _lhbTab = sub
  document.getElementById('lhb-sub-graph').classList.toggle('active', sub==='graph')
  document.getElementById('lhb-sub-list').classList.toggle('active',  sub==='list')
  document.getElementById('lhb-graph-area').style.display = sub==='graph' ? 'block' : 'none'
  document.getElementById('lhb-list-area').style.display  = sub==='list'  ? 'block' : 'none'
  // 已有数据则直接重渲染，不重新请求
  if(_lhbCache){
    if(sub==='graph') renderYouzaiGraph(_lhbCache, document.getElementById('lhb-graph-canvas'))
    else              renderLhbList(_lhbCache,     document.getElementById('lhb-list-canvas'))
  }
}

async function _initLhbDates(){
  try{
    const r = await fetch('/api/lhb/dates')
    const d = await r.json()
    const sel   = document.getElementById('lhb-date')
    const today = _normDate(new Date())
    const dates = (d.dates||[]).slice()
    if(!dates.includes(today)) dates.unshift(today)
    sel.innerHTML = dates.map(x=>`<option value="${x}">${x===today?x+'（今天）':x}</option>`).join('')
  }catch(e){}
}

async function loadLhb(force){
  const sel  = document.getElementById('lhb-date')
  const date = sel && sel.value ? sel.value : ''
  const gc   = document.getElementById('lhb-graph-canvas')
  const lc   = document.getElementById('lhb-list-canvas')
  const st   = document.getElementById('lhb-status')
  if(st) st.textContent = force === true ? '正在强制刷新…' : '正在加载…'
  gc.innerHTML = '<div class="empty-hint">加载中…</div>'
  lc.innerHTML = '<div class="empty-hint">加载中…</div>'
  try{
    const r = await fetch(`/api/lhb/data?date=${date}${force===true?'&refresh=1':''}`)
    const d = await r.json()
    _lhbCache = d
    if(st){
      const t = d.generated_at ? d.generated_at.slice(11,19) : ''
      const updating = d.status === 'updating' || d.finalized === false
      const label = updating ? '整理中' : '已归档'
      const seat = d.seat_detail_target ? ` · 席位 ${d.seat_detail_count||0}/${d.seat_detail_target}` : ''
      st.textContent = d.ok ? `${label} ${d.total||0} 只${seat}${t?' · '+t:''}` : (d.error || '暂无数据')
    }
    renderYouzaiGraph(d, gc)
    renderLhbList(d, lc)
    // 若当前子 tab 是榜单则切回显示
    if(_lhbTab === 'list'){
      document.getElementById('lhb-graph-area').style.display = 'none'
      document.getElementById('lhb-list-area').style.display  = 'block'
    }
  }catch(e){
    if(st) st.textContent = '加载失败'
    gc.innerHTML = `<div class="empty-hint">加载失败: ${e.message}</div>`
    lc.innerHTML = `<div class="empty-hint">加载失败: ${e.message}</div>`
  }
}

function _lhbEsc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') }
function _lhbAttr(s){ return _lhbEsc(s).replace(/"/g,'&quot;') }
function _lhbJs(s){ return String(s==null?'':s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/[\r\n]+/g,' ') }
function _lhbSectorBadges(s, compact){
  const items = []
  const ind = (s.industry||'').trim()
  if(ind) items.push({txt:ind, cls:'ind'})
  ;(s.concepts||[]).slice(0, compact?1:2).forEach(c=>{ if(c) items.push({txt:c, cls:''}) })
  if(!items.length) return `<span class="lhb-sector-badge pending" title="行业/概念数据源暂未返回，刷新后自动更新">${compact?'待更新':'行业待更新'}</span>`
  const title = [ind, ...(s.concepts||[])].filter(Boolean).join(' / ')
  return items.map(x=>`<span class="lhb-sector-badge ${x.cls}" title="${_lhbAttr(title)}">${_lhbEsc(x.txt)}</span>`).join('')
}
function _wireLhbGraphHover(container){
  const wrap = container.querySelector('.lhb-graph-wrap')
  if(!wrap) return
  const nodes = wrap.querySelectorAll('.lhb-br-item,.lhb-s-item,.lhb-yz-item')
  const setHot = el => {
    wrap.classList.add('has-focus')
    const code = el.dataset.code, br = el.dataset.br, yz = el.dataset.yz
    wrap.querySelectorAll('.hot').forEach(x=>x.classList.remove('hot'))
    const sel = [code&&`[data-code="${code}"]`, br&&`[data-br="${br}"]`, yz&&`[data-yz="${yz}"]`].filter(Boolean).join(',')
    if(sel) wrap.querySelectorAll(sel).forEach(x=>x.classList.add('hot'))
  }
  nodes.forEach(el=>{
    el.addEventListener('mouseenter', ()=>setHot(el))
    el.addEventListener('mouseleave', ()=>{ wrap.classList.remove('has-focus'); wrap.querySelectorAll('.hot').forEach(x=>x.classList.remove('hot')) })
  })
}
function toggleLhbSeats(id){
  const el = document.getElementById(id)
  if(!el) return
  el.classList.toggle('open')
  const btn = document.querySelector(`[data-seat-toggle="${id}"]`)
  if(btn) btn.textContent = el.classList.contains('open') ? '收起席位' : '展开全部席位'
}

// ── 游资图谱渲染 ───────────────────────────────────────────────
function renderYouzaiGraph(data, container){
  if(!data.ok || !data.stocks?.length){
    container.innerHTML = `<div class="empty-hint">${data.error||'暂无数据（非交易日或数据未发布）'}</div>`
    return
  }
  const stocks  = data.stocks.slice()
  const ROW_H   = 62, PAD_TOP = 30

  // ── 构建买方营业部节点 ──────────────────────────────────────
  const brMap = {}
  stocks.forEach(s=>{
    ;(s.branches||[]).filter(b=>b.net>0).forEach(b=>{
      if(!brMap[b.name]) brMap[b.name]={name:b.name,tag:b.tag||'',total:0,codes:new Set()}
      brMap[b.name].total += b.net
      brMap[b.name].codes.add(s.code)
    })
  })
  // ── 游资节点：直接用后端权威统计 youzai_stat（覆盖全部上榜股，
  //    与顶部徽章同源），避免前端只按 top30 重算导致数量对不上、整组缺失 ──
  const allBranches = Object.values(brMap).sort((a,b)=>b.total-a.total)
  const branches  = allBranches.slice(0,60)
  const youzaiList= (data.youzai_stat||[]).map(y=>({
    name:  y.name,
    count: (y.stocks||[]).length,
    total: y.total_net||0
  }))

  // Y 坐标
  stocks.forEach((s,i)  => s._y = PAD_TOP + i * ROW_H)
  branches.forEach((b,i)=> b._y = PAD_TOP + i * ROW_H)
  youzaiList.forEach((y,i)=>y._y = PAD_TOP + i * ROW_H)

  const totalH = Math.max(stocks.length, branches.length, youzaiList.length) * ROW_H + PAD_TOP + 24
  // 列宽
  const C1W=306, C2X=350, C2W=286, C3X=680, C3W=230, TOTAL_W=C3X+C3W+10

  let svgPaths = ''

  // ── Column 1: 买方营业部 ────────────────────────────────────
  let col1 = `<div class="lhb-col" style="left:0;width:${C1W}px">
    <div class="lhb-col-hd">买方营业部</div>`
  branches.forEach((b,bi)=>{
    const amt = Math.abs(b.total)>=10000 ? (b.total/10000).toFixed(1)+'亿' : b.total.toFixed(0)+'万'
    const tag = b.tag ? `<span class="lhb-yz-badge" style="font-size:9px;padding:1px 5px;margin-right:3px">${_lhbEsc(b.tag)}</span>` : ''
    const brId = `br${bi}`
    col1 += `<div class="lhb-br-item" data-br="${brId}" style="top:${b._y}px" title="${_lhbAttr(b.name)}">
      <div style="display:flex;align-items:center;gap:2px">${tag}<span class="lhb-br-name">${_lhbEsc(b.name)}</span></div>
      <span class="lhb-amt-b">净买 +${amt}</span>
    </div>`
    // 连线: 营业部 → 股票
    Array.from(b.codes).forEach(code=>{
      const s = stocks.find(x=>x.code===code); if(!s) return
      const y1=b._y+ROW_H/2, y2=s._y+ROW_H/2
      const x1=C1W, x2=C2X, mx=(x1+x2)/2
      svgPaths+=`<path data-br="${brId}" data-code="${s.code}" d="M${x1},${y1} C${mx+18},${y1} ${mx-18},${y2} ${x2},${y2}" class="lhb-edge-buy" fill="none"/>`
    })
  })
  col1 += '</div>'

  // ── Column 2: 股票 ──────────────────────────────────────────
  let col2 = `<div class="lhb-col" style="left:${C2X}px;width:${C2W}px">
    <div class="lhb-col-hd">龙虎榜股票</div>`
  stocks.forEach(s=>{
    const net=s.net_amt||0
    const ns = Math.abs(net)>=10000?(net/10000).toFixed(1)+'亿':net.toFixed(0)+'万'
    const cls= net>=0?'clr-up':'clr-dn'
    const chg= s.chg!=null ? `${s.chg>=0?'+':''}${s.chg}%` : ''
    const border = net>=0 ? 'var(--red)' : 'var(--green)'
    const tags = (s.youzai||[]).slice(0,2).map(y=>`<span class="lhb-yz-badge" style="font-size:9px;padding:1px 5px">${_lhbEsc(y)}</span>`).join('')
    const sectors = _lhbSectorBadges(s, true)
    col2 += `<div class="lhb-s-item" data-code="${s.code}" style="top:${s._y}px;border-left:3px solid ${border}"
      data-ui-click="showKLine('${s.code}','${_lhbJs(s.name||s.code)}')" title="${_lhbAttr(s.reason||'点击查看K线')}">
      <div style="display:flex;align-items:center;gap:4px">
        <span class="lhb-s-name">${_lhbEsc(s.name)}</span>
        <span class="${cls}" style="font-size:11px">${chg}</span>
      </div>
      <div style="display:flex;align-items:center;gap:3px;flex-wrap:wrap">
        <span class="${cls}" style="font-weight:700;font-size:12px">${net>=0?'+':''}${ns}</span>${sectors}${tags}
      </div>
    </div>`
    // 连线: 股票 → 游资
    ;(s.youzai||[]).forEach(yz=>{
      const y = youzaiList.find(x=>x.name===yz); if(!y) return
      const y1=s._y+ROW_H/2, y2=y._y+ROW_H/2
      const x1=C2X+C2W, x2=C3X, mx=(x1+x2)/2
      const yzIdx = youzaiList.findIndex(x=>x.name===yz)
      svgPaths+=`<path data-code="${s.code}" data-yz="yz${yzIdx}" d="M${x1},${y1} C${mx+18},${y1} ${mx-18},${y2} ${x2},${y2}" class="lhb-edge-yz" fill="none"/>`
    })
  })
  col2 += '</div>'

  // ── Column 3: 游资汇总 ──────────────────────────────────────
  let col3 = `<div class="lhb-col" style="left:${C3X}px;width:${C3W}px">
    <div class="lhb-col-hd">游资动向</div>`
  youzaiList.forEach((y,yi)=>{
    const t = y.total, at = Math.abs(t)
    const ns = at>=10000?(at/10000).toFixed(1)+'亿':at.toFixed(0)+'万'
    col3 += `<div class="lhb-yz-item" data-yz="yz${yi}" style="top:${y._y}px">
      <span class="lhb-yz-badge">${_lhbEsc(y.name)}</span>
      <div style="font-size:10px;color:var(--t3);margin-top:3px">${y.count}只 · ${t>=0?'+':'-'}${ns}</div>
    </div>`
  })
  col3 += '</div>'

  // 顶部摘要（与游资动向列同源，数量一致）
  const yzStat = youzaiList.slice(0,6)
    .map(y=>`<span class="lhb-yz-badge" style="font-size:11px">${_lhbEsc(y.name)}<b style="margin-left:4px;color:var(--t2)">${y.count}只</b></span>`)
    .join(' ')
  const hiddenBranches = allBranches.length > branches.length ? ` · 买方席位显示前${branches.length}/${allBranches.length}` : ''
  const summary = `<div style="margin-bottom:12px;font-size:12px;color:var(--t3);display:flex;flex-wrap:wrap;gap:6px;align-items:center">
    <span>${data.date} · <b style="color:var(--t1)">${data.total}</b>只上榜</span>
    <span>游资 ${youzaiList.length} 组${hiddenBranches}</span>
    ${yzStat}
  </div>`

  container.innerHTML = summary + `
  <div style="overflow-x:auto">
    <div class="lhb-graph-wrap" style="height:${totalH}px;min-width:${TOTAL_W}px;position:relative">
      ${col1}${col2}${col3}
      <svg class="lhb-svg" style="position:absolute;left:0;top:0;width:${TOTAL_W}px;height:${totalH}px;pointer-events:none">
        ${svgPaths}
      </svg>
    </div>
  </div>`
  _wireLhbGraphHover(container)
}

// ── 龙虎榜单渲染 ───────────────────────────────────────────────
function renderLhbList(data, container){
  if(!data.ok || !data.stocks?.length){
    container.innerHTML = `<div class="empty-hint">${data.error||'暂无数据'}</div>`
    return
  }
  const yzStat = (data.youzai_stat||[]).slice(0,8)
    .map(y=>`<span class="lhb-yz-badge">${_lhbEsc(y.name)}<b style="color:var(--t2);margin-left:3px">×${(y.stocks||[]).length}</b></span>`).join(' ')
  let html = `<div style="margin-bottom:10px;font-size:12px;color:var(--t3);display:flex;flex-wrap:wrap;gap:5px;align-items:center">
    <span>${_lhbEsc(data.date)} 共 <b style="color:var(--t1)">${data.total}</b> 只上榜</span>
    ${yzStat}
  </div>
  <table class="lhb-tbl">
    <thead><tr>
      <th style="min-width:360px">股票 / 席位明细</th>
      <th>涨幅</th><th>净买(万)</th><th>买入(万)</th><th>卖出(万)</th>
      <th>换手</th><th>上榜原因</th><th>游资</th>
    </tr></thead><tbody>`

  data.stocks.forEach((s,idx)=>{
    const net=s.net_amt||0
    const cc = net>=0?'clr-up':'clr-dn'
    const cc2= (s.chg||0)>=0?'clr-up':'clr-dn'
    const tags= (s.youzai||[]).map(y=>`<span class="lhb-yz-badge">${_lhbEsc(y)}</span>`).join(' ')
    const seatId = `lhbSeats_${s.code}_${idx}`
    const visibleSeats = 5
    const seats= (s.branches||[]).map((b,i)=>{
      const buy = Number(b.buy||0)
      const sell = Number(b.sell||0)
      const bnet = Number(b.net||0)
      const bc= bnet>=0?'#f97316':'#22c55e'
      const tg= b.tag?` <span class="lhb-yz-badge" style="font-size:9px;padding:1px 5px">${_lhbEsc(b.tag)}</span>`:''
      const extra = i>=visibleSeats ? ' lhb-seat-extra' : ''
      return `<div class="lhb-seat-row${extra}">
        <div class="lhb-seat-name" title="${_lhbAttr(b.name||'')}">${_lhbEsc(b.name||'')}${tg}</div>
        <div class="lhb-seat-amt" style="color:${bc}">${bnet>=0?'+':''}${bnet.toFixed(0)}万</div>
        <div class="lhb-seat-name" style="grid-column:1 / -1;color:var(--t3);font-size:10px">买 ${buy.toFixed(0)}万 / 卖 ${sell.toFixed(0)}万</div>
      </div>`
    }).join('') || '<div style="font-size:11px;color:var(--t3)">暂无席位明细</div>'
    const moreSeats = (s.branches||[]).length > visibleSeats
      ? `<button type="button" class="lhb-seat-toggle" data-seat-toggle="${seatId}" data-ui-click="toggleLhbSeats('${seatId}')">展开全部席位</button>`
      : ''
    const sectors = _lhbSectorBadges(s, false)
    html+=`<tr>
      <td>
        <div class="lhb-stock-head">
          <a class="lhb-code-link" data-ui-click="showKLine('${_lhbJs(s.code)}','${_lhbJs(s.name||s.code)}')" title="点击查看K线">
            <b>${_lhbEsc(s.name||'')}</b> <span style="font-size:10px;color:var(--t3)">${_lhbEsc(s.code)}</span>
          </a>${sectors}
        </div>
        <div id="${seatId}" class="lhb-seat-list">${seats}</div>${moreSeats}
      </td>
      <td class="${cc2}" style="font-weight:700;white-space:nowrap">${s.chg!=null?(s.chg>=0?'+':'')+s.chg+'%':''}</td>
      <td class="${cc}"  style="font-weight:700;white-space:nowrap">${net>=0?'+':''}${net.toFixed(0)}</td>
      <td>${s.buy_amt!=null?s.buy_amt.toFixed(0):''}</td>
      <td>${s.sell_amt!=null?s.sell_amt.toFixed(0):''}</td>
      <td style="white-space:nowrap">${s.turnover!=null?s.turnover+'%':''}</td>
      <td style="font-size:11px;color:var(--t2);min-width:100px">${_lhbEsc(s.reason||'')}</td>
      <td>${tags}</td>
    </tr>`
  })
  html += '</tbody></table>'
  container.innerHTML = html
}

// ── 复盘数据 ───────────────────────────────────────────────────
async function loadReviewDates(){
  try{
    const r = await fetch('/api/review/dates')
    const d = await r.json()
    const sel   = document.getElementById('rv-date')
    const today = _normDate(new Date())
    const dates = (d.dates||[]).slice()
    if(!dates.includes(today)) dates.unshift(today)
    sel.innerHTML = dates.map(x=>`<option value="${x}">${x===today ? x+'（今天）' : x}</option>`).join('')
  }catch(e){}
}

async function loadReview(force, silent){
  const sel  = document.getElementById('rv-date')
  const date = sel && sel.value ? sel.value : ''
  const left = document.getElementById('rv-left')
  const main = document.getElementById('rv-main')
  if(silent !== true){   // 静默刷新（盘中自动）不清空旧内容，避免闪屏
    left.innerHTML = '<div class="empty-hint">加载中…（盘中实时采集约需10-30秒）</div>'
    main.innerHTML = '<div class="empty-hint">加载中…</div>'
  }
  try{
    const ctrl = new AbortController()
    const tid  = setTimeout(()=>ctrl.abort(), 60000)
    const r    = await fetch(`/api/review/daily?date=${date}${force===true?'&refresh=1':''}`, {signal: ctrl.signal})
    clearTimeout(tid)
    const d = await r.json()
    if(!d.ok) throw new Error(d.error || '请求失败')
    const phaseTxt = {live:'盘中实时', close:'收盘版', evening:'晚间版', manual:'手动生成'}[d.phase] || d.phase
    document.getElementById('rv-phase').textContent = `${phaseTxt} · ${d.generated_at||''}`
    left.innerHTML = _renderSentiment(d.data||{})
    main.innerHTML = _renderReviewMain(d)
  }catch(e){
    if(silent === true) return   // 静默刷新失败不打扰，保留旧内容
    const msg = e.name==='AbortError' ? '请求超时（60s），请稍后重试' : e.message
    left.innerHTML = `<div class="empty-hint" style="color:#f85149">获取失败：${msg}</div>`
    main.innerHTML = ''
  }
}

async function generateReview(){
  // 异步模式：POST 立即返回，后台采集+AI约1-2分钟，前端每4秒轮询状态
  const btn = document.getElementById('rv-gen-btn')
  const sel = document.getElementById('rv-date')
  btn.disabled = true; btn.textContent = '⏳ 启动中…'
  try{
    const r = await api('/review/generate', 'POST', {date: sel ? sel.value : undefined})
    if(!r) throw new Error('请求失败（可能需要先登录）')
    if(!r.ok) throw new Error(r.error || '启动失败')
    // 轮询直到后台完成（最多8分钟）
    const t0 = Date.now()
    while(Date.now() - t0 < 480000){
      await new Promise(res=>setTimeout(res, 4000))
      const el = Math.round((Date.now()-t0)/1000)
      btn.textContent = `⏳ 生成中 ${el}s（采集+AI约1-2分钟）`
      try{
        const st = await (await fetch('/api/review/status')).json()
        if(!st.running){
          if(st.success === false) throw new Error(st.error || '生成失败')
          if(st.error) alert('提示：' + st.error)   // 数据归档成功但AI部分失败
          await loadReviewDates()
          if(sel && st.date) sel.value = st.date
          await loadReview()
          break
        }
      }catch(pe){ if(pe.message && !/fetch/i.test(pe.message)) throw pe }
    }
  }catch(e){
    alert('生成失败：' + e.message)
  }
  btn.disabled = false; btn.textContent = '⚡ 生成AI复盘'
}

// ── 渲染：左栏情绪温度计 ───────────────────────────────────────
function _rvEsc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') }
function _rvAttr(s){ return _rvEsc(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;') }
function _rvSafeUrl(s){
  try{
    const url = new URL(String(s||''), window.location.origin)
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : ''
  }catch(e){ return '' }
}
function _rvChg(v){
  const up = (v||0) >= 0
  return `<b style="color:${up?'#f85149':'#3fb950'}">${up?'+':''}${v}%</b>`   // A股红涨绿跌
}

// 涨跌结构卡片内的领涨/领跌板块行（板块接口拉不到时降级：从涨停天梯统计涨停集中行业）
function _rvBoardRows(data){
  const sec = data.sectors||{}
  let h = ''
  const top = (sec.industry_top||[]).slice(0,3)
  const bot = (sec.industry_bottom||[]).slice(-3)
  if(top.length){
    h += `<div class="rv-row" style="align-items:flex-start"><span style="flex-shrink:0">领涨板块</span><span style="text-align:right">`
    h += top.map(s=>`<span class="mr-sector-tag up" style="margin:1px 0 1px 4px">${_rvEsc(s.name)} +${s.chg}%</span>`).join('')
    h += `</span></div>`
  } else {
    const cnt = {}
    ;((data.zt||{}).ladder||[]).forEach(lv=>(lv.stocks||[]).forEach(s=>{
      if(s.industry) cnt[s.industry] = (cnt[s.industry]||0) + 1
    }))
    const tops = Object.entries(cnt).sort((a,b)=>b[1]-a[1]).slice(0,3)
    if(tops.length){
      h += `<div class="rv-row" style="align-items:flex-start"><span style="flex-shrink:0">涨停集中</span><span style="text-align:right">`
      h += tops.map(([name,n])=>`<span class="mr-sector-tag up" style="margin:1px 0 1px 4px">${_rvEsc(name)} ×${n}</span>`).join('')
      h += `</span></div>`
    }
  }
  if(bot.length){
    h += `<div class="rv-row" style="align-items:flex-start"><span style="flex-shrink:0">领跌板块</span><span style="text-align:right">`
    h += bot.map(s=>`<span class="mr-sector-tag dn" style="margin:1px 0 1px 4px">${_rvEsc(s.name)} ${s.chg}%</span>`).join('')
    h += `</span></div>`
  }
  return h
}

function _renderSentiment(data){
  const zt = data.zt||{}, mk = data.market||{}, b = mk.breadth||{}, tv = data.turnover||{}
  let html = ''

  if(data.sentiment_score !== null && data.sentiment_score !== undefined){
    const sc  = data.sentiment_score
    const lbl = sc>=70?'亢奋':sc>=55?'偏暖':sc>=45?'中性':sc>=30?'偏冷':'冰点'
    html += `<div class="daily-card">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <span style="font-size:12px;color:var(--t3);font-weight:600">情绪温度</span>
        <span style="font-size:22px;font-weight:700">${sc}<span style="font-size:12px;color:var(--t3)"> / 100 · ${lbl}</span></span>
      </div>
      <div class="rv-temp"><i style="left:calc(${sc}% - 2px)"></i></div>
    </div>`
  }

  const idx = mk.indices||{}
  if(Object.keys(idx).length){
    html += `<div class="daily-card"><div style="font-size:12px;color:var(--t3);margin-bottom:6px;font-weight:600">主要指数</div>`
    for(const [name, v] of Object.entries(idx)){
      const up = (v.change||0) >= 0
      html += `<div class="mr-index-row"><span class="mr-idx-name">${name}</span>
        <span class="mr-idx-price">${v.price}</span>
        <span class="mr-idx-chg ${up?'up':'dn'}">${up?'+':''}${v.change}%</span></div>`
    }
    html += `</div>`
  }

  html += `<div class="daily-card"><div style="font-size:12px;color:var(--t3);margin-bottom:6px;font-weight:600">涨跌结构</div>
    <div class="mr-breadth">
      <div class="mr-stat"><div class="mr-stat-val" style="color:#f85149">${b.up ?? '—'}</div><div class="mr-stat-lbl">上涨</div></div>
      <div class="mr-stat"><div class="mr-stat-val" style="color:#3fb950">${b.down ?? '—'}</div><div class="mr-stat-lbl">下跌</div></div>
      <div class="mr-stat"><div class="mr-stat-val" style="color:#f85149">${zt.zt_count ?? '—'}</div><div class="mr-stat-lbl">涨停</div></div>
      <div class="mr-stat"><div class="mr-stat-val" style="color:#3fb950">${zt.dt_count ?? '—'}</div><div class="mr-stat-lbl">跌停</div></div>
    </div>
    <div class="rv-row"><span>炸板率</span><b>${zt.zb_rate!=null ? zt.zb_rate+'%' : '—'}</b></div>
    <div class="rv-row"><span>最高连板</span><b>${zt.max_height ? zt.max_height+' 板' : '—'}</b></div>
    <div class="rv-row"><span>昨涨停今表现</span>${zt.prev_zt_avg!=null ? _rvChg(zt.prev_zt_avg) : '<b>—</b>'}</div>
    <div class="rv-row"><span>昨涨停红盘率</span><b>${zt.prev_zt_red_rate!=null ? zt.prev_zt_red_rate+'%' : '—'}</b></div>
    ${_rvDataQuality(b, '涨跌结构')}
    ${_rvBoardRows(data)}
  </div>`

  if(tv.today || (tv.trend||[]).length){   // 趋势拉不到（代理环境）时也要显示今日成交额
    const trend = tv.trend||[]
    const max   = trend.length ? Math.max(...trend.map(x=>x.amount)) : 0
    html += `<div class="daily-card">
      <div style="display:flex;justify-content:space-between"><span style="font-size:12px;color:var(--t3);font-weight:600">两市成交额</span>
      <b>${tv.today ? tv.today+' 亿' : '—'}</b></div>`
    if(trend.length){
      html += `<div class="rv-bars">${trend.map((x,i)=>`<i class="${i===trend.length-1?'last':''}" style="height:${Math.max(3,Math.round(x.amount/max*100))}%" title="${x.date}  ${x.amount}亿"></i>`).join('')}</div>
      <div style="font-size:10px;color:var(--t3);text-align:right;margin-top:2px">近${trend.length}个交易日</div>`
    }
    html += _rvDataQuality(tv, '成交额')
    html += `</div>`
  }

  const sec = data.sectors||{}
  if((sec.industry_top||[]).length){
    html += `<div class="daily-card"><div style="font-size:12px;color:var(--t3);margin-bottom:6px;font-weight:600">板块快览</div><div>`
    sec.industry_top.slice(0,5).forEach(s=> html += `<span class="mr-sector-tag up">${_rvEsc(s.name)} +${s.chg}%</span>`)
    ;(sec.industry_bottom||[]).slice(0,5).forEach(s=> html += `<span class="mr-sector-tag dn">${_rvEsc(s.name)} ${s.chg}%</span>`)
    html += `</div></div>`
  }

  return html || '<div class="empty-hint">暂无数据</div>'
}

function _rvDataQuality(stat, label){
  if(!stat) return ''
  const sourceMap = {
    eastmoney_three_market_verified: '东方财富三市校准',
    official_exchange: '交易所官方',
    eastmoney_spot: '东方财富实时',
    legu: '乐咕市场温度',
    tdx_quotes: 'TDX实时行情',
    tdx_index: 'TDX指数行情'
  }
  const source = stat.source ? (sourceMap[stat.source] || stat.source) : ''
  const parts = []
  if(source) parts.push(`来源：${_rvEsc(source)}`)
  if(stat.stat_date) parts.push(`日期：${_rvEsc(stat.stat_date)}`)
  if(stat.coverage != null) parts.push(`覆盖率：${Math.round(Number(stat.coverage) * 1000) / 10}%`)
  if(!parts.length && !stat.degraded) return ''
  const degraded = !!stat.degraded
  const reason = degraded && stat.reason ? ` · ${_rvEsc(stat.reason)}` : ''
  return `<div style="margin-top:8px;font-size:10px;line-height:1.45;color:${degraded?'#d29922':'var(--t4)'}">${label}数据${degraded?'降级':'确认'} · ${parts.join(' · ')}${reason}</div>`
}

// ── 渲染：中栏 AI总结 + 天梯 + 轮动 ───────────────────────────
function _renderReviewMain(d){
  const data = d.data||{}
  let html = ''

  html += `<div class="daily-card" style="border-color:rgba(88,166,255,.4)">
    <div style="font-size:12px;color:var(--bl);font-weight:700;margin-bottom:8px">🤖 AI 复盘总结
      ${d.phase==='evening' ? '<span style="font-size:10px;color:var(--t3)">（晚间版 · 含新闻联播与晚间要闻）</span>' : ''}</div>
    ${d.ai_summary
      ? `<div class="rv-ai-body">${_renderAiText(d.ai_summary)}</div>`
      : `<div class="empty-hint">尚未生成 AI 总结 —— 交易日 15:10 自动生成收盘版、19:50 晚间版；也可点右上角「⚡ 生成AI复盘」立即生成</div>`}
  </div>`

  const ladder = (data.zt||{}).ladder||[]
  if(ladder.length){
    html += `<div class="daily-card"><div style="font-size:12px;color:var(--t3);font-weight:600;margin-bottom:10px">🪜 涨停天梯（${(data.zt||{}).zt_count||0} 家）</div>`
    ladder.forEach(lv=>{
      html += `<div class="rv-ladder-lv"><div class="rv-ladder-h">${lv.height}板</div><div class="rv-ladder-stocks">`
      ;(lv.stocks||[]).forEach(s=>{
        html += `<span class="rv-stock-tag" title="${_rvEsc(s.code)}"><b>${_rvEsc(s.name)}</b>${s.industry?`<span>${_rvEsc(s.industry)}</span>`:''}</span>`
      })
      html += `</div></div>`
    })
    html += `</div>`
  }

  const sec = data.sectors||{}
  if((sec.industry_top||[]).length || (sec.concept_top||[]).length){
    html += `<div class="daily-card"><div style="font-size:12px;color:var(--t3);font-weight:600;margin-bottom:8px">🔄 板块轮动</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">`
    html += `<div><div style="font-size:11px;color:var(--t3);margin-bottom:4px">行业涨幅榜</div>`
    ;(sec.industry_top||[]).forEach(s=> html += `<div class="rv-row"><span>${_rvEsc(s.name)}</span>${_rvChg(s.chg)}</div>`)
    html += `</div><div><div style="font-size:11px;color:var(--t3);margin-bottom:4px">概念涨幅榜</div>`
    ;(sec.concept_top||[]).forEach(s=> html += `<div class="rv-row"><span>${_rvEsc(s.name)}</span>${_rvChg(s.chg)}</div>`)
    html += `</div></div></div>`
  }

  const news = (data.news||[]).filter(n=>n.important).slice(0,8)
  if(news.length){
    html += `<div class="daily-card"><div style="font-size:12px;color:var(--t3);font-weight:600;margin-bottom:6px">⭐ 当日要闻</div>`
    news.forEach(n=> html += `<div class="rv-news-item imp">
      <div class="rv-news-meta"><span>${(n.ts||'').slice(5,16)}</span><span class="rv-src-badge">${_rvEsc(n.source)}</span></div>
      <div class="rv-news-title">${_rvEsc(n.title)}</div></div>`)
    html += `</div>`
  }
  return html
}

function _renderAiText(t){
  let h = _rvEsc(t)
  h = h.replace(/【([^】]+)】/g, '<div class="rv-sec-h">【$1】</div>')
  h = h.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
  h = h.replace(/\n+/g, '<br>')
  h = h.replace(/(<div class="rv-sec-h">[^<]*<\/div>)<br>/g, '$1')
  return h
}

// ── 财经快讯 ───────────────────────────────────────────────────
let _newsBusy = false

async function loadNews(silent){
  if(_newsBusy) return
  _newsBusy = true
  const box = document.getElementById('rv-news-body')
  if(silent !== true) box.innerHTML = '<div class="empty-hint">加载中…</div>'
  try{
    const src = document.getElementById('rv-news-src').value
    const q   = document.getElementById('rv-news-q').value.trim()
    const imp = document.getElementById('rv-news-imp').checked
    const ps  = new URLSearchParams({limit:'100'})
    if(src) ps.set('source', src)
    if(q)   ps.set('q', q)
    if(imp) ps.set('important', '1')
    const r = await fetch('/api/news/feed?' + ps.toString())
    const d = await r.json()
    const items = d.items||[]
    document.getElementById('rv-news-count').textContent = items.length ? `${items.length} 条` : ''
    box.innerHTML = items.length
      ? items.map(_renderNewsItem).join('')
      : '<div class="empty-hint">暂无新闻（后台正在抓取，30秒后自动刷新）</div>'
  }catch(e){
    if(silent !== true) box.innerHTML = `<div class="empty-hint" style="color:#f85149">获取失败：${e.message}</div>`
  }
  _newsBusy = false
}

function _renderNewsItem(n){
  const t     = (n.ts||'').slice(5,16)
  const safeUrl = _rvSafeUrl(n.url)
  const title = safeUrl
    ? `<a href="${_rvAttr(safeUrl)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:none">${_rvEsc(n.title)}</a>`
    : _rvEsc(n.title)
  const body  = n.content && n.content !== n.title
    ? `<div class="rv-news-cnt">${_rvEsc(n.content.slice(0,120))}${n.content.length>120?'…':''}</div>` : ''
  return `<div class="rv-news-item${n.important?' imp':''}">
    <div class="rv-news-meta"><span>${t}</span><span class="rv-src-badge">${_rvEsc(n.source)}</span>${n.important?'<span class="rv-imp-badge">重要</span>':''}</div>
    <div class="rv-news-title">${title}</div>${body}</div>`
}

async function _loadNewsSources(){
  try{
    const r = await fetch('/api/news/sources')
    const d = await r.json()
    const sel = document.getElementById('rv-news-src')
    const cur = sel.value
    sel.innerHTML = '<option value="">全部来源</option>' +
      (d.sources||[]).map(s=>`<option value="${_rvEsc(s.source)}">${_rvEsc(s.source)}（${s.count}）</option>`).join('')
    sel.value = cur
  }catch(e){}
}
