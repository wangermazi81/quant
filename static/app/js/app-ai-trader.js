/* ══════════════ 🤖 AI 交易模块（本地）前端驱动 ══════════════ */
const aitS = { roles:[], agents:{}, decisions:[], execs:[], pending:{}, providers:{}, strategyCatalog:[], settings:null, config:null, tokenUsage:null, candidateTrace:null, es:null, inited:false, loading:false, curStock:'', lastError:'' };

function _aiActionAttrs(action, value){
  return `data-ai-action="${action}" data-ai-value="${encodeURIComponent(String(value ?? ''))}"`;
}

document.addEventListener('click', e=>{
  const target=e.target.closest('[data-ai-action]');
  if(!target) return;
  e.stopPropagation();
  const value=decodeURIComponent(target.dataset.aiValue||'');
  const actions={
    'show-decision': ()=>aitShowDec(value),
    'confirm': ()=>aitConfirm(value,true),
    'reject': ()=>aitConfirm(value,false),
    'remove-watch': ()=>aitRemoveWatch(value),
  };
  const action=actions[target.dataset.aiAction];
  if(action) action();
});

async function initAiTrader(){
  if(aitS.loading) return;
  if(aitS.inited){ aitRefreshStatus(); return; }
  aitS.loading = true;
  aitSetHealth('loading','正在连接 AI 交易引擎','读取模型、风控、账户和实时流');
  try{
    const meta = await api('/ai_trader/meta');
    if(!meta || !meta.ok) throw new Error(aitApiMsg(meta,'AI 交易元信息加载失败'));
    aitS.roles = meta.roles||[]; aitS.providers = meta.providers||{}; aitS.strategyCatalog = meta.strategy_candidates||[];
    aitFillProviders(); aitRenderStrategyOptions();
    await aitLoadConfig();
    await aitLoadSettings();
    const snap = await api('/ai_trader/snapshot');
    if(!snap || !snap.ok) throw new Error(aitApiMsg(snap,'AI 交易快照加载失败'));
    aitApplySnapshot(snap);
    aitRenderAgents();
    aitConnectSSE();
    await aitLoadAccount();
    if(!aitS.acctTimer) aitS.acctTimer = setInterval(aitLoadAccount, 15000);
    aitS.inited = true;
    aitS.lastError = '';
    aitRenderHealth();
  }catch(e){
    aitS.inited = false;
    aitS.lastError = e && e.message ? e.message : String(e||'初始化失败');
    aitSetHealth('error','AI 交易加载失败', aitS.lastError);
    aitRenderAgents('<div class="ait-empty ait-error">'+esc(aitS.lastError)+'<br><button class="btn btn-blue btn-sm" onclick="initAiTrader()">重新连接</button></div>');
    fbToast(aitS.lastError, true);
  }finally{
    aitS.loading = false;
  }
}

function aitConnectSSE(){
  if(aitS.es){ try{aitS.es.close()}catch(e){} }
  const es = new EventSource(API+'/ai_trader/stream');
  aitS.es = es;
  es.onopen = ()=>{ aitS.lastError=''; aitRenderHealth(); };
  es.onerror = ()=>{ aitS.lastError='实时流断开，页面会自动重连；若一直断开，多半是登录态过期或后端进程未刷新'; aitRenderHealth(); };
  es.addEventListener('snapshot', e=> aitSafeEvent(e, aitApplySnapshot));
  es.addEventListener('ai_status', e=> aitSafeEvent(e, aitApplyStatus));
  es.addEventListener('ai_token_usage', e=> aitSafeEvent(e, d=>{ aitS.tokenUsage=d; aitRenderTokenUsage(); }));
  es.addEventListener('ai_round', e=> aitSafeEvent(e, d=>{ const el=document.getElementById('aitRound'); if(d.phase==='start'&&el) el.textContent='第 '+d.round_no+' 轮'; }));
  es.addEventListener('ai_agent', e=> aitSafeEvent(e, aitOnAgent));
  es.addEventListener('ai_decision', e=> aitSafeEvent(e, aitOnDecision));
  es.addEventListener('ai_exec', e=> aitSafeEvent(e, aitOnExec));
  es.addEventListener('ai_pending', e=> aitSafeEvent(e, aitOnPending));
  es.addEventListener('ai_pending_resolved', e=> aitSafeEvent(e, d=>{ delete aitS.pending[d.id]; aitRenderPending(); }));
  es.addEventListener('ai_account', e=> aitSafeEvent(e, d=>{ aitRenderAccount(d.account, d.positions); }));
}

function aitSafeEvent(e, fn){
  try{ fn(JSON.parse(e.data)); }
  catch(err){ aitS.lastError='实时事件解析失败：'+(err&&err.message?err.message:err); aitRenderHealth(); }
}

function aitApiMsg(r, fallback){ return (r && (r.msg || r.error)) || fallback; }

function aitApplySnapshot(s){
  if(s.roles && s.roles.length) aitS.roles = s.roles;
  aitS.agents = {}; (s.agent_cards||[]).forEach(c=> aitS.agents[c.role]=c);
  aitS.decisions = s.decisions||[];
  aitS.execs = s.execs||[];
  aitS.pending = {}; (s.pending||[]).forEach(p=> aitS.pending[p.id]=p);
  if(s.token_usage) aitS.tokenUsage = s.token_usage;
  if(s.candidate_trace) aitS.candidateTrace = s.candidate_trace;
  aitApplyStatus(s);
  aitRenderAgents(); aitRenderDecisions(); aitRenderExecs(); aitRenderPending(); aitRenderTokenUsage(); aitRenderCandidateTrace();
  aitRenderHealth();
}

function aitApplyStatus(s){
  const sb=document.getElementById('aitStatus');
  if(sb){
    if(s.halted_reason){ sb.className='ait-badge halt'; sb.textContent='🔥 已熔断'; sb.title=s.halted_reason; }
    else if(s.running && s.waiting_reason){ sb.className='ait-badge run'; sb.textContent='● 等待中'; sb.title=s.waiting_reason; }
    else if(s.running){ sb.className='ait-badge run'; sb.textContent='● 运行中'; sb.title=s.next_round_in_sec>0?('下次扫描约 '+s.next_round_in_sec+' 秒后'):''; }
    else { sb.className='ait-badge stop'; sb.textContent='● 已停止'; sb.title=''; }
  }
  const mb=document.getElementById('aitMode'), live=(s.broker_mode==='live');
  if(mb){ mb.className='ait-badge '+(live?'live':'paper'); mb.textContent = live?'实盘(需人工确认)':'模拟盘(自动)'; }
  const roundEl=document.getElementById('aitRound'); if(roundEl && typeof s.round_no==='number') roundEl.textContent='第 '+s.round_no+' 轮';
  if(s.settings){ aitS.settings=s.settings; aitRenderStrategyBadge(); }
  if(s.token_usage){ aitS.tokenUsage=s.token_usage; aitRenderTokenUsage(); }
  if(s.candidate_trace){ aitS.candidateTrace=s.candidate_trace; aitRenderCandidateTrace(); }
  const st=document.getElementById('aitBtnStart'); if(st) st.disabled=!!s.running;
  const sp=document.getElementById('aitBtnStop'); if(sp) sp.disabled=!s.running;
  const once=!!s.once_running;
  const ro=document.getElementById('aitBtnRunOnce'); if(ro){ ro.disabled=once||!!s.running; ro.textContent=once?'⏳ 分析中…':'🔄 单轮测试'; }
  const so=document.getElementById('aitBtnStopOnce'); if(so) so.style.display=once?'inline-flex':'none';
  aitRenderHealth(s);
}

function aitSetHealth(level, title, detail){
  aitS.health = {level:level||'idle', title:title||'', detail:detail||''};
  aitRenderHealth();
}

function aitRenderHealth(status){
  const box=document.getElementById('aitHealth'); if(!box) return;
  const cfg=aitS.config||{}, s=status||{}, settings=aitS.settings||{};
  const modelOk=!!(cfg.enabled && cfg.has_key && cfg.model);
  const streamOk=!!(aitS.es && aitS.es.readyState===1);
  const err=aitS.lastError || (aitS.health&&aitS.health.detail) || '';
  const items=[
    {cls:modelOk?'ok':'warn', k:'模型', v:modelOk?('已启用 · '+esc(cfg.model)):'未就绪', sub:modelOk?'AI交易会调用当前用户模型':'点“模型”配置 Key、模型并启用'},
    {cls:streamOk?'ok':(aitS.inited?'warn':'idle'), k:'实时流', v:streamOk?'已连接':'未连接', sub:streamOk?'团队思考/订单会实时刷新':(err||'打开 AI 交易页后自动连接')},
    {cls:s.running?'ok':'idle', k:'执行', v:s.running?'运行中':'待命', sub:s.waiting_reason || (s.once_running?'单轮分析中':'可启动常驻或单轮测试')},
    {cls:(settings.strategy_candidates_enabled!==false)?'ok':'warn', k:'候选源', v:(settings.strategy_candidates_enabled!==false)?'策略雷达开启':'策略雷达关闭', sub:aitStrategyHealthText(settings)},
  ];
  box.innerHTML=items.map(it=>'<div class="ait-health-card '+it.cls+'"><span>'+it.k+'</span><b>'+it.v+'</b><em>'+it.sub+'</em></div>').join('');
}

function aitStrategyHealthText(s){
  const files=s.strategy_candidate_strategies||[];
  if(files.length) return files.slice(0,2).map(aitStrategyName).join(' / ')+(files.length>2?' 等':'');
  return '默认策略库 + 全市场初筛 + 自选池';
}

function aitOnAgent(c){
  aitS.agents[c.role]=c;
  if(c.code){ aitS.curStock=c.code; const el=document.getElementById('aitCurStock'); if(el)el.textContent='当前标的：'+c.code; }
  aitRenderAgents();
}
function aitOnDecision(d){
  const i=aitS.decisions.findIndex(x=>x.id===d.id);
  if(i>=0) aitS.decisions[i]=d; else aitS.decisions.push(d);
  aitS.decisions=aitS.decisions.slice(-60); aitRenderDecisions();
}
function aitOnExec(r){ aitS.execs.push(r); aitS.execs=aitS.execs.slice(-60); aitRenderExecs(); aitLoadAccount(); }
function aitOnPending(p){ aitS.pending[p.id]=p; aitRenderPending(); }

const AIT_ORDER=['lead','market','technical','fundamental','bull','bear','risk'];
function aitRenderAgents(forcedHtml){
  const box=document.getElementById('aitAgents'); if(!box) return;
  if(forcedHtml){ box.innerHTML=forcedHtml; return; }
  const rm={}; aitS.roles.forEach(r=>rm[r.role]=r);
  let html='';
  AIT_ORDER.forEach(role=>{
    const meta=rm[role]; if(!meta) return;
    const c=aitS.agents[role]||{}, st=c.status||'idle';
    const stCls=st==='thinking'?'ait-st-think':(st==='done'?'ait-st-done':'ait-st-idle');
    const stTxt=st==='thinking'?'思考中…':(st==='done'?'已结论':'待命');
    html+='<div class="ait-agent '+(meta.kind==='lead'?'lead':'')+(st==='thinking'?' active':'')+'">'
      +'<div class="ait-agent-hd">'+(meta.icon||'🤖')+' '+esc(meta.name)
      +'<span class="ait-agent-st '+stCls+'">'+stTxt+'</span></div>'
      +(c.code?'<div class="ait-agent-sub">标的 '+esc(c.code)+'</div>':'')
      +(c.text?'<div class="ait-agent-txt">'+esc(c.text)+'</div>':'')
      +(c.conclusion?'<div class="ait-agent-concl">▸ '+esc(c.conclusion)+'</div>':'')
      +'</div>';
  });
  box.innerHTML=html;
}
function aitRenderDecisions(){
  const box=document.getElementById('aitDecisions'); if(!box) return;
  if(!aitS.decisions.length){ box.innerHTML='<div class="ait-empty">启动后，主Agent的裁决将在此实时显示；点某只票可展开团队各角色对它的建议</div>'; return; }
  let html='';
  aitS.decisions.slice().reverse().forEach(d=>{
    const hasAg=d.agents&&d.agents.length;
    html+='<div class="ait-dec '+(d.blocked?'blocked':'')+'"'+(hasAg?' '+_aiActionAttrs('show-decision',d.id)+' style="cursor:pointer" title="点击查看团队对该票的详细分析"':'')+'>'
      +'<div class="ait-dec-hd">'
      +'<span class="ait-act '+esc(d.action)+'">'+esc(d.action)+'</span>'
      +'<span class="ait-dec-code">'+esc(d.code)+'</span><span class="ait-dec-name">'+esc(d.name||'')+'</span>'
      +'<span class="ait-dec-conf">信心 '+(d.confidence||0)+' · '+esc(d.time||'')+(hasAg?' 🔍':'')+'</span></div>'
      +(d.reason?'<div class="ait-dec-reason">'+esc(d.reason)+'</div>':'')
      +(d.consensus?('<div class="ait-dec-cons">🗳️ 个股票 多'+(d.consensus.long||0)+'/空'+(d.consensus.short||0)+'/中'+(d.consensus.neutral||0)+(d.consensus.market_cn?' · 市场'+esc(d.consensus.market_cn):'')+' · '+(d.consensus.buy_ok?'<b class="ok">看多达标✓</b>':'<b class="no">看多未达标✗</b>')+'</div>'):'')
      +aitDecisionTokenHtml(d)
      +(d.blocked?'<div class="ait-dec-block">🛡️ 风控拦截：'+esc(d.blocked)+'</div>':'')
      +'</div>';
  });
  box.innerHTML=html;
}
function aitAgentsHtml(agents){
  return (agents||[]).map(a=>'<div class="ait-dec-ag">'
    +'<div class="ait-dec-ag-h">'+(a.icon||'🤖')+' '+esc(a.name||a.role||'')+'</div>'
    +(a.conclusion?'<div class="ait-dec-ag-c">▸ '+esc(a.conclusion)+'</div>':'')
    +(a.text?'<div class="ait-dec-ag-t">'+esc(a.text)+'</div>':'')
    +'</div>').join('');
}
function aitShowDec(id){
  const d=aitS.decisions.find(x=>x.id===id); if(!d) return;
  const cons=d.consensus?('<div class="ait-dec-cons" style="margin:8px 0">🗳️ 个股方向票(技术/基本面/多空)：看多'+(d.consensus.long||0)+' / 看空'+(d.consensus.short||0)+' / 中性'+(d.consensus.neutral||0)+'｜市场环境：<b>'+esc(d.consensus.market_cn||'中性')+'</b> · '+(d.consensus.buy_ok?'<b class="ok">看多达标✓ 允许买入</b>':'<b class="no">看多未达标✗（'+esc(d.consensus.market_cn||'本')+'市买入需≥'+(d.consensus.eff_min||d.consensus.min||'?')+'票个股看多且多&gt;空）</b>')+'</div>'):'';
  document.getElementById('aitDecModalBox').innerHTML=
    '<h3 style="display:flex;align-items:center;gap:8px"><span class="ait-act '+esc(d.action)+'">'+esc(d.action)+'</span>'+esc(d.code)+' <span style="color:var(--t3);font-weight:600">'+esc(d.name||'')+'</span></h3>'
    +'<div style="font-size:12px;color:var(--t3);margin-bottom:8px">信心 '+(d.confidence||0)+' · '+esc(d.time||'')+(d.holding?' · <b style="color:#3D6FD0">当前持仓</b>':'')+'</div>'
    +(d.reason?'<div class="ait-dec-reason" style="margin-bottom:4px"><b>🎯 主Agent裁决：</b>'+esc(d.reason)+'</div>':'')
    +cons
    +aitDecisionTokenHtml(d)
    +(d.blocked?'<div class="ait-dec-block">🛡️ 风控拦截：'+esc(d.blocked)+'</div>':'')
    +'<div class="ait-sec-hd" style="margin-top:12px">👥 团队各角色对该票的建议</div>'
    +(aitAgentsHtml(d.agents)||'<div class="ait-empty">无角色留痕</div>')
    +'<div style="display:flex;justify-content:flex-end;margin-top:14px"><button class="btn btn-gray btn-sm" onclick="document.getElementById(\'aitDecModal\').classList.remove(\'show\')">关闭</button></div>';
  document.getElementById('aitDecModal').classList.add('show');
}
function aitRenderExecs(){
  const box=document.getElementById('aitExecs'); if(!box) return;
  if(!aitS.execs.length){ box.innerHTML='<div class="ait-empty">暂无执行记录</div>'; return; }
  let html='';
  aitS.execs.slice().reverse().forEach(r=>{
    html+='<div class="ait-exec"><span class="ait-act '+esc(r.action)+'">'+esc(r.action)+'</span>'
      +'<b>'+esc(r.code)+'</b> '+esc(r.name||'')+' @'+(r.price||'-')
      +'<span style="margin-left:auto" class="'+(r.ok?'ok':'fail')+'">'+(r.ok?'✅':'❌')+' '+esc(r.msg||'')+'</span></div>';
  });
  box.innerHTML=html;
}
function aitRenderPending(){
  const box=document.getElementById('aitPending'); if(!box) return;
  const arr=Object.values(aitS.pending);
  if(!arr.length){ box.innerHTML=''; return; }
  box.innerHTML=arr.map(p=>'<div class="ait-pend"><div style="font-weight:700">⏳ 实盘待确认 · '
    +'<span class="ait-act '+esc(p.action)+'">'+esc(p.action)+'</span> '+esc(p.code)+' '+esc(p.name||'')+'</div>'
    +'<div style="font-size:12px;color:var(--t2);margin-top:4px">价 '+(p.price||'-')+' · 信心 '+(p.confidence||0)+' · '+esc(p.reason||'')+'</div>'
    +'<div class="ait-pend-row"><button class="btn btn-green btn-sm" '+_aiActionAttrs('confirm',p.id)+'>✓ 确认下单</button>'
    +'<button class="btn btn-gray btn-sm" '+_aiActionAttrs('reject',p.id)+'>✕ 拒绝</button></div></div>').join('');
}

async function aitControl(path, fallback){
  const r=await api(path,'POST');
  const msg=aitApiMsg(r, fallback);
  if(!r || !r.ok){ aitS.lastError=msg; aitRenderHealth(); }
  else aitS.lastError='';
  fbToast(msg, !r || !r.ok);
  await aitRefreshStatus();
  return r;
}
async function aitRefreshStatus(){
  const r=await api('/ai_trader/snapshot');
  if(r&&r.ok) aitApplySnapshot(r);
  return r;
}
async function aitStart(){ await aitControl('/ai_trader/start','AI 交易启动失败'); }
async function aitStop(){ await aitControl('/ai_trader/stop','AI 交易已停止'); }
async function aitEmergency(){ if(!confirm('确定一键急停？将停止AI交易并清空待确认。')) return; await aitControl('/ai_trader/emergency_stop','AI 交易已急停'); }
async function aitRunOnce(){ await aitControl('/ai_trader/run_once','单轮测试触发失败'); }
async function aitStopOnce(){ await aitControl('/ai_trader/stop_once','单轮测试停止失败'); }

/* 🤖 AI 独立模拟账户 */
function aitFmt(n){ return (Number(n)||0).toLocaleString('zh-CN',{maximumFractionDigits:0}); }
async function aitLoadAccount(){ const r=await api('/ai_trader/account'); if(r&&r.ok) aitRenderAccount(r.account, r.positions); return r; }
function aitRenderAccount(acc, poss){
  const a=document.getElementById('aitAccount');
  if(a&&acc){
    const ret=acc.total_return||0, rc=ret>=0?'up':'dn';
    a.innerHTML='<div class="ait-acct-grid">'
      +'<div><span>账户权益</span><b>¥'+aitFmt(acc.equity)+'</b></div>'
      +'<div><span>可用现金</span><b>¥'+aitFmt(acc.cash)+'</b></div>'
      +'<div><span>累计收益率</span><b class="'+rc+'">'+(ret>=0?'+':'')+ret.toFixed(2)+'%</b></div>'
      +'<div><span>持仓数</span><b>'+(acc.position_count||0)+' 只</b></div></div>';
  }
  const pb=document.getElementById('aitPositions');
  if(pb){
    if(!poss||!poss.length){ pb.innerHTML='<div class="ait-empty">AI 暂无持仓</div>'; }
    else pb.innerHTML=poss.map(p=>{ const up=(p.pnl_pct||0)>=0;
      return '<div class="ait-pos-row"><b>'+esc(p.code)+'</b><span class="ait-pos-nm">'+esc(p.name||'')+'</span>'
        +'<span>'+(p.shares||0)+'股</span><span>现'+(p.cur_price||'-')+'</span>'
        +'<span class="'+(up?'up':'dn')+'">'+(up?'+':'')+(p.pnl_pct||0).toFixed(2)+'%</span></div>';
    }).join('');
  }
}
async function aitResetAccount(){
  const v=prompt('重置 AI 独立模拟账户 —— 输入初始资金（元）：','1000000');
  if(v===null) return;
  const r=await api('/ai_trader/account/reset','POST',{initial_capital:parseFloat(v)||1000000});
  fbToast((r&&r.msg)||'已重置', r&&!r.ok); aitLoadAccount();
}
async function aitConfirm(id,ap){ const r=await api('/ai_trader/confirm','POST',{id:id,approve:ap}); fbToast(aitApiMsg(r, ap?'确认完成':'已拒绝'), !r||!r.ok); if(r&&r.ok){ delete aitS.pending[id]; aitRenderPending(); aitLoadAccount(); } }

function aitStrategyName(file){
  const it=(aitS.strategyCatalog||[]).find(x=>x.file===file);
  return (it&&it.name)||file;
}
function aitRenderTokenUsage(){
  const el=document.getElementById('aitToken'); if(!el) return;
  const u=aitS.tokenUsage||{};
  const total=Number(u.total_tokens||0), calls=Number(u.calls||0);
  el.textContent='Token '+(u.estimated!==false?'估算 ':'')+aitFmt(total)+' · '+calls+'次';
  const last=u.last_call||{};
  el.title='累计 Prompt '+aitFmt(u.prompt_tokens||0)+'，Completion '+aitFmt(u.completion_tokens||0)+(last.total_tokens?('；最近一次 '+aitFmt(last.total_tokens)):'');
}
function aitRenderStrategyBadge(){
  const el=document.getElementById('aitStrategyBadge'); if(!el) return;
  const s=aitS.settings||{}, files=s.strategy_candidate_strategies||[];
  const enabled=s.strategy_candidates_enabled!==false;
  const names=(files.length?files:[]).slice(0,3).map(aitStrategyName);
  el.textContent=enabled?('策略：'+(names.join(' / ')||'默认候选链')):'策略：已关闭';
  el.title=enabled?(files.map(aitStrategyName).join(' / ')||'默认候选链'):'策略库候选已关闭';
}
function aitRenderCandidateTrace(){
  const box=document.getElementById('aitCandidateTrace'); if(!box) return;
  const tr=aitS.candidateTrace||{}, rows=tr.strategies||[], final=tr.final||[];
  if(!rows.length && !final.length){ box.innerHTML='<div class="ait-empty">等待下一轮候选扫描</div>'; return; }
  let html='';
  rows.forEach(r=>{
    const picks=(r.picked||[]).map(p=>esc(p.code)+(p.name?' '+esc(p.name):'')).join('，');
    html+='<div class="ait-trace-row"><b>'+esc(aitStrategyName(r.file||'策略库'))+'</b>'
      +'<span style="float:right;color:var(--t3)">'+(r.ok?'结果 '+(r.count||0):'未通过')+'</span>'
      +'<div class="ait-trace-sub">'+(picks?('入选：'+picks):(r.msg?('原因：'+esc(r.msg)):'暂无入选'))+'</div></div>';
  });
  if(final.length){
    html+='<div class="ait-trace-sub" style="padding:2px 4px">最终交给团队分析：'
      +final.map(p=>'<b>'+esc(p.code)+'</b>').join('，')+(tr.updated_at?' · '+esc(tr.updated_at):'')+'</div>';
  }
  box.innerHTML=html;
}
function aitRenderStrategyOptions(){
  const box=document.getElementById('aitStrategyChecks'); if(!box) return;
  const list=aitS.strategyCatalog||[];
  if(!list.length){ box.innerHTML='<div style="font-size:12px;color:var(--t3)">策略目录加载中…</div>'; return; }
  const selected=new Set((aitS.settings&&aitS.settings.strategy_candidate_strategies)||[]);
  box.innerHTML=list.map(it=>{
    const checked=selected.has(it.file) || (!selected.size && it.default);
    return '<label><input type="checkbox" name="aitStrategyPick" value="'+esc(it.file)+'" '+(checked?'checked':'')+'>'
      +'<span>'+esc(it.name||it.file)+'<span class="ait-check-sub">'+esc(it.file)+(it.desc?' · '+esc(it.desc):'')+'</span></span></label>';
  }).join('');
}
function aitDecisionTokenHtml(d){
  const u=(d&&d.token_usage)||{};
  const total=Number(u.total_tokens||0), calls=Number(u.calls||0);
  if(!total && !calls) return '';
  const parts=['Token '+(u.estimated!==false?'估算 ':'')+aitFmt(total)];
  if(calls) parts.push(calls+' 次模型调用');
  if(u.prompt_tokens||u.completion_tokens) parts.push('Prompt '+aitFmt(u.prompt_tokens||0)+' / Completion '+aitFmt(u.completion_tokens||0));
  return '<div class="ait-dec-token">🧾 '+parts.join(' · ')+'</div>';
}

function aitFillProviders(){
  const sel=document.getElementById('aitCfgProvider'); if(!sel) return;
  sel.innerHTML=Object.entries(aitS.providers).map(([k,v])=>'<option value="'+k+'">'+esc(v.name)+'</option>').join('');
}
function aitProviderChange(){
  const k=document.getElementById('aitCfgProvider').value, p=aitS.providers[k]; if(!p) return;
  if(p.base_url) document.getElementById('aitCfgBase').value=p.base_url;
  if(p.model) document.getElementById('aitCfgModel').value=p.model;
}
async function aitDetectModels(){
  const msg=document.getElementById('aitCfgMsg');
  msg.style.color='var(--t3)'; msg.textContent='🔍 检测可用模型中…';
  const body={ base_url:document.getElementById('aitCfgBase').value.trim() };
  const key=document.getElementById('aitCfgKey').value.trim(); if(key) body.api_key=key;
  const r=await api('/ai_trader/detect_models','POST',body);
  if(r&&r.ok){
    if(r.base_url && !document.getElementById('aitCfgBase').value.trim()) document.getElementById('aitCfgBase').value=r.base_url;
    if(r.provider_guess && aitS.providers[r.provider_guess]) document.getElementById('aitCfgProvider').value=r.provider_guess;
    const models=r.models||[];
    document.getElementById('aitCfgModelList').innerHTML=models.map(m=>'<option value="'+esc(m)+'">').join('');
    const sel=document.getElementById('aitCfgModelSel');
    sel.innerHTML='<option value="">— 选择模型（共 '+models.length+' 个）—</option>'+models.map(m=>'<option value="'+esc(m)+'">'+esc(m)+'</option>').join('');
    sel.style.display=models.length?'':'none';
    const mi=document.getElementById('aitCfgModel'); if(!mi.value.trim()&&models.length) mi.value=models[0];
    msg.style.color='#368B63';
    msg.textContent='✅ 检测到 '+models.length+' 个可用模型，下拉选择即可'+(r.total>models.length?'（已过滤非对话模型，原始 '+r.total+'）':'');
  }else{
    document.getElementById('aitCfgModelSel').style.display='none';
    msg.style.color='#D6456A'; msg.textContent='❌ '+((r&&r.error)||'检测失败');
  }
}
function aitAutoDetect(){
  // Key 输入完成(blur/回车)后自动探测；短于 8 位视为没填完不打扰
  const key=document.getElementById('aitCfgKey').value.trim();
  if(key.length>=8) aitDetectModels();
}
async function aitLoadConfig(){
  const r=await api('/ai_trader/config'); if(!r||!r.ok){ aitS.lastError=aitApiMsg(r,'模型配置读取失败'); aitRenderHealth(); return; } const c=r.config;
  aitS.config=c||{};
  if(c.provider) document.getElementById('aitCfgProvider').value=c.provider;
  document.getElementById('aitCfgBase').value=c.base_url||'';
  document.getElementById('aitCfgModel').value=c.model||'';
  document.getElementById('aitCfgEnabled').checked=!!c.enabled;
  document.getElementById('aitCfgKey').placeholder=c.has_key?('已配置 '+c.api_key_mask+'（留空不改）'):'sk-...';
  aitRenderHealth();
}
function aitOpenConfig(){ document.getElementById('aitConfigModal').classList.add('show'); }
async function aitTestConfig(){
  document.getElementById('aitCfgMsg').textContent='测试中…';
  const body={provider:document.getElementById('aitCfgProvider').value,base_url:document.getElementById('aitCfgBase').value,model:document.getElementById('aitCfgModel').value,api_key:document.getElementById('aitCfgKey').value};
  const r=await api('/ai_trader/config/test','POST',body);
  const ok=!!(r&&r.ok), msg=aitApiMsg(r,'测试失败');
  const el=document.getElementById('aitCfgMsg'); el.style.color=ok?'#368B63':'#D6456A'; el.textContent=msg;
}
async function aitSaveConfig(){
  const body={provider:document.getElementById('aitCfgProvider').value,base_url:document.getElementById('aitCfgBase').value,model:document.getElementById('aitCfgModel').value,enabled:document.getElementById('aitCfgEnabled').checked};
  const k=document.getElementById('aitCfgKey').value; if(k) body.api_key=k;
  const r=await api('/ai_trader/config','POST',body);
  if(r&&r.ok){ aitS.config={...(aitS.config||{}),...(r.config||{}), model:body.model, enabled:body.enabled}; fbToast('模型配置已保存'); document.getElementById('aitConfigModal').classList.remove('show'); document.getElementById('aitCfgKey').value=''; await aitLoadConfig(); }
  else { aitS.lastError=aitApiMsg(r,'保存失败'); aitRenderHealth(); fbToast(aitS.lastError,true); }
}

async function aitLoadSettings(){ const r=await api('/ai_trader/settings'); if(r&&r.ok){ aitS.settings=r.settings; aitFillSettings(); aitRenderHealth(); } else { aitS.lastError=aitApiMsg(r,'风控设置读取失败'); aitRenderHealth(); } }
function aitFillSettings(){
  const s=aitS.settings; if(!s) return; const r=s.risk||{};
  const set=(id,v)=>{const el=document.getElementById(id); if(el)el.value=v;};
  set('aitSetMaxCand',s.max_candidates); set('aitSetInterval',s.interval_sec); set('aitSetMonitor',s.monitor_sec);
  set('aitSetMinConf',r.min_confidence); set('aitSetMaxPos',r.max_positions); set('aitSetPerTrade',r.per_trade_ratio);
  set('aitSetSingleMax',r.single_max_ratio); set('aitSetDayLimit',r.daily_trade_limit); set('aitSetMaxLoss',r.daily_max_loss_pct);
  set('aitSetPreUni',s.prescreen_universe); set('aitSetPreTop',s.prescreen_top); set('aitSetConsensus',r.consensus_min_votes);
  set('aitSetStratMax',s.strategy_candidate_max_codes); set('aitSetStratTop',s.strategy_candidate_top);
  const ig=document.getElementById('aitSetIgnoreTime'); if(ig) ig.checked=!!s.ignore_market_time;
  const ps=document.getElementById('aitSetPrescreen'); if(ps) ps.checked=s.prescreen_enabled!==false;
  const sc=document.getElementById('aitSetStrategyCandidates'); if(sc) sc.checked=s.strategy_candidates_enabled!==false;
  aitRenderStrategyOptions();
  aitRenderStrategyBadge();
  aitRenderWatch();
}
function aitRenderWatch(){
  const box=document.getElementById('aitWlTags'); if(!box||!aitS.settings) return;
  box.innerHTML=(aitS.settings.watchlist||[]).map(c=>'<div class="ait-wl-tag">'+esc(c)+'<span '+_aiActionAttrs('remove-watch',c)+'>×</span></div>').join('')||'<span style="font-size:11px;color:var(--t4)">暂无自选，AI将只评估持仓</span>';
}
async function aitAddWatch(){
  const inp=document.getElementById('aitWlInput'), code=inp.value.trim(); if(!code) return;
  const r=await api('/ai_trader/watchlist','POST',{op:'add',code:code});
  if(r&&r.ok){ aitS.settings.watchlist=r.watchlist; inp.value=''; aitRenderWatch(); }
}
async function aitRemoveWatch(code){
  const r=await api('/ai_trader/watchlist','POST',{op:'remove',code:code});
  if(r&&r.ok){ aitS.settings.watchlist=r.watchlist; aitRenderWatch(); }
}
function aitOpenSettings(){ aitFillSettings(); document.getElementById('aitSettingsModal').classList.add('show'); }
async function aitSaveSettings(){
  const num=id=>parseFloat(document.getElementById(id).value);
  const picked=Array.from(document.querySelectorAll('input[name="aitStrategyPick"]:checked')).map(x=>x.value);
  const body={ max_candidates:num('aitSetMaxCand'), interval_sec:num('aitSetInterval'), monitor_sec:num('aitSetMonitor'),
    ignore_market_time:document.getElementById('aitSetIgnoreTime').checked,
    strategy_candidates_enabled:document.getElementById('aitSetStrategyCandidates').checked,
    strategy_candidate_strategies:picked,
    strategy_candidate_max_codes:num('aitSetStratMax'),
    strategy_candidate_top:num('aitSetStratTop'),
    prescreen_enabled:document.getElementById('aitSetPrescreen').checked,
    prescreen_universe:num('aitSetPreUni'), prescreen_top:num('aitSetPreTop'),
    risk:{ min_confidence:num('aitSetMinConf'), max_positions:num('aitSetMaxPos'), per_trade_ratio:num('aitSetPerTrade'),
      single_max_ratio:num('aitSetSingleMax'), daily_trade_limit:num('aitSetDayLimit'), daily_max_loss_pct:num('aitSetMaxLoss'),
      consensus_min_votes:num('aitSetConsensus') } };
  const r=await api('/ai_trader/settings','POST',body);
  if(r&&r.ok){ aitS.settings=r.settings; aitRenderStrategyOptions(); aitRenderStrategyBadge(); aitRenderHealth(); fbToast('设置已保存'); document.getElementById('aitSettingsModal').classList.remove('show'); }
  else { aitS.lastError=aitApiMsg(r,'保存失败'); aitRenderHealth(); fbToast(aitS.lastError,true); }
}
