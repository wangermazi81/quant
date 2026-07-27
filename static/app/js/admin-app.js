// ================================================================
// STATE
// ================================================================
const API = '/api';
let currentUser = null;
let currentPage = 'dashboard';
let tabs = [];
let usersState = {page:1, search:''};
let txState = {page:1};
let editingUserId = null;
let pointsTargetId = null;
let resetPwdTargetId = null;
let permissionTargetId = null;
let permissionTargetName = '';
let permissionTargetMode = 'save';
let cfCallback = null;
let aiTradeDecisionRefreshTimer = null;
let aiTradeDecisionLoading = false;
// 通知流尚未接入真实事件源 → 置空，不再显示写死的假通知（避免误导）
let notices = [];
let ALL_PERMS = [
  {key:'dashboard_view', label:'工作台查看', group:'基础权限', description:'后台首页、统计概览。'},
  {key:'user_manage', label:'用户管理', group:'系统管理', description:'查看用户、启停用户、重置密码。'},
  {key:'points_manage', label:'积分管理', group:'系统管理', description:'积分流水、充值、扣减、积分规则。'},
  {key:'content_manage', label:'内容管理', group:'内容运营', description:'公告、更新日志、反馈。'},
  {key:'invite_manage', label:'邀请码管理', group:'系统管理', description:'查看、创建、作废邀请码。'},
  {key:'ai_config', label:'AI 平台配置', group:'平台配置', description:'平台级 AI 配置、模型检测、AI 测试。'},
  {key:'data_config', label:'数据源配置', group:'平台配置', description:'数据源状态、刷新、下载、配置。'},
  {key:'strategy_manage', label:'策略与因子管理', group:'策略安全', description:'仅允许查看名称、运行状态、结果，不能查看核心源码。'},
  {key:'strategy_source_view', label:'策略源码查看', group:'策略安全', description:'允许查看内置策略、内置因子的核心源码；仅给可信管理员勾选。'},
  {key:'security_audit', label:'安全审计', group:'安全权限', description:'登录日志、异常访问、反爬触发记录。'},
  {key:'admin_grant', label:'管理员授权', group:'最高权限', description:'仅 admin 拥有，不开放给普通管理员。'},
];

// ================================================================
// MENU DEFINITION
// ================================================================
// 菜单按「使用频率」重排：日常运营置顶且默认展开；低频配置下沉、默认折叠。
// 同一 group 的条目必须连续（侧栏按 group 首次出现顺序分组渲染）。
const MENU = [
  {id:'dashboard', icon:'fas fa-tachometer-alt', label:'工作台', group:'主菜单'},
  // 日常运营（高频，默认展开）
  {id:'users',     icon:'fas fa-users',          label:'用户管理', group:'日常运营'},
  {id:'feedback',  icon:'fas fa-comment-dots',   label:'问题反馈', group:'日常运营'},
  {id:'invites',   icon:'fas fa-ticket-alt',     label:'邀请码',   group:'日常运营'},
  {id:'reset-requests', icon:'fas fa-key',        label:'找回申请', group:'日常运营'},
  // 内容管理
  {id:'announcements', icon:'fas fa-bullhorn',    label:'公告管理', group:'内容管理'},
  {id:'changelog',     icon:'fas fa-history',     label:'更新日志', group:'内容管理'},
  // 计费与积分（低频，默认折叠）
  {id:'points',    icon:'fas fa-coins',          label:'积分流水', group:'计费与积分'},
  {id:'point-costs', icon:'fas fa-sliders-h',    label:'积分设置', group:'计费与积分'},
  // 平台配置（很低频，默认折叠）
  {id:'datasource-config', icon:'fas fa-database', label:'数据源配置', group:'平台配置'},
  {id:'ai-config',  icon:'fas fa-robot',         label:'AI接口设置', group:'平台配置'},
  {id:'notify-config', icon:'fas fa-bell',       label:'通知设置',   group:'平台配置'},
  {id:'roles',     icon:'fas fa-shield-alt',     label:'角色权限',   group:'平台配置'},
  // 数据分析（默认折叠）
  {id:'stats',         icon:'fas fa-chart-bar',   label:'使用统计',  group:'数据分析'},
  {id:'factor-mining', icon:'fas fa-microscope',  label:'AI因子挖掘', group:'数据分析'},
  {id:'ai-trade-decision', icon:'fas fa-brain',   label:'AI交易决策', group:'数据分析'},
  // 账户
  {id:'user-center',   icon:'fas fa-user-circle', label:'个人中心',  group:'账户'},
];
const PAGE_PERMISSIONS = {
  dashboard: 'dashboard_view',
  users: 'user_manage',
  roles: 'admin_grant',
  points: 'points_manage',
  'point-costs': 'points_manage',
  'datasource-config': 'data_config',
  'ai-config': 'ai_config',
  invites: 'invite_manage',
  'reset-requests': 'user_manage',
  'notify-config': 'content_manage',
  stats: 'dashboard_view',
  'factor-mining': 'strategy_manage',
  'ai-trade-decision': 'admin_grant',
  announcements: 'content_manage',
  changelog: 'content_manage',
  feedback: 'content_manage',
};
const ROOT_ONLY_PAGES = new Set(['ai-trade-decision']);
const PAGE_NAMES = {
  'dashboard':'工作台','users':'用户管理','roles':'角色权限',
  'points':'积分流水','stats':'使用统计','factor-mining':'AI因子挖掘','ai-trade-decision':'AI交易决策','announcements':'公告管理',
  'invites':'邀请码','reset-requests':'找回申请','notify-config':'通知设置','feedback':'问题反馈','point-costs':'积分设置','datasource-config':'数据源配置','ai-config':'AI接口设置',
  'user-center':'个人中心','change-pwd':'修改密码','notifications':'系统通知',
  'changelog':'更新日志',
};

// ================================================================
// SIDEBAR RENDER
// ================================================================
function isRootAdmin(){
  return !!(currentUser && (currentUser.is_root_admin || currentUser.username === 'admin'));
}

function canPerm(key){
  if(!key) return true;
  if(isRootAdmin()) return true;
  return (currentUser?.admin_permissions || []).includes(key);
}

function canPage(id){
  if(ROOT_ONLY_PAGES.has(id)) return isRootAdmin();
  return canPerm(PAGE_PERMISSIONS[id]);
}

function visibleMenuItems(){
  return MENU.filter(m=>canPage(m.id));
}

function defaultPage(){
  const first = visibleMenuItems().find(m=>m.id !== 'user-center');
  return first ? first.id : 'user-center';
}

// ── 侧栏分组折叠（工单7b）：点组名折叠/展开，选择记忆在 localStorage ──
// 首次访问默认收起低频组；当前页所在组强制展开；侧栏图标模式(collapsed)下
// 折叠自动失效（CSS 控制），保证条目永远可达。
const MENU_FOLD_KEY = 'jw_admin_menu_fold';
const MENU_FOLD_DEFAULT = {'计费与积分': true, '平台配置': true, '数据分析': true, '账户': true};

function _menuFoldState(){
  try{
    const raw = localStorage.getItem(MENU_FOLD_KEY);
    return raw ? JSON.parse(raw) : {...MENU_FOLD_DEFAULT};
  }catch(e){ return {...MENU_FOLD_DEFAULT}; }
}

function toggleMenuGroup(g){
  const st = _menuFoldState();
  st[g] = !st[g];
  try{ localStorage.setItem(MENU_FOLD_KEY, JSON.stringify(st)); }catch(e){}
  renderMenu();
}

function renderMenu(){
  const menu = document.getElementById('sidebarMenu');
  const groups = {};
  visibleMenuItems().forEach(m=>{
    if(!groups[m.group]) groups[m.group]=[];
    groups[m.group].push(m);
  });
  const fold = _menuFoldState();
  let html='';
  Object.entries(groups).forEach(([g,items])=>{
    const folded = !!fold[g] && !items.some(m=>m.id===currentPage);
    html+=`<div class="menu-group-title menu-group-toggle" data-ui-click="toggleMenuGroup('${g}')" title="点击${folded?'展开':'折叠'}分组">
      <span>${g}</span><i class="fas fa-chevron-${folded?'right':'down'} mg-caret"></i>
    </div>`;
    items.forEach(m=>{
      html+=`<div class="menu-item${currentPage===m.id?' active':''}${folded?' folded':''}" data-ui-click="navigate('${m.id}')" data-id="${m.id}">
        <i class="${m.icon} mi-icon"></i>
        <span class="mi-label">${m.label}</span>
      </div>`;
    });
  });
  menu.innerHTML=html;
}

function toggleSidebar(){
  const sb = document.getElementById('sidebar');
  // 窄屏：侧栏是屏外抽屉，汉堡键开/关抽屉；宽屏：原折叠行为
  if(window.innerWidth <= 900){ sb.classList.toggle('mobile-open'); return; }
  sb.classList.toggle('collapsed');
}

// ================================================================
// TABS
// ================================================================
function renderTabs(){
  const bar = document.getElementById('tabsBar');
  if(!tabs.length){bar.innerHTML='';return;}
  bar.innerHTML = tabs.map((t,i)=>`
    <div class="tab${t.id===currentPage?' active':''}" data-ui-click="navigate('${t.id}')">
      ${PAGE_NAMES[t.id]||t.id}
      ${t.closable?`<span class="tab-close" data-ui-click="event.stopPropagation();closeTab('${t.id}')"><i class="fas fa-times"></i></span>`:''}
    </div>`).join('');
}

function addTab(id){
  if(!tabs.find(t=>t.id===id)){
    tabs.push({id, closable: id!=='dashboard'});
    renderTabs();
  }
}

function closeTab(id){
  const idx = tabs.findIndex(t=>t.id===id);
  if(idx<0) return;
  tabs.splice(idx,1);
  if(currentPage===id){
    const next = tabs[Math.min(idx, tabs.length-1)];
    if(next) navigate(next.id, false);
  }
  renderTabs();
}

// ================================================================
// NAVIGATE
// ================================================================
function navigate(id, addTabFlag=true){
  if(id !== '403' && id !== '404' && !canPage(id)){
    showToast('没有权限访问该功能', 'error');
    id = '403';
    addTabFlag = false;
  }
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  const pg = document.getElementById('page-'+id);
  if(!pg){navigate('404'); return;}
  // 窄屏抽屉：选完菜单自动收起
  if(window.innerWidth <= 900){ const sb=document.getElementById('sidebar'); if(sb) sb.classList.remove('mobile-open'); }
  pg.classList.add('active');
  currentPage = id;
  setAiTradeDecisionPolling(id === 'ai-trade-decision');
  if(addTabFlag){
    if(!tabs.find(t=>t.id==='dashboard')) tabs.unshift({id:'dashboard',closable:false});
    addTab(id);
  }
  renderMenu();
  renderTabs();

  // 页面专属初始化
  if(id === 'announcements') loadAnnouncements();
  if(id === 'changelog') loadChangelogAdmin();
  if(id === 'feedback') loadFeedback();

  // Breadcrumb
  const bread = PAGE_NAMES[id]||id;
  document.getElementById('headerBread').innerHTML = `<span>管理后台</span> / <b>${bread}</b>`;

  // Lazy load
  if(id==='dashboard') loadDashboard();
  if(id==='users') loadUsers(usersState.page);
  if(id==='roles') renderRoles();
  if(id==='points') loadTransactions(1);
  if(id==='stats') loadStats();
  if(id==='user-center') loadUserCenter();
  if(id==='invites') loadInvites();
  if(id==='reset-requests') loadResetRequests();
  if(id==='notify-config') loadNotifyConfig();
  if(id==='point-costs') loadPointCosts();
  if(id==='datasource-config') loadDatasourceConfig();
  if(id==='factor-mining') loadFactorMining();
  if(id==='ai-trade-decision') loadAiTradeDecision();
  if(id==='ai-config') loadAiConfig();
  if(id==='notifications') renderNotifications();

  // URL hash 同步：F5 保持当前页、浏览器前进/后退可用（此前刷新永远回工作台）
  if(location.hash.slice(1) !== id){
    try{ location.hash = id; }catch(e){}
  }
}

// hash → 页面（后退/前进/直达链接）
window.addEventListener('hashchange', ()=>{
  const id = location.hash.slice(1);
  if(id && id !== currentPage && document.getElementById('page-'+id)) navigate(id);
});

// ================================================================
// AUTH
// ================================================================
async function loadPermissionCatalog(){
  const r = await fetch(`${API}/admin/permissions/catalog`,{credentials:'include'});
  if(!r.ok) return;
  const d = await r.json();
  if(d.ok && Array.isArray(d.permissions) && d.permissions.length){
    ALL_PERMS = d.permissions;
  }
}

async function checkAuth(){
  try{
    const r = await fetch(`${API}/auth/me`,{credentials:'include'});
    if(!r.ok){location.href='/'; return;}
    currentUser = await r.json();
    if(!currentUser.is_admin){location.href='/'; return;}
    await loadPermissionCatalog();
    document.getElementById('hAvatar').textContent = currentUser.username[0].toUpperCase();
    document.getElementById('hUname').textContent = currentUser.username;
    document.getElementById('hRole').textContent = isRootAdmin() ? '最高账号 admin' : '管理员';
    // 按 URL hash 恢复页面（F5 不再永远回工作台）；无效 hash 走默认页
    const hashPage = location.hash.slice(1);
    navigate(hashPage && document.getElementById('page-'+hashPage) ? hashPage : defaultPage());
  }catch(e){location.href='/';}
}

async function doLogout(){
  await fetch(`${API}/auth/logout`,{method:'POST',credentials:'include'});
  location.href='/';
}

// ================================================================
// DASHBOARD
// ================================================================
async function loadDashboard(){
  const r = await fetch(`${API}/admin/stats`,{credentials:'include'});
  if(!r.ok) return;
  const d = await r.json();
  document.getElementById('st-users').textContent = d.total_users;
  document.getElementById('st-active-sub').textContent = `活跃用户 ${d.active_users}`;
  document.getElementById('st-points').textContent = d.total_points.toLocaleString();
  document.getElementById('st-calls').textContent = d.today_calls;
  document.getElementById('st-active').textContent = d.active_users;
  loadSystemStatus();
  renderLineChart();
  renderPieChart();
  renderActivity();
}

async function loadSystemStatus(){
  const box = document.getElementById('systemStatusBody');
  if(!box) return;
  try{
    const r = await fetch(`${API}/admin/system/status`,{credentials:'include'});
    const d = await safeJson(r);
    if(!r.ok || !d.ok){
      box.innerHTML = `<div class="system-status-meta">暂无权限或诊断接口不可用</div>`;
      return;
    }
    const checks = d.checks || {};
    const rows = [
      ['database','数据库', c => c.readable ? '可读' : '不可读'],
      ['data_dir','数据目录', c => c.writable ? '可写' : '只读'],
      ['review_archive','复盘归档', c => c.latest_date || '暂无归档'],
      ['lhb_cache','龙虎榜缓存', c => c.latest_date || '暂无缓存'],
      ['market_snapshot','市场快照', c => c.latest_date || '暂无快照'],
    ];
    box.innerHTML = rows.map(([key, name, meta])=>{
      const c = checks[key] || {};
      const ok = !!c.ok;
      const count = Number.isFinite(Number(c.file_count)) ? `文件 ${Number(c.file_count)}` : '';
      const updated = c.latest_updated_at ? `更新 ${esc(c.latest_updated_at)}` : '';
      return `<div class="system-status-item">
        <div class="system-status-head">
          <span class="system-status-name">${name}</span>
          <span class="system-status-pill ${ok?'ok':'bad'}">${ok?'正常':'异常'}</span>
        </div>
        <div class="system-status-meta">
          <div>${esc(String(meta(c)))}</div>
          ${count?`<div>${count}</div>`:''}
          ${updated?`<div>${updated}</div>`:''}
        </div>
      </div>`;
    }).join('');
  }catch(e){
    box.innerHTML = `<div class="system-status-meta">诊断加载失败</div>`;
  }
}

// ── 运营统计：全部来自 /api/admin/analytics 的真实数据 ─────────────
// 用轻量内联 SVG 自绘，不引入 echarts（境外 CDN 已全部下线）。
let _analyticsCache = null;
async function loadAnalytics(force){
  if(_analyticsCache && !force) return _analyticsCache;
  try{
    const r = await fetch(`${API}/admin/analytics`,{credentials:'include'});
    if(!r.ok){ return null; }
    const d = await r.json();
    if(d && d.ok){ _analyticsCache = d; return d; }
  }catch(e){}
  return null;
}

function _statMsg(elId, text){
  const el = document.getElementById(elId);
  if(!el) return;
  el.innerHTML = `<div style="height:100%;min-height:160px;display:flex;align-items:center;justify-content:center;color:#9AA3AD;font-size:12px">${text}</div>`;
}

const _CHART_COLORS = ['#EF5B78','#5B8DEF','#4CAF89','#E8964A','#B580E8','#4FB3C7','#D9A441','#8A929B'];

// 折线/面积图（多序列）
function _svgLine(elId, labels, series, opt){
  const el = document.getElementById(elId);
  if(!el) return;
  const W=720, H=200, PL=42, PR=12, PT=14, PB=26;
  const all = series.flatMap(s=>s.data);
  const max = Math.max(1, ...all);
  const iw = W-PL-PR, ih = H-PT-PB;
  const xs = i => PL + (labels.length<2 ? iw/2 : i*iw/(labels.length-1));
  const ys = v => PT + ih - (v/max)*ih;
  const grid = [0,.25,.5,.75,1].map(f=>{
    const y = PT+ih-f*ih;
    return `<line x1="${PL}" y1="${y}" x2="${W-PR}" y2="${y}" stroke="#EFE3D2" stroke-width="1"/>`
         + `<text x="${PL-6}" y="${y+4}" text-anchor="end" font-size="10" fill="#9AA3AD">${Math.round(max*f)}</text>`;
  }).join('');
  const paths = series.map((s,si)=>{
    const c=_CHART_COLORS[si%_CHART_COLORS.length];
    const pts=s.data.map((v,i)=>`${xs(i)},${ys(v)}`).join(' ');
    const area=`${PL},${PT+ih} ${pts} ${xs(s.data.length-1)},${PT+ih}`;
    return `<polygon points="${area}" fill="${c}" opacity=".10"/>`
         + `<polyline points="${pts}" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round"/>`
         + s.data.map((v,i)=>`<circle cx="${xs(i)}" cy="${ys(v)}" r="3" fill="${c}"><title>${labels[i]}：${v}</title></circle>`).join('');
  }).join('');
  const xlab = labels.map((l,i)=> (labels.length>10 && i%2) ? '' :
    `<text x="${xs(i)}" y="${H-8}" text-anchor="middle" font-size="10" fill="#9AA3AD">${esc(String(l).slice(5))}</text>`).join('');
  const legend = series.length>1 ? `<div style="display:flex;gap:14px;justify-content:center;margin-top:2px">`
    + series.map((s,si)=>`<span style="font-size:11px;color:var(--text2)"><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${_CHART_COLORS[si%_CHART_COLORS.length]};margin-right:4px"></i>${esc(s.name)}</span>`).join('')
    + `</div>` : '';
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${grid}${paths}${xlab}</svg>${legend}`;
}

// 横向条形图（替代饼图，小数据集更易读）
function _svgBars(elId, items, unit){
  const el = document.getElementById(elId);
  if(!el) return;
  if(!items.length){ _statMsg(elId,'暂无数据'); return; }
  const max = Math.max(1, ...items.map(i=>i.value));
  el.innerHTML = `<div style="display:flex;flex-direction:column;gap:9px;padding:6px 2px">`
    + items.map((it,i)=>{
        const pct = Math.round(it.value/max*100);
        const c=_CHART_COLORS[i%_CHART_COLORS.length];
        return `<div>
          <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px">
            <span style="color:var(--text1)">${esc(it.name)}</span>
            <b style="color:${c}">${it.value.toLocaleString()}${unit||''}</b>
          </div>
          <div style="height:8px;background:#F3EDE4;border-radius:999px;overflow:hidden">
            <div style="width:${pct}%;height:100%;background:${c};border-radius:999px"></div>
          </div></div>`;
      }).join('')
    + `</div>`;
}

async function renderLineChart(){
  const d = await loadAnalytics();
  if(!d){ _statMsg('chartLine','统计数据加载失败'); return; }
  const t = d.points_trend;
  _svgLine('chartLine', t.days, [
    {name:'消耗', data:t.spend},
    {name:'充值', data:t.topup},
  ]);
}

async function renderPieChart(){
  const d = await loadAnalytics();
  if(!d){ _statMsg('chartPie','统计数据加载失败'); return; }
  _svgBars('chartPie', d.feature_usage.map(f=>({name:f.name, value:f.count})), ' 次');
}

async function renderActivity(){
  const list = document.getElementById('activityList');
  if(!list) return;
  const d = await loadAnalytics();
  if(!d || !d.recent.length){
    list.innerHTML = `<li class="tl-item"><div class="tl-dot"></div><div class="tl-body"><div class="tl-title" style="color:#8A929B">暂无活动记录</div></div></li>`;
    return;
  }
  list.innerHTML = d.recent.map(i=>`
    <li class="tl-item">
      <div class="tl-dot"></div>
      <div class="tl-body"><div class="tl-title">${esc(i.text)}</div><div class="tl-time">${esc(String(i.time||'').slice(5,16))}</div></div>
    </li>`).join('');
}

// ================================================================
// USER MANAGEMENT
// ================================================================
async function loadUsers(page){
  usersState.page = page;
  usersState.search = document.getElementById('userSearch').value.trim();
  const params = new URLSearchParams({page, per_page:15, search:usersState.search});
  const r = await fetch(`${API}/admin/users?${params}`,{credentials:'include'});
  if(!r.ok){ showToast(`用户列表加载失败（HTTP ${r.status}）`,'error'); return; }
  const d = await r.json();
  renderUsersTable(d.users);
  renderPager('usersPager', d.total, d.page, d.per_page, p=>loadUsers(p));
}

function roleTag(u){
  if(u.is_root_admin) return '<span class="tag tag-red">最高账号 admin</span>';
  if(u.is_admin) return '<span class="tag tag-purple">普通管理员</span>';
  return '<span class="tag tag-blue">普通用户</span>';
}

function permissionBadges(perms){
  const list = (perms || []).filter(p=>p !== 'admin_grant');
  if(!list.length) return '<span style="color:var(--text3);font-size:12px">未分配功能权限</span>';
  return list.slice(0,3).map(p=>`<span class="tag">${esc(ALL_PERMS.find(x=>x.key===p)?.label || p)}</span>`).join('') +
    (list.length > 3 ? `<span style="color:var(--text3);font-size:12px"> +${list.length-3}</span>` : '');
}

function permissionCatalogItems(perms=ALL_PERMS){
  return perms.map(p=>`
    <div class="perm-catalog-item">
      <div class="perm-catalog-title">${esc(p.label)} <span class="perm-group">${esc(p.group || '未分组')}</span></div>
      <div class="perm-catalog-desc">${esc(p.description || '')}</div>
    </div>`).join('');
}

function adminUserActionAttrs(action, u){
  return `data-admin-user-action="${action}" data-user-id="${Number(u.id)}" data-user-name="${encodeURIComponent(String(u.username || ''))}" data-user-email="${encodeURIComponent(String(u.email || ''))}" data-user-points="${Number(u.points || 0)}" data-user-active="${u.is_active ? 1 : 0}" data-user-admin="${u.is_admin ? 1 : 0}"`;
}

function renderUserActions(u){
  const actions = [];
  if(canPerm('points_manage')){
    actions.push(`<button class="btn btn-outline btn-sm" ${adminUserActionAttrs('points',u)}><i class="fas fa-coins"></i> 积分</button>`);
  }
  if(canPerm('user_manage') && !u.is_root_admin){
    actions.push(`<button class="btn btn-outline btn-sm" ${adminUserActionAttrs('edit',u)} title="编辑用户名/邮箱"><i class="fas fa-edit"></i></button>`);
    actions.push(`<button class="btn btn-outline btn-sm" ${adminUserActionAttrs('reset-password',u)} title="重置密码"><i class="fas fa-key"></i></button>`);
    actions.push(`<button class="btn btn-outline btn-sm" ${adminUserActionAttrs('toggle-active',u)} title="${u.is_active?'禁用':'启用'}"><i class="fas fa-${u.is_active?'ban':'check'}"></i></button>`);
  }
  if(isRootAdmin() && !u.is_root_admin){
    if(u.is_admin){
      actions.push(`<button class="btn btn-outline btn-sm" ${adminUserActionAttrs('permissions',u)} title="调整权限"><i class="fas fa-shield-alt"></i> 权限</button>`);
    }
    actions.push(`<button class="btn btn-outline btn-sm" ${adminUserActionAttrs('toggle-admin',u)} title="${u.is_admin?'撤销管理员':'设为管理员'}"><i class="fas fa-${u.is_admin?'user-minus':'user-shield'}"></i></button>`);
  }
  if(canPerm('user_manage') && !u.is_root_admin){
    actions.push(`<button class="btn btn-danger btn-sm" ${adminUserActionAttrs('delete',u)} title="删除用户"><i class="fas fa-trash"></i></button>`);
  }
  return actions.length ? actions.join('') : '<span style="color:var(--text3);font-size:12px">无可用操作</span>';
}

function renderUsersTable(users){
  const tbody = document.getElementById('usersTbody');
  if(!users.length){tbody.innerHTML=`<tr><td colspan="9" style="text-align:center;padding:40px;color:var(--text3)"><i class="fas fa-inbox" style="font-size:32px;opacity:.3;display:block;margin-bottom:8px"></i>暂无数据</td></tr>`;return;}
  tbody.innerHTML = users.map(u=>`
    <tr>
      <td>${u.id}</td>
      <td><b>${esc(u.username)}</b></td>
      <td style="color:var(--text2)">${esc(u.email)}</td>
      <td><span style="font-weight:600;color:var(--orange)">${u.points.toLocaleString()}</span></td>
      <td>${roleTag(u)}<div style="margin-top:4px;display:flex;gap:4px;flex-wrap:wrap">${u.is_admin && !u.is_root_admin ? permissionBadges(u.admin_permissions) : ''}</div></td>
      <td>${u.is_active?'<span class="tag tag-green">启用</span>':'<span class="tag tag-red">禁用</span>'}</td>
      <td style="color:var(--text3);font-size:12px">${u.created_at||'—'}</td>
      <td style="color:var(--text3);font-size:12px">${u.last_login||'从未'}</td>
      <td>
        <div style="display:flex;gap:4px;flex-wrap:wrap">
          ${renderUserActions(u)}
        </div>
      </td>
    </tr>`).join('');
}

function openUserModal(uid, fill){
  editingUserId = uid||null;
  document.getElementById('modalUserTitle').textContent = uid?'编辑用户':'新建用户';
  // 编辑模式回填当前值（此前编辑分支无入口且无条件清空字段，等于摆设）
  document.getElementById('u-username').value = fill?.username || '';
  document.getElementById('u-email').value = fill?.email || '';
  document.getElementById('u-password').value='';
  document.getElementById('u-points').value='100';
  document.getElementById('u-role').value='0';
  document.getElementById('u-active').value='1';
  document.getElementById('u-err').textContent='';
  // 编辑模式只改 用户名/邮箱；密码走「重置密码」按钮、积分走「积分」按钮、
  // 角色走「设为管理员」按钮 —— 禁用这三项防止误以为这里能改。
  const lockEdit = !!uid;
  ['u-password','u-points','u-role','u-active'].forEach(id=>{
    const el = document.getElementById(id);
    if(el){ el.disabled = lockEdit; el.title = lockEdit ? '编辑模式不改此项（用列表里的专用按钮）' : ''; }
  });
  document.getElementById('u-pwd-req').style.display = uid?'none':'inline';
  document.getElementById('u-pwd-hint').textContent = uid?'密码请用列表中的「重置密码」按钮':'';
  openModal('modalUser');
}

async function submitUser(){
  const username = document.getElementById('u-username').value.trim();
  const email = document.getElementById('u-email').value.trim();
  const password = document.getElementById('u-password').value;
  const points = parseInt(document.getElementById('u-points').value)||100;
  const is_admin = parseInt(document.getElementById('u-role').value);
  const is_active = parseInt(document.getElementById('u-active').value);
  const errEl = document.getElementById('u-err');
  errEl.textContent='';

  if(!username||!email){errEl.textContent='用户名和邮箱为必填项';return;}
  if(!editingUserId && !password){errEl.textContent='新建用户必须设置密码';return;}

  const btn = document.getElementById('u-submit-btn');
  btn.disabled=true;btn.innerHTML='<span class="loading-spin"></span> 保存中';
  try{
    let r, d;
    if(editingUserId){
      // 编辑：只提交 用户名/邮箱（PUT /api/admin/users/<id>）
      r = await fetch(`${API}/admin/users/${editingUserId}`,{
        method:'PUT',credentials:'include',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({username,email})
      });
    } else {
      r = await fetch(`${API}/auth/register`,{
        method:'POST',credentials:'include',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({username,email,password:password||'temp123456'})
      });
    }
    d = await r.json();
    if(d.ok){
      closeModal('modalUser');
      showToast(editingUserId?'用户资料已更新':'用户已创建','success');
      loadUsers(usersState.page);
    }
    else errEl.textContent = d.error||'操作失败';
  }finally{btn.disabled=false;btn.innerHTML='<i class="fas fa-save"></i> 保存';}
}

async function toggleActive(uid, current, uname){
  const next = !current;
  showConfirm(`确定要${next?'启用':'禁用'}用户 <b>${esc(uname)}</b> 吗？`, next?'btn-primary':'btn-danger',
    async()=>{
      const r = await fetch(`${API}/admin/users/${uid}/active`,{
        method:'POST',credentials:'include',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({active:next})
      });
      closeModal('modalConfirm');
      if(r.ok) loadUsers(usersState.page);
    }
  );
}

async function toggleAdmin(uid, current, uname){
  const next = !current;
  if(next){
    openAdminPermissions(uid, uname, true);
    return;
  }
  showConfirm(`确定要${next?'设置':'撤销'} <b>${esc(uname)}</b> 的管理员权限吗？`, 'btn-warning',
    async()=>{
      const r = await fetch(`${API}/admin/users/${uid}/admin`,{
        method:'POST',credentials:'include',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({is_admin:next})
      });
      closeModal('modalConfirm');
      if(r.ok) loadUsers(usersState.page);
    }
  );
}

// ================================================================
// POINTS MODAL
// ================================================================
let pointsTargetCur = null;   // 当前积分，用于「设为此值」的变更预览
function openPointsModal(uid, uname, cur){
  pointsTargetId = uid;
  pointsTargetCur = (cur!=null && !isNaN(cur)) ? Number(cur) : null;
  document.getElementById('pts-uname').textContent = uname;
  document.getElementById('pts-cur').textContent = pointsTargetCur!=null ? pointsTargetCur.toLocaleString() : '—';
  document.getElementById('pts-amount').value='';
  document.getElementById('pts-desc').value='';
  document.getElementById('pts-err').textContent='';
  _ptsResetPreview();
  openModal('modalPoints');
}

function _ptsResetPreview(){
  const box = document.getElementById('pts-preview');
  if(box){ box.style.display='none'; box.innerHTML=''; box.dataset.armed=''; }
}

// 「设为此值」是覆盖式操作：误点会把用户几千积分直接抹成输入值。
// 因此第一次点击只展示「变更前 → 变更后」预览并要求再次确认，不直接提交。
function _ptsArmSetConfirm(val){
  const box = document.getElementById('pts-preview');
  if(!box) return false;
  const before = pointsTargetCur;
  const delta  = (before!=null) ? val - before : null;
  const deltaTxt = delta==null ? '' :
    (delta>0 ? `<span style="color:var(--green)">+${delta.toLocaleString()}</span>`
     : delta<0 ? `<span style="color:var(--red)">${delta.toLocaleString()}</span>`
     : '<span style="color:var(--text3)">无变化</span>');
  const danger = (delta!=null && delta < 0);
  box.innerHTML = `<div class="alert ${danger?'alert-error':'alert-warn'}" style="margin-bottom:10px">
      <div style="font-weight:700;margin-bottom:4px">${danger?'⚠️ 这会<u>扣减</u>该用户积分':'请确认积分变更'}</div>
      <div style="font-size:13px;line-height:1.8">
        变更前：<b>${before!=null?before.toLocaleString():'未知'}</b>
        &nbsp;→&nbsp; 变更后：<b style="color:var(--orange)">${val.toLocaleString()}</b>
        ${deltaTxt?`&nbsp;（${deltaTxt}）`:''}
      </div>
      <div style="margin-top:8px">
        <button class="btn btn-danger btn-sm" data-ui-click="submitPoints('set',true)">确认覆盖为 ${val.toLocaleString()}</button>
        <button class="btn btn-outline btn-sm" style="margin-left:6px" data-ui-click="_ptsResetPreview()">取消</button>
      </div>
    </div>`;
  box.style.display='block';
  box.dataset.armed='1';
  return true;
}

async function submitPoints(mode, confirmed){
  const val  = parseInt(document.getElementById('pts-amount').value);
  const desc = document.getElementById('pts-desc').value.trim();
  const err  = document.getElementById('pts-err');
  err.textContent='';
  if(mode==='set'){
    if(isNaN(val)||val<0){err.textContent='请输入有效的目标积分（≥0 的整数）';_ptsResetPreview();return;}
    if(!confirmed){ _ptsArmSetConfirm(val); return; }   // 先预览再确认
    const r = await fetch(`${API}/admin/users/${pointsTargetId}/set_points`,{
      method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({value:val,description:desc||'管理员修改积分'})
    });
    const d = await r.json();
    if(d.ok){
      closeModal('modalPoints');_ptsResetPreview();loadUsers(usersState.page);
      showToast(`已将积分设为 ${val.toLocaleString()}`,'success');
    }
    else err.textContent = d.error||'修改失败';
  } else {
    if(!val||val<=0){err.textContent='充值数量必须为大于0的整数';return;}
    const r = await fetch(`${API}/admin/users/${pointsTargetId}/points`,{
      method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({amount:val,description:desc||'管理员充值'})
    });
    const d = await r.json();
    if(d.ok){
      closeModal('modalPoints');_ptsResetPreview();loadUsers(usersState.page);
      showToast(`已充值 +${val.toLocaleString()} 积分`,'success');
    }
    else err.textContent = d.error||'充值失败';
  }
}

async function delUser(uid, uname){
  showConfirm(`⚠️ 确定要<b>永久删除</b>用户 <b>${esc(uname)}</b> 吗？<br>该用户的积分 / 持仓 / 设置 / 日志 将一并删除，<b>不可恢复</b>！`, 'btn-danger',
    async()=>{
      const r = await fetch(`${API}/admin/users/${uid}`,{method:'DELETE',credentials:'include'});
      const d = await r.json().catch(()=>({}));
      closeModal('modalConfirm');
      if(d.ok){ loadUsers(usersState.page); }
      else { alert(d.error||'删除失败'); }
    }
  );
}

// ================================================================
// RESET PASSWORD MODAL
// ================================================================
function openResetPwdModal(uid, uname){
  resetPwdTargetId = uid;
  document.getElementById('rp-uname').textContent = uname;
  document.getElementById('rp-pwd').value='';
  document.getElementById('rp-pwd2').value='';
  document.getElementById('rp-err').textContent='';
  openModal('modalResetPwd');
}

async function submitResetPwd(){
  const pwd = document.getElementById('rp-pwd').value;
  const pwd2 = document.getElementById('rp-pwd2').value;
  const err = document.getElementById('rp-err');
  err.textContent='';
  if(!pwd||pwd.length<6){err.textContent='密码至少6位';return;}
  if(pwd!==pwd2){err.textContent='两次密码不一致';return;}
  const r = await fetch(`${API}/admin/users/${resetPwdTargetId}/reset_password`,{
    method:'POST',credentials:'include',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({new_password:pwd})
  });
  const d = await r.json();
  if(d.ok){closeModal('modalResetPwd');showToast('密码已重置','success');}
  else err.textContent = d.error||'重置失败';
}

// ================================================================
// TRANSACTIONS
// ================================================================
async function loadTransactions(page){
  txState.page = page;
  const r = await fetch(`${API}/admin/transactions?page=${page}&per_page=20`,{credentials:'include'});
  if(!r.ok){ showToast(`积分流水加载失败（HTTP ${r.status}）`,'error'); return; }
  const d = await r.json();
  const tbody = document.getElementById('txTbody');
  if(!d.records.length){tbody.innerHTML=`<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--text3)">暂无记录</td></tr>`;return;}
  tbody.innerHTML = d.records.map(r=>`
    <tr>
      <td>${r.id}</td>
      <td><b>${esc(r.username||'—')}</b></td>
      <td><span class="tag ${typeTag(r.type)}">${typeLabel(r.type)}</span></td>
      <td style="font-weight:600;color:${r.amount>0?'var(--green)':'var(--red)'}">
        ${r.amount>0?'+':''}${r.amount}
      </td>
      <td>${r.balance}</td>
      <td style="color:var(--text2)">${esc(r.description||'—')}</td>
      <td style="color:var(--text3);font-size:12px">${r.created_at||'—'}</td>
    </tr>`).join('');
  renderPager('txPager', d.total, d.page, d.per_page, p=>loadTransactions(p));
}

function typeTag(t){
  const m={'register':'tag-green','admin_add':'tag-blue','ai_analysis':'tag-orange','backtest':'tag-purple','scan':'tag-orange','paper_trade':'tag-blue'};
  return m[t]||'tag-gray';
}
function typeLabel(t){
  const m={'register':'注册赠送','admin_add':'管理充值','ai_analysis':'AI分析','backtest':'策略回测','scan':'市场扫描','paper_trade':'模拟交易'};
  return m[t]||t;
}

// ================================================================
// STATS CHARTS
// ================================================================
function loadStats(){
  setTimeout(()=>{
    renderBarChart();
    renderGrowthChart();
    renderDistChart();
  },100);
}

async function renderBarChart(){
  const d = await loadAnalytics();
  if(!d){ _statMsg('chartBar','统计数据加载失败'); return; }
  _svgBars('chartBar', d.feature_usage.map(f=>({name:f.name, value:f.points})), ' 分');
}

async function renderGrowthChart(){
  const d = await loadAnalytics();
  if(!d){ _statMsg('chartGrowth','统计数据加载失败'); return; }
  const g = d.user_growth;
  _svgLine('chartGrowth', g.months, [{name:'新增用户', data:g.counts}]);
}

async function renderDistChart(){
  const d = await loadAnalytics();
  if(!d){ _statMsg('chartDist','统计数据加载失败'); return; }
  _svgBars('chartDist', d.points_dist, ' 分');
}

// ================================================================
// ROLES
// ================================================================
async function renderRoles(){
  const grid = document.getElementById('roleGrid');
  if(!isRootAdmin()){
    grid.innerHTML = '<div class="empty" style="padding:36px;color:var(--text2)">仅最高账号 admin 可以管理管理员授权。</div>';
    return;
  }
  grid.innerHTML = '<div class="empty" style="padding:36px;color:var(--text2)"><span class="loading-spin"></span> 正在加载权限目录...</div>';
  await loadPermissionCatalog();
  const r = await fetch(`${API}/admin/users/admins`,{credentials:'include'});
  const d = r.ok ? await r.json() : {users:[]};
  const admins = (d.users || []).filter(u=>u.is_admin);
  const catalogCard = `
    <div class="role-card role-card-wide">
      <div class="rc-title">权限白名单</div>
      <div class="rc-desc">后端只接受这些固定 key；前端自造权限不会生效。普通管理员只有勾选“策略源码查看”后才能读取内置策略/因子源码。</div>
      <div class="perm-catalog">${permissionCatalogItems(ALL_PERMS)}</div>
    </div>`;
  const adminCards = admins.map(u=>`
    <div class="role-card">
      <div class="rc-title">${esc(u.username)} ${u.is_root_admin ? '<span class="tag tag-red">最高账号</span>' : '<span class="tag tag-purple">普通管理员</span>'}</div>
      <div class="rc-desc">${u.is_root_admin ? '默认拥有后台最高权限，不参与普通管理员勾选。' : '可查看/操作的功能由 admin 勾选授权。'}</div>
      <div class="rc-perms">${u.is_root_admin ? '<span class="rc-perm">全部后台权限</span>' : permissionBadges(u.admin_permissions)}</div>
      <div class="rc-footer">
        ${u.is_root_admin ? '' : `<button class="btn btn-outline btn-sm" ${adminUserActionAttrs('permissions',u)}><i class="fas fa-edit"></i> 调整权限</button>`}
      </div>
    </div>`).join('');
  grid.innerHTML = catalogCard + (adminCards || '<div class="empty" style="padding:36px;color:var(--text2)">暂无普通管理员</div>');
}

async function openAdminPermissions(uid, uname, makeAdmin=false){
  permissionTargetId = uid;
  permissionTargetName = uname;
  permissionTargetMode = makeAdmin ? 'grant' : 'save';
  await loadPermissionCatalog();
  let currentPerms = [];
  if(!makeAdmin){
    const r = await fetch(`${API}/admin/users/${uid}/permissions`,{credentials:'include'});
    const d = r.ok ? await r.json() : {};
    if(d.is_root_admin){
      showToast('最高账号 admin 不参与普通管理员授权', 'error');
      return;
    }
    currentPerms = d.permissions || [];
  }
  document.getElementById('modalRoleTitle').textContent = makeAdmin ? '设为管理员并授权' : '调整管理员权限';
  document.getElementById('role-name').value = uname;
  document.getElementById('role-desc').value = makeAdmin ? '保存后该用户成为普通管理员' : '仅勾选允许查看/操作的功能';
  const permGrid = document.getElementById('permGrid');
  permGrid.innerHTML = ALL_PERMS.filter(p=>p.key !== 'admin_grant').map(p=>`
    <div class="perm-item${currentPerms.includes(p.key)?' checked':''}" data-ui-click="this.classList.toggle('checked')" data-key="${p.key}">
      <div class="perm-check"></div>
      <div>
        <div class="perm-name">${esc(p.label)} <span class="perm-group">${esc(p.group || '未分组')}</span></div>
        <div class="perm-desc">${esc(p.description || '')}</div>
      </div>
    </div>`).join('');
  openModal('modalRole');
}

async function submitRole(){
  if(!permissionTargetId) return;
  const perms = [...document.querySelectorAll('.perm-item.checked')].map(el=>el.dataset.key);
  const url = permissionTargetMode === 'grant'
    ? `${API}/admin/users/${permissionTargetId}/admin`
    : `${API}/admin/users/${permissionTargetId}/permissions`;
  const body = permissionTargetMode === 'grant'
    ? {is_admin:true, permissions:perms}
    : {permissions:perms};
  const r = await fetch(url,{
    method:'POST',credentials:'include',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)
  });
  const d = await r.json().catch(()=>({}));
  if(!r.ok || !d.ok){
    showToast(d.error || d.msg || '权限保存失败', 'error');
    return;
  }
  closeModal('modalRole');
  showToast('权限已保存','success');
  if(currentPage === 'roles') renderRoles();
  loadUsers(usersState.page);
}

// ================================================================
// USER CENTER
// ================================================================
async function loadUserCenter(){
  const r = await fetch(`${API}/auth/me`,{credentials:'include'});
  if(!r.ok) return;
  const u = await r.json();
  currentUser = u;
  const initials = u.username[0].toUpperCase();
  document.getElementById('pcAvatar').textContent = initials;
  document.getElementById('pcName').textContent = u.username;
  document.getElementById('pcRole').textContent = u.is_admin?'超级管理员':'普通用户';
  document.getElementById('pcPoints').textContent = u.points?.toLocaleString()||'—';
  document.getElementById('pcUserId').textContent = u.user_id||'—';
  document.getElementById('pc-username').value = u.username;
  document.getElementById('pc-email').value = u.email||'';
  const curEl = document.getElementById('pc-cur-points');
  if(curEl) curEl.textContent = u.points?.toLocaleString()||'—';
}

// 个人中心：管理员给自己充值/设为积分（独立调用积分接口，不影响系统运行态）
async function selfPoints(mode){
  const uid = currentUser && currentUser.user_id;
  const msg = document.getElementById('pc-pts-msg');
  msg.style.color=''; msg.textContent='';
  if(!uid){ msg.textContent='未获取到当前账号信息，请刷新'; return; }
  const val = parseInt(document.getElementById('pc-pts-val').value);
  let url, body;
  if(mode==='set'){
    if(isNaN(val)||val<0){ msg.textContent='请输入有效的目标积分（≥0 的整数）'; return; }
    url = `${API}/admin/users/${uid}/set_points`; body = {value:val, description:'管理员自助修改积分'};
  } else {
    if(!val||val<=0){ msg.textContent='充值数量必须为大于0的整数'; return; }
    url = `${API}/admin/users/${uid}/points`; body = {amount:val, description:'管理员自助充值'};
  }
  try{
    const r = await fetch(url,{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const d = await r.json();
    if(d.ok){
      msg.style.color='#22c55e';
      msg.textContent = `✓ 成功，当前积分 ${Number(d.new_balance).toLocaleString()}`;
      document.getElementById('pc-pts-val').value='';
      loadUserCenter();   // 刷新积分显示
    } else { msg.textContent = d.error||'操作失败'; }
  }catch(e){ msg.textContent='请求失败：'+e; }
}

async function doChangePassword(){
  const old = document.getElementById('cpOld').value;
  const nw = document.getElementById('cpNew').value;
  const nw2 = document.getElementById('cpNew2').value;
  const msg = document.getElementById('cp-msg');
  msg.textContent=''; msg.style.color='var(--red)';
  if(!old||!nw){msg.textContent='请填写完整';return;}
  if(nw.length<6){msg.textContent='新密码至少6位';return;}
  if(nw!==nw2){msg.textContent='两次密码不一致';return;}
  const r = await fetch(`${API}/user/change_password`,{
    method:'POST',credentials:'include',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({old_password:old,new_password:nw})
  });
  const d = await r.json();
  if(d.ok){
    msg.style.color='var(--green)'; msg.textContent='密码修改成功！';
    document.getElementById('cpOld').value='';
    document.getElementById('cpNew').value='';
    document.getElementById('cpNew2').value='';
  } else msg.textContent = d.error||'修改失败';
}

// ================================================================
// NOTIFICATIONS
// ================================================================
function renderNotifications(){
  const list = document.getElementById('noticeList');
  list.innerHTML = notices.map(n=>`
    <div style="padding:16px;border-bottom:1px solid var(--border);display:flex;gap:12px">
      <i class="fas fa-bell" style="color:var(--theme-active);margin-top:3px;flex-shrink:0"></i>
      <div>
        <div style="font-size:14px;font-weight:600">${n.title}</div>
        <div style="font-size:13px;color:var(--text2);margin-top:4px">${n.desc}</div>
        <div style="font-size:12px;color:var(--text3);margin-top:6px">${n.time}</div>
      </div>
    </div>`).join('') || '<div style="text-align:center;padding:40px;color:var(--text3)">暂无通知</div>';
}

function renderNpList(){
  const list = document.getElementById('npList');
  list.innerHTML = notices.slice(0,5).map(n=>`
    <div class="np-item">
      <div class="np-item-title">${n.title}</div>
      <div class="np-item-time">${n.time}</div>
    </div>`).join('') || '<div style="padding:20px;text-align:center;color:var(--text3)">暂无通知</div>';
  const badge = document.getElementById('noticeBadge');
  if(badge){ badge.textContent = notices.length; badge.style.display = notices.length ? '' : 'none'; }
}

function clearNotices(){
  notices=[];
  renderNpList();
  document.getElementById('noticeBadge').style.display='none';
}

// ================================================================
// PAGINATION
// ================================================================
function renderPager(id, total, page, perPage, cb){
  const el = document.getElementById(id);
  const pages = Math.ceil(total/perPage)||1;
  // 全部按钮走 addEventListener（不再用 data-ui-click）——此前 prev 内插匿名函数名为空、
  // page/next 的 data-ui-click="void 0" 被 ui-events 捕获阶段 stopPropagation 吞掉，
  // 导致三种翻页按钮全部失效、只能看第 1 页。
  let html = `<div class="page-info">共 ${total} 条</div>`;
  html += `<button class="pg-btn" ${page<=1?'disabled':''} data-p="${page-1}">&#8249;</button>`;
  for(let i=Math.max(1,page-2);i<=Math.min(pages,page+2);i++){
    html += `<button class="pg-btn${i===page?' active':''}" data-p="${i}">${i}</button>`;
  }
  html += `<button class="pg-btn" ${page>=pages?'disabled':''} data-p="${page+1}">&#8250;</button>`;
  el.innerHTML = html;
  el.querySelectorAll('.pg-btn[data-p]').forEach(btn=>{
    if(btn.disabled) return;
    btn.addEventListener('click',()=>cb(parseInt(btn.dataset.p)));
  });
}

// ================================================================
// MODALS
// ================================================================
function openModal(id){document.getElementById(id).classList.add('show')}
function closeModal(id){document.getElementById(id).classList.remove('show')}

function showConfirm(msg, btnClass, cb){
  document.getElementById('cfMsg').innerHTML = msg;
  const btn = document.getElementById('cfOkBtn');
  btn.className = `btn ${btnClass}`;
  btn.textContent = '确认';
  cfCallback = cb;
  openModal('modalConfirm');
}

document.querySelectorAll('.modal-overlay').forEach(overlay=>{
  overlay.addEventListener('click',e=>{
    if(e.target===overlay) overlay.classList.remove('show');
  });
});

// ================================================================
// THEME
// ================================================================
function toggleThemePanel(){
  document.getElementById('themePanel').classList.toggle('hidden');
}

document.querySelectorAll('.theme-dot').forEach(dot=>{
  dot.addEventListener('click',()=>{
    const color = dot.dataset.color;
    document.documentElement.style.setProperty('--theme', color);
    document.documentElement.style.setProperty('--theme-light', color);
    document.querySelectorAll('.theme-dot').forEach(d=>d.classList.remove('active'));
    dot.classList.add('active');
    localStorage.setItem('adminTheme', color);
    // Update theme-active for non-dark themes
    if(color==='#242f42'){
      document.documentElement.style.setProperty('--theme-active','#1abc9c');
    } else {
      document.documentElement.style.setProperty('--theme-active', color);
    }
  });
});

function loadTheme(){
  const saved = localStorage.getItem('adminTheme');
  if(saved){
    const dot = document.querySelector(`.theme-dot[data-color="${saved}"]`);
    if(dot) dot.click();
  } else {
    document.querySelector('.theme-dot[data-color="#242f42"]').classList.add('active');
  }
}

// ================================================================
// FULLSCREEN
// ================================================================
function toggleFullscreen(){
  if(!document.fullscreenElement){
    document.documentElement.requestFullscreen();
    document.getElementById('fsIcon').className='fas fa-compress';
  } else {
    document.exitFullscreen();
    document.getElementById('fsIcon').className='fas fa-expand';
  }
}

// ================================================================
// NOTICE PANEL
// ================================================================
function toggleNotice(){
  const panel = document.getElementById('noticePanel');
  panel.classList.toggle('show');
  renderNpList();
}
function closeNotice(){document.getElementById('noticePanel').classList.remove('show')}

// ================================================================
// USER DROPDOWN
// ================================================================
function toggleUserDropdown(){
  document.getElementById('userDropdown').classList.toggle('show');
}
function closeDropdown(){document.getElementById('userDropdown').classList.remove('show')}

// ================================================================
// TOAST
// ================================================================
function showToast(msg, type='info'){
  const toast = document.createElement('div');
  const colors = {success:'var(--green)',error:'var(--red)',info:'var(--blue)',warning:'var(--orange)'};
  Object.assign(toast.style,{
    position:'fixed',bottom:'24px',right:'24px',
    background:colors[type]||colors.info,color:'#fff',
    padding:'12px 20px',borderRadius:'6px',fontSize:'13px',fontWeight:'600',
    boxShadow:'0 4px 16px rgba(0,0,0,.2)',zIndex:'9999',
    transform:'translateY(20px)',opacity:'0',transition:'.3s',
    maxWidth:'320px'
  });
  toast.textContent=msg;
  document.body.appendChild(toast);
  requestAnimationFrame(()=>{toast.style.transform='translateY(0)';toast.style.opacity='1';});
  setTimeout(()=>{
    toast.style.opacity='0';toast.style.transform='translateY(20px)';
    setTimeout(()=>toast.remove(),300);
  },2800);
}

// ================================================================
// UTILS
// ================================================================
function esc(s){
  if(s===null||s===undefined) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function escJs(s){
  if(s===null||s===undefined) return '';
  return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\r?\n/g,' ');
}

// ================================================================
// GLOBAL CLICK CLOSE
// ================================================================
document.addEventListener('click',e=>{
  const userAction=e.target.closest('[data-admin-user-action]');
  if(userAction){
    e.preventDefault();
    const id=Number(userAction.dataset.userId);
    const name=decodeURIComponent(userAction.dataset.userName||'');
    const points=Number(userAction.dataset.userPoints||0);
    const active=Number(userAction.dataset.userActive||0);
    const isAdmin=Number(userAction.dataset.userAdmin||0);
    const email=decodeURIComponent(userAction.dataset.userEmail||'');
    const actions={
      edit:()=>openUserModal(id,{username:name,email:email}),
      points:()=>openPointsModal(id,name,points),
      'reset-password':()=>openResetPwdModal(id,name),
      'toggle-active':()=>toggleActive(id,active,name),
      permissions:()=>openAdminPermissions(id,name),
      'toggle-admin':()=>toggleAdmin(id,isAdmin,name),
      delete:()=>delUser(id,name),
    };
    const action=actions[userAction.dataset.adminUserAction];
    if(action) action();
  }
  if(!e.target.closest('#headerUser')) closeDropdown();
  if(!e.target.closest('#noticeBtn')&&!e.target.closest('#noticePanel')) closeNotice();
});

// ================================================================
// INIT
// ================================================================
window.addEventListener('DOMContentLoaded',()=>{
  loadTheme();
  renderMenu();
  checkAuth();
  renderNpList();
  // Default tab
  tabs = [{id:'dashboard',closable:false}];
  renderTabs();
});

// ================================================================
// 公告管理
// ================================================================
const ANN_TYPE_LABELS = {
  info:'📢 普通通知', success:'✅ 功能更新',
  warning:'⚠️ 注意事项', danger:'🚨 紧急公告'
};

async function _annFetch(method, path, body, timeoutMs){
  // 永不抛异常：HTTP错误/非JSON响应/超时都返回 {ok:false,error:...}，
  // 调用方不会因未捕获异常卡在"加载中"状态
  const opts = {method, credentials:'include', headers:{'Content-Type':'application/json'}};
  if(body) opts.body = JSON.stringify(body);
  const ctl = new AbortController();
  const tid = setTimeout(()=>ctl.abort(), timeoutMs || 60000);
  opts.signal = ctl.signal;
  try{
    const r = await fetch(`${API}${path}`, opts);
    clearTimeout(tid);
    let j;
    try{ j = await r.json() }
    catch(_){
      if(r.status === 401) return {ok:false, error:'登录已失效（后台重启过），请刷新页面重新登录'};
      return {ok:false, error:`HTTP ${r.status}：接口返回了非JSON内容${r.status===404?'（后端版本过旧，请重启平台）':''}`};
    }
    if(r.status === 401 && j && !j.ok && !j.error) j.error = '登录已失效，请刷新页面重新登录';
    return j;
  }catch(e){
    clearTimeout(tid);
    return {ok:false, error: e.name==='AbortError' ? '请求超时（后端无响应）' : ('网络错误：'+(e.message||e))};
  }
}

// ================================================================
// AI因子挖掘
// ================================================================
let FM_LAST_FACTOR_REPORT = null;
let FM_LAST_FACTOR_RUN_ID = '';
let FM_ACTIVE_TASK_ID = '';
let FM_POLL_GENERATION = 0;

function fmSetText(id, text){
  const el = document.getElementById(id);
  if(el) el.textContent = text;
}

function fmNumText(v){
  if(v===null || v===undefined || v==='') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString('zh-CN') : String(v);
}

function fmRateText(v){
  if(v===null || v===undefined || v==='') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? `${(n*100).toFixed(1)}%` : '—';
}

function fmPctText(v){
  if(v===null || v===undefined || v==='') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? `${n.toFixed(2)}%` : '—';
}

function fmReadInt(id, fallback){
  const el = document.getElementById(id);
  const n = Number.parseInt(el ? el.value : '', 10);
  return Number.isFinite(n) ? n : fallback;
}

function fmStatusText(status){
  if(status === 'ok') return '可用';
  if(status === 'degraded') return '降级';
  if(!status) return '未运行';
  return status;
}

function fmListText(items){
  if(!Array.isArray(items) || !items.length) return '—';
  return items.map(x=>esc(x)).join('、');
}

function fmTierText(tier){
  if(tier === 'strong') return '强候选';
  if(tier === 'watch') return '观察';
  if(tier === 'observe') return '仅参考';
  return tier || '—';
}

function fmLiveReliabilityHtml(item){
  const score = item?.live_readiness_score;
  const label = item?.live_readiness_label || (item?.live_readiness_status === 'avoid' ? '仅观察' : item?.live_readiness_status === 'watch' ? '需竞价确认' : '实盘可跟踪');
  const warnings = Array.isArray(item?.live_warnings) ? item.live_warnings : [];
  const color = item?.live_readiness_status === 'avoid' ? 'var(--red)' : item?.live_readiness_status === 'watch' ? 'var(--orange)' : 'var(--green)';
  const warningText = warnings.length ? warnings.slice(0,2).join('；') : '次日竞价最终确认';
  const feedback = item?.factor_explain?.live_reliability?.feedback;
  const fbText = feedback && Number(feedback.sample_count || 0) > 0
    ? `反馈 阻断${fmRateText(feedback.blocked_rate)} / 执行${fmRateText(feedback.execution_rate)} / 模拟${fmPctText(feedback.paper_avg_pnl_pct)}`
    : '';
  return `<span style="color:${color};font-size:11px">${esc(label)} ${fmRateText(score)}</span><br><span style="font-size:11px;color:var(--text3)">${esc(warningText)}</span>${fbText ? `<br><span style="font-size:11px;color:var(--text3)">${esc(fbText)}</span>` : ''}`;
}

function fmMetricText(key, value){
  if(value===null || value===undefined || value==='') return '—';
  const n = Number(value);
  if(!Number.isFinite(n)) return esc(String(value));
  if(['ret_3','ret_5','ret_10','ret_20','volatility_10','near_high_20'].includes(key)) return fmRateText(n);
  if(['volume_ratio_20','amount_ratio_20'].includes(key)) return `${n.toFixed(2)}x`;
  if(key === 'limit_count_20') return String(Math.round(n));
  return n.toFixed(2);
}

function fmFactorExplainHtml(item, compact=false){
  const ex = item && typeof item.factor_explain === 'object' ? item.factor_explain : null;
  const matched = Array.isArray(ex?.matched_factors) ? ex.matched_factors : [];
  const riskFactors = Array.isArray(ex?.risk_factors) ? ex.risk_factors : [];
  const snapshot = ex?.feature_snapshot && typeof ex.feature_snapshot === 'object' ? ex.feature_snapshot : {};
  const update = ex?.update_policy && typeof ex.update_policy === 'object' ? ex.update_policy : {};
  const fallbackPositive = Array.isArray(item?.positive_factors) ? item.positive_factors.map(x=>({label:x, text:x})) : [];
  const fallbackRisk = Array.isArray(item?.risk_flags) ? item.risk_flags.map(x=>({label:x, text:x})) : [];
  const positives = matched.length ? matched : fallbackPositive;
  const risks = riskFactors.length ? riskFactors : fallbackRisk;
  const live = ex?.live_reliability && typeof ex.live_reliability === 'object' ? ex.live_reliability : {
    score: item?.live_readiness_score,
    label: item?.live_readiness_label,
    warnings: item?.live_warnings
  };
  const fb = live?.feedback && Number(live.feedback.sample_count || 0) > 0 ? live.feedback : null;
  const fbLine = fb ? `实盘反馈：阻断 ${fmRateText(fb.blocked_rate)} / 执行 ${fmRateText(fb.execution_rate)} / 模拟均盈亏 ${fmPctText(fb.paper_avg_pnl_pct)}` : '';
  const featureLine = [
    ['ret_5','5日'], ['volume_ratio_20','量比'], ['amount_ratio_20','额比'], ['near_high_20','近高'], ['limit_count_20','涨停'], ['market_emotion_score','情绪']
  ].filter(([k])=>snapshot[k]!==undefined && snapshot[k]!==null).map(([k,label])=>`${label} ${fmMetricText(k, snapshot[k])}`).join(' / ');
  const positiveHtml = positives.length
    ? positives.slice(0, compact ? 3 : 5).map(x=>`<span style="display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:6px;background:rgba(22,163,74,.12);color:var(--green);font-size:11px">${esc(x.label || x.key || x)}</span>`).join('')
    : '<span style="color:var(--text3)">暂无明显正向标签</span>';
  const riskHtml = risks.length
    ? risks.slice(0, compact ? 2 : 4).map(x=>`<span style="display:inline-block;margin:2px 4px 2px 0;padding:2px 6px;border-radius:6px;background:rgba(220,38,38,.10);color:var(--red);font-size:11px">${esc(x.label || x.key || x)}</span>`).join('')
    : '<span style="color:var(--green)">无显性风险旗标</span>';
  const summary = ex?.summary || `${esc(item?.strategy_key || item?.strategy_name || 'relay_factor_mining_v1')}：${fmListText(item?.positive_factors)}`;
  const policy = update.signal_refresh || '每个交易日收盘后刷新候选池';
  const review = update.factor_review || '每周复盘稳定性，每20个交易日或连续失效时评估权重';
  return `
    <div style="min-width:${compact?'220px':'280px'};max-width:420px;line-height:1.55;font-size:12px;color:var(--text2)">
      <div style="font-weight:700;color:var(--text1);margin-bottom:3px">${esc(ex?.factor_title || '二三板接力情绪强势延续')}</div>
      <div style="color:var(--text3);margin-bottom:4px">${esc(summary)}</div>
      <div><b>命中：</b>${positiveHtml}</div>
      ${featureLine ? `<div style="color:var(--text3);margin-top:3px"><b>数值：</b>${esc(featureLine)}</div>` : ''}
      <div style="margin-top:3px"><b>风险：</b>${riskHtml}</div>
      <div style="margin-top:3px"><b>实盘：</b><span style="color:var(--text2)">${esc(live.label || '需竞价确认')} ${fmRateText(live.score)}</span>${Array.isArray(live.warnings)&&live.warnings.length ? `<div style="color:var(--text3);margin-top:2px">${esc(live.warnings.slice(0, compact ? 1 : 3).join('；'))}</div>` : ''}</div>
      ${fbLine ? `<div style="color:var(--text3);margin-top:2px">${esc(fbLine)}</div>` : ''}
      ${compact ? '' : `<details style="margin-top:4px"><summary style="cursor:pointer;color:var(--text3)">更新节奏/交易约束</summary><div style="margin-top:4px;color:var(--text3)">${esc(policy)}；${esc(review)}</div></details>`}
    </div>`;
}

const FM_DEFAULT_RULES = {
  weights: {ret_5:0.18, ret_10:0.12, ret_20:0.12, volume_ratio_20:0.16, amount_ratio_20:0.10, near_high_20:0.16, limit_count_20:0.10, volatility_10:-0.08, overheat_ret_3:-0.08},
  risk_thresholds: {overextended_3d:0.24, high_volatility:0.08, weak_volume:0.75, deep_drawdown:-0.18},
  tier_thresholds: {strong_percentile:0.97, watch_percentile:0.90, strong_requires_no_risk:true},
  filters: {min_relay_score:null, min_volume_ratio_20:null, min_near_high_20:null, exclude_high_risk:false},
  market_gate: {enabled:true, emo_min:20, emo_max:70, require_cooling:true}
};

const FM_RULE_LABELS = {
  ret_5:'5日动量', ret_10:'10日动量', ret_20:'20日动量', volume_ratio_20:'20日量比', amount_ratio_20:'20日额比', near_high_20:'接近20日高点', limit_count_20:'20日涨停次数', volatility_10:'10日波动惩罚', overheat_ret_3:'3日过热惩罚',
  overextended_3d:'3日涨幅过热', high_volatility:'10日高波动', weak_volume:'弱量阈值', deep_drawdown:'20日深回撤',
  strong_percentile:'强候选分位', watch_percentile:'观察分位', min_relay_score:'最低评分', min_volume_ratio_20:'最低量比', min_near_high_20:'最低接近高点',
  emo_min:'情绪下限(含)', emo_max:'情绪上限(不含)'
};

function fmCloneRules(rules){ return JSON.parse(JSON.stringify(rules || FM_DEFAULT_RULES)); }
function fmRuleInputId(group, key){ return `fm-rule-${group}-${key}`; }
function fmRuleNumber(group, key, value, step='0.01'){
  return `<label style="font-size:12px;color:var(--text2)">${esc(FM_RULE_LABELS[key] || key)}<input id="${fmRuleInputId(group,key)}" class="form-input" type="number" step="${step}" value="${value ?? ''}" placeholder="默认"></label>`;
}

function fmRenderRuleEditor(rules){
  const next = fmCloneRules(rules);
  const weights = document.getElementById('fm-rule-weights');
  const risks = document.getElementById('fm-rule-risks');
  const tiers = document.getElementById('fm-rule-tiers');
  const filters = document.getElementById('fm-rule-filters');
  if(weights) weights.innerHTML = Object.entries(next.weights || {}).map(([k,v])=>fmRuleNumber('weights', k, v)).join('');
  if(risks) risks.innerHTML = Object.entries(next.risk_thresholds || {}).map(([k,v])=>fmRuleNumber('risk_thresholds', k, v)).join('');
  if(tiers) tiers.innerHTML = ['strong_percentile','watch_percentile'].map(k=>fmRuleNumber('tier_thresholds', k, (next.tier_thresholds || {})[k])).join('');
  if(filters) filters.innerHTML = ['min_relay_score','min_volume_ratio_20','min_near_high_20'].map(k=>fmRuleNumber('filters', k, (next.filters || {})[k])).join('');
  const strongNoRisk = document.getElementById('fm-rule-strong-no-risk');
  const excludeRisk = document.getElementById('fm-filter-exclude-risk');
  if(strongNoRisk) strongNoRisk.checked = (next.tier_thresholds || {}).strong_requires_no_risk !== false;
  if(excludeRisk) excludeRisk.checked = !!((next.filters || {}).exclude_high_risk);
  // 市场状态门
  const gate = next.market_gate || FM_DEFAULT_RULES.market_gate;
  const gateBox = document.getElementById('fm-rule-gate');
  if(gateBox) gateBox.innerHTML = ['emo_min','emo_max'].map(k=>fmRuleNumber('market_gate', k, gate[k], '1')).join('');
  const gateEnabled = document.getElementById('fm-gate-enabled');
  const gateCooling = document.getElementById('fm-gate-cooling');
  if(gateEnabled) gateEnabled.checked = gate.enabled !== false;
  if(gateCooling) gateCooling.checked = gate.require_cooling !== false;
}

function fmReadRuleFloat(group, key){
  const el = document.getElementById(fmRuleInputId(group, key));
  if(!el || String(el.value).trim()==='') return null;
  const v = Number(el.value);
  return Number.isFinite(v) ? v : null;
}

function fmReadMiningRules(){
  const rules = fmCloneRules(FM_DEFAULT_RULES);
  Object.keys(rules.weights).forEach(k=>{ const v=fmReadRuleFloat('weights', k); if(v!==null) rules.weights[k]=v; });
  Object.keys(rules.risk_thresholds).forEach(k=>{ const v=fmReadRuleFloat('risk_thresholds', k); if(v!==null) rules.risk_thresholds[k]=v; });
  ['strong_percentile','watch_percentile'].forEach(k=>{ const v=fmReadRuleFloat('tier_thresholds', k); if(v!==null) rules.tier_thresholds[k]=v; });
  ['min_relay_score','min_volume_ratio_20','min_near_high_20'].forEach(k=>{ rules.filters[k]=fmReadRuleFloat('filters', k); });
  rules.tier_thresholds.strong_requires_no_risk = !!document.getElementById('fm-rule-strong-no-risk')?.checked;
  rules.filters.exclude_high_risk = !!document.getElementById('fm-filter-exclude-risk')?.checked;
  ['emo_min','emo_max'].forEach(k=>{ const v=fmReadRuleFloat('market_gate', k); if(v!==null) rules.market_gate[k]=v; });
  rules.market_gate.enabled = !!document.getElementById('fm-gate-enabled')?.checked;
  rules.market_gate.require_cooling = !!document.getElementById('fm-gate-cooling')?.checked;
  return rules;
}

function fmResetRules(){
  fmRenderRuleEditor(FM_DEFAULT_RULES);
  fmRenderRuleExplain({mining_rules:FM_DEFAULT_RULES, condition_explain:[]});
}

function fmRenderRuleExplain(report){
  const el = document.getElementById('fm-rule-explain');
  if(!el) return;
  const rules = (report && report.mining_rules) || FM_DEFAULT_RULES;
  const filters = (rules.filters || {});
  const activeFilters = Object.entries(filters).filter(([,v])=>v!==null && v!==false && v!=='').map(([k,v])=>`${FM_RULE_LABELS[k] || k}: ${v}`);
  const explain = (report && Array.isArray(report.condition_explain) && report.condition_explain.length) ? report.condition_explain : [];
  el.innerHTML = `
    <div><b>当前生效规则：</b>权重 ${Object.entries(rules.weights || {}).map(([k,v])=>`${FM_RULE_LABELS[k] || k}=${v}`).join('，')}</div>
    <div><b>风险阈值：</b>${Object.entries(rules.risk_thresholds || {}).map(([k,v])=>`${FM_RULE_LABELS[k] || k}=${v}`).join('，')}</div>
    <div><b>分层：</b>强候选≥${(rules.tier_thresholds || {}).strong_percentile}，观察≥${(rules.tier_thresholds || {}).watch_percentile}，强候选无风险=${(rules.tier_thresholds || {}).strong_requires_no_risk !== false ? '是' : '否'}</div>
    <div><b>过滤：</b>${activeFilters.length ? activeFilters.join('，') : '未启用额外过滤'}</div>
    <div><b>状态门：</b>${(rules.market_gate || {}).enabled !== false
      ? `情绪带 [${(rules.market_gate || {}).emo_min ?? 20}, ${(rules.market_gate || {}).emo_max ?? 70})${(rules.market_gate || {}).require_cooling !== false ? '，且要求降温/持平进场' : ''}（回测输出 门前/门后 双轨对照）`
      : '未启用（每个测试日都进场）'}</div>
    ${explain.length ? `<div style="margin-top:4px"><b>引擎说明：</b>${explain.map(esc).join('；')}</div>` : ''}`;
}

// 今日市场状态门横幅 + 门轨指标卡
function fmRenderGate(report){
  const banner = document.getElementById('fm-gate-banner');
  const summary = (report && report.summary) || {};
  const gate = summary.gate_today || null;
  if(banner){
    if(!gate || !gate.enabled){
      banner.style.display = 'none';
    } else {
      const pass = !!gate.pass;
      banner.style.display = 'block';
      banner.style.background = pass ? 'rgba(103,194,58,.09)' : 'rgba(230,162,60,.10)';
      banner.style.borderColor = pass ? 'rgba(103,194,58,.45)' : 'rgba(230,162,60,.5)';
      const emo = (gate.emotion===null||gate.emotion===undefined) ? '—' : Number(gate.emotion).toFixed(1);
      const delta = (gate.emotion_delta===null||gate.emotion_delta===undefined) ? '—'
        : (gate.emotion_delta>0?'+':'') + Number(gate.emotion_delta).toFixed(1);
      banner.innerHTML = `<b style="color:${pass?'var(--green)':'var(--orange)'}">${pass?'🟢 今日通过市场状态门':'🟡 今日未过市场状态门 — 建议观望'}</b>
        &nbsp;市场情绪 <b>${emo}</b>（较前日 ${delta}）｜ ${esc(gate.reason_cn || gate.reason || '')}
        ${!pass ? '<br>候选已全体降级（strong→watch→observe）并标记 <b>market_gate_blocked</b>；历史归因显示不过门日子接力胜率显著偏低。' : ''}
        ${summary.gate_note ? `<div style="margin-top:4px;font-size:11px;color:var(--text3)">${esc(summary.gate_note)}</div>` : ''}`;
    }
  }
  const gatedVal = document.getElementById('fm-gated-return');
  const gatedSub = document.getElementById('fm-gated-sub');
  if(gatedVal){
    if(summary.market_gate_applied){
      gatedVal.textContent = fmPctText(summary.gated_live_total_return_pct ?? summary.gated_total_return_pct);
      if(gatedSub) gatedSub.textContent =
        `门后 ${fmNumText(summary.gated_trade_count)} 天(跳过 ${fmNumText(summary.gated_skipped_days)}) 胜率 ${fmRateText(summary.gated_live_hit_rate ?? summary.gated_hit_rate)} ｜ 门前 ${fmPctText(summary.live_total_return_pct ?? summary.total_return_pct)}`;
    } else {
      gatedVal.textContent = '—';
      if(gatedSub) gatedSub.textContent = summary.market_gate_note || '门未启用';
    }
  }
}

async function loadFactorMining(){
  const msg = document.getElementById('fm-msg');
  if(msg){ msg.style.color = 'var(--text2)'; msg.textContent = '加载最新结果…'; }
  const rules = await _annFetch('GET', '/admin/factor-mining/rules');
  if(rules && rules.ok) fmRenderRuleEditor(rules.mining_rules || FM_DEFAULT_RULES);
  else fmRenderRuleEditor(FM_DEFAULT_RULES);
  const r = await _annFetch('GET', '/admin/factor-mining/latest');
  renderFactorMining(r);
  fmLoadWatchlist();
  const task = await _annFetch('GET', '/admin/factor-mining/status');
  if(task && task.ok && task.found && (task.status === 'queued' || task.status === 'running')){
    fmPollFactorMining(task.task_id);
  }
}

function renderFactorMining(r){
  const msg = document.getElementById('fm-msg');
  const candidateBody = document.getElementById('fm-candidates');
  const windowBody = document.getElementById('fm-windows');
  if(!r || !r.ok){
    FM_LAST_FACTOR_REPORT = null;
    FM_LAST_FACTOR_RUN_ID = '';
    const error = (r && r.error) || '加载失败';
    if(msg){ msg.style.color = 'var(--red)'; msg.textContent = error; }
    if(candidateBody) candidateBody.innerHTML = `<tr><td colspan="10" style="text-align:center;padding:36px;color:var(--red)">${esc(error)}</td></tr>`;
    if(windowBody) windowBody.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:36px;color:var(--red)">${esc(error)}</td></tr>`;
    fmRenderRuleExplain({mining_rules:FM_DEFAULT_RULES, condition_explain:[]});
    return;
  }

  if(!r.found && !r.report){
    FM_LAST_FACTOR_REPORT = null;
    FM_LAST_FACTOR_RUN_ID = '';
    fmSetText('fm-status','未运行');
    fmSetText('fm-updated','没有历史报告');
    fmSetText('fm-trusted','—');
    fmSetText('fm-market','数据状态 —');
    fmSetText('fm-hit-rate','—');
    fmSetText('fm-trades','交易样本 —');
    fmSetText('fm-total-return','—');
    fmSetText('fm-drawdown','最大回撤 —');
    if(msg){ msg.style.color = 'var(--text2)'; msg.textContent = '暂无挖掘结果'; }
    if(candidateBody) candidateBody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:36px;color:var(--text3)">暂无候选</td></tr>';
    if(windowBody) windowBody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:36px;color:var(--text3)">暂无窗口结果</td></tr>';
    fmRenderRuleExplain({mining_rules:FM_DEFAULT_RULES, condition_explain:[]});
    fmRenderGate(null);
    return;
  }

  const report = r.report || {};
  FM_LAST_FACTOR_REPORT = report;
  FM_LAST_FACTOR_RUN_ID = r.run_id || '';
  if(report.mining_rules) fmRenderRuleEditor(report.mining_rules);
  fmRenderRuleExplain(report);
  const summary = report.summary || {};
  const config = report.config || {};
  const candidates = Array.isArray(report.latest_candidates) ? report.latest_candidates : [];
  const windows = Array.isArray(report.windows) ? report.windows : [];
  const updated = r.updated_at || report.generated_at || '';
  const marketStatus = summary.latest_market_data_status || 'not_checked';
  const marketReason = summary.latest_market_data_reason || '';
  const emotion = summary.latest_market_emotion_score;

  fmSetText('fm-status', fmStatusText(report.status));
  fmSetText('fm-updated', `${updated || '生成时间未知'}${r.run_id ? ' / '+r.run_id : ''}`);
  fmSetText('fm-trusted', `${fmNumText(summary.trusted_feature_rows)} / ${fmNumText(summary.feature_rows)}`);
  fmSetText('fm-market', `${marketStatus}${emotion!==null && emotion!==undefined ? ' / 情绪 '+emotion : ''}`);
  const liveHitRate = summary.live_hit_rate ?? summary.hit_rate;
  const liveTotalReturn = summary.live_total_return_pct ?? summary.total_return_pct;
  const liveDrawdown = summary.live_max_drawdown_pct ?? summary.max_drawdown_pct;
  const blockRate = summary.live_execution_block_rate;
  fmSetText('fm-hit-rate', fmRateText(liveHitRate));
  fmSetText('fm-trades', `实盘样本 ${fmNumText(summary.live_trade_count ?? summary.trade_count)} / 原始 ${fmNumText(summary.trade_count)} / 候选 ${fmNumText(summary.latest_candidate_count || candidates.length)}`);
  fmSetText('fm-total-return', fmPctText(liveTotalReturn));
  fmSetText('fm-drawdown', `实盘回撤 ${fmPctText(liveDrawdown)} / 原始 ${fmPctText(summary.raw_max_drawdown_pct ?? summary.max_drawdown_pct)}`);
  fmRenderGate(report);
  if(msg){
    msg.style.color = report.status === 'ok' ? 'var(--green)' : 'var(--orange)';
    const liveMsg = blockRate!==null && blockRate!==undefined ? `；执行过滤 ${fmRateText(blockRate)}` : '';
    const feedbackMsg = summary.live_feedback_code_count ? `；闭环反馈 ${summary.live_feedback_code_count} 只股票` : '';
    msg.textContent = `${report.reason || marketReason || `参数：${config.n_stocks || '—'}股 / ${config.days || '—'}天`}${liveMsg}${feedbackMsg}`;
  }

  if(candidateBody){
    if(candidates.length){
      candidateBody.innerHTML = candidates.map(c=>`
        <tr>
          <td>${esc(c.trade_date || '')}</td>
          <td>${fmNumText(c.candidate_rank)}</td>
          <td><b>${esc(fmStockLabel(c))}</b></td>
          <td>${esc(fmTierText(c.candidate_tier))}<br><span style="font-size:11px;color:var(--text3)">分位 ${fmRateText(c.score_percentile)}</span></td>
          <td>${fmNumText(c.relay_score)}</td>
          <td>${fmRateText(c.live_confidence ?? c.confidence)}<br>${fmLiveReliabilityHtml(c)}</td>
          <td>${esc(c.market_regime || '')}<br><span style="font-size:11px;color:var(--text3)">${esc(c.market_data_status || '')}</span></td>
          <td>${esc(fmWatchPlanText(c))}</td>
          <td>${fmFactorExplainHtml(c)}</td>
          <td>${fmListText(c.risk_flags || c.negative_factors)}</td>
        </tr>`).join('');
    }else{
      const reason = report.reason || marketReason || '当前没有满足数据可信和候选条件的结果';
      candidateBody.innerHTML = `<tr><td colspan="10" style="text-align:center;padding:36px;color:var(--text3)">${esc(reason)}</td></tr>`;
    }
  }

  if(windowBody){
    const recentWindows = windows.slice(-8).reverse();
    if(recentWindows.length){
      windowBody.innerHTML = recentWindows.map(w=>`
        <tr>
          <td>${esc(w.train_start || '')} 至 ${esc(w.train_end || '')}</td>
          <td>${esc(w.test_start || '')} 至 ${esc(w.test_end || '')}</td>
          <td>${Number(w.direction) >= 0 ? '正向' : '反向'}</td>
          <td>${fmNumText(w.trade_count)}</td>
          <td>${fmRateText(w.live_hit_rate ?? w.hit_rate)}<br><span style="font-size:11px;color:var(--text3)">原始 ${fmRateText(w.hit_rate)}</span></td>
          <td>${fmPctText(w.live_total_return_pct ?? w.total_return_pct)}<br><span style="font-size:11px;color:var(--text3)">原始 ${fmPctText(w.total_return_pct)}</span></td>
          <td>${fmPctText(w.live_max_drawdown_pct ?? w.max_drawdown_pct)}<br><span style="font-size:11px;color:var(--text3)">过滤 ${fmRateText(w.live_execution_block_rate)}</span></td>
        </tr>`).join('');
    }else{
      windowBody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:36px;color:var(--text3)">暂无可用窗口，通常是可信样本不足</td></tr>';
    }
  }
}

function fmStockLabel(item){
  const rawCode = String((item && (item.code || item.ts_code)) || '').trim();
  const code = rawCode.split('.')[0];
  const name = String((item && item.name) || '').trim();
  return name && name !== rawCode && name !== code ? `${name}（${code}）` : code;
}

function fmWatchStatusText(status){
  const map = {pending:'待观察', ready:'可买入', bought:'已买入', skipped:'已跳过', watch:'继续观察'};
  return map[status] || status || '待观察';
}

function fmWatchPlanText(item){
  const hold = item.hold_days ? `${item.hold_days}日` : '按策略';
  return `仓位 ${fmRateText(item.position_pct)} / 止损 ${fmRateText(item.stop_loss)} / 止盈 ${fmRateText(item.take_profit)} / 持有 ${hold}`;
}

// 当前查看的交易日（空=后端给的最新一天）
let FM_WATCH_DATE = '';
// 只看可执行（live_readiness_status=ready）。市场弱势时 100 只候选可能只有几只能买，
// 平铺看不出「明天到底买哪几只」。
let FM_WATCH_READY_ONLY = false;

function fmSwitchWatchDate(el){
  FM_WATCH_DATE = (el && el.value) || '';
  return fmLoadWatchlist();
}

function fmToggleReadyOnly(){
  FM_WATCH_READY_ONLY = !FM_WATCH_READY_ONLY;
  return fmLoadWatchlist();
}

async function fmLoadWatchlist(){
  const body = document.getElementById('fm-watchlist');
  const updated = document.getElementById('fm-watch-updated');
  if(body) body.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:36px;color:var(--text3)">加载中…</td></tr>';
  // 默认只看最新交易日：多跑几天会有几百条新旧混排，真正要盯的永远是最近一天
  const params = [];
  if(FM_WATCH_DATE) params.push(`date=${encodeURIComponent(FM_WATCH_DATE)}`);
  if(FM_WATCH_READY_ONLY) params.push('readiness=ready');
  const qs = params.length ? `?${params.join('&')}` : '';
  const r = await _annFetch('GET', `/admin/factor-watchlist${qs}`);
  if(!r || !r.ok){
    const error = (r && r.error) || '加载失败';
    if(body) body.innerHTML = `<tr><td colspan="10" style="text-align:center;padding:36px;color:var(--red)">${esc(error)}</td></tr>`;
    if(updated) updated.textContent = error;
    return;
  }
  const items = Array.isArray(r.items) ? r.items : [];
  const dates = Array.isArray(r.trade_dates) ? r.trade_dates : [];
  FM_WATCH_DATE = r.selected_date || '';
  if(updated){
    const picker = dates.length > 1
      ? `<select class="form-input" style="padding:4px 8px;font-size:12px;margin-left:8px"
                data-ui-change="fmSwitchWatchDate(this)">
           ${dates.map(d=>`<option value="${esc(d)}" ${d===FM_WATCH_DATE?'selected':''}>${esc(d)}（${(r.counts_by_date||{})[d]||0}只）</option>`).join('')}
         </select>`
      : '';
    const cr = r.counts_by_readiness || {};
    const readyN = cr.ready || 0;
    // 「今日可执行 N 只」是这张表最该被一眼看到的数字：市场弱势时 100 只候选
    // 可能只有几只真能买，其余都是观察级。
    const readyBadge = `<span style="margin-left:10px;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700;
        background:${readyN ? 'rgba(42,122,80,.12)' : 'rgba(148,163,184,.15)'};
        color:${readyN ? '#2A7A50' : 'var(--text3)'}">可执行 ${readyN}</span>`
      + `<span style="margin-left:6px;font-size:12px;color:var(--text3)">观察 ${cr.watch || 0} ｜ 回避 ${cr.avoid || 0}</span>`;
    const readyBtn = `<button class="btn btn-outline" style="padding:3px 10px;font-size:12px;margin-left:8px"
        data-ui-click="fmToggleReadyOnly()">${FM_WATCH_READY_ONLY ? '显示全部' : '只看可执行'}</button>`;
    updated.innerHTML = `${esc(r.selected_date || '')} 共 <b>${items.length}</b> 只`
      + (r.total_all && r.total_all !== items.length ? `　池内累计 ${r.total_all}` : '')
      + readyBadge + readyBtn
      + (r.updated_at ? `<span style="margin-left:8px;font-size:12px;color:var(--text3)">更新 ${esc(r.updated_at)}</span>` : '')
      + picker;
  }
  if(!body) return;
  if(!items.length){
    body.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:36px;color:var(--text3)">暂无标的，可从最新候选一键导入</td></tr>';
    return;
  }
  body.innerHTML = items.map(item=>`
    <tr>
      <td>${esc(item.trade_date || '')}</td>
      <td>${fmNumText(item.candidate_rank)}</td>
      <td><b>${esc(fmStockLabel(item))}</b></td>
      <td>${esc(item.strategy_name || '')}<br><span style="font-size:11px;color:var(--text3)">${esc(fmTierText(item.candidate_tier))} / ${fmNumText(item.relay_score)}</span></td>
      <td>${fmRateText(item.live_confidence ?? item.confidence)}<br><span style="font-size:11px;color:var(--text3)">分位 ${fmRateText(item.score_percentile)}</span><br>${fmLiveReliabilityHtml(item)}</td>
      <td>${esc(fmWatchPlanText(item))}</td>
      <td>${fmFactorExplainHtml(item, true)}</td>
      <td>
        <select class="form-input" style="min-width:96px;padding:6px 8px" data-ui-change="fmUpdateWatchStatus('${escJs(item.id)}',this)">
          ${['pending','ready','bought','skipped','watch'].map(s=>`<option value="${s}" ${item.status===s?'selected':''}>${fmWatchStatusText(s)}</option>`).join('')}
        </select>
      </td>
      <td><input class="form-input" style="min-width:160px;padding:6px 8px" value="${esc(item.note || '')}" placeholder="竞价/消息/风控备注" data-ui-change="fmUpdateWatchNote('${escJs(item.id)}',this)"></td>
      <td style="white-space:nowrap">
        <button class="btn btn-outline" style="padding:4px 8px;font-size:12px" data-ui-click="fmCopyCode('${escJs(item.code || item.ts_code || '')}')"><i class="fas fa-copy"></i></button>
        <button class="btn btn-outline" style="padding:4px 8px;font-size:12px" data-ui-click="fmDeleteWatchItem('${escJs(item.id)}')"><i class="fas fa-trash"></i></button>
      </td>
    </tr>`).join('');
}

async function fmImportWatchlist(){
  const candidates = FM_LAST_FACTOR_REPORT && Array.isArray(FM_LAST_FACTOR_REPORT.latest_candidates) ? FM_LAST_FACTOR_REPORT.latest_candidates : null;
  if(!candidates || !candidates.length){
    showToast('暂无最新候选可导入，请先完成因子挖掘', 'warning');
    return;
  }
  const r = await _annFetch('POST', '/admin/factor-watchlist/import', {
    run_id: FM_LAST_FACTOR_RUN_ID,
    strategy_name: 'AI因子挖掘候选',
    candidates,
  });
  if(r && r.ok){
    showToast(`已导入 ${r.imported} 只候选`, 'success');
    fmLoadWatchlist();
  }else{
    showToast((r && r.error) || '导入失败', 'error');
  }
}

// 下面两个薄包装：让 data-ui-change 只传「字面量 + this」，
// 不再需要在属性里写对象字面量 {status:this.value} —— 那种形态无法被安全解析器处理，
// 会逼着我们保留 CSP 的 unsafe-eval。
function fmUpdateWatchStatus(id, el){ return fmUpdateWatchItem(id, {status: el.value}) }
function fmUpdateWatchNote(id, el){ return fmUpdateWatchItem(id, {note: el.value}) }

async function fmUpdateWatchItem(id, patch){
  const r = await _annFetch('PATCH', `/admin/factor-watchlist/${encodeURIComponent(id)}`, patch);
  if(r && r.ok) showToast('已更新候选标注', 'success');
  else showToast((r && r.error) || '更新失败', 'error');
}

async function fmDeleteWatchItem(id){
  if(!confirm('确认从次日自选池删除这个标的吗？')) return;
  const r = await _annFetch('DELETE', `/admin/factor-watchlist/${encodeURIComponent(id)}`);
  if(r && r.ok){ showToast('已删除', 'success'); fmLoadWatchlist(); }
  else showToast((r && r.error) || '删除失败', 'error');
}

function fmCopyCode(code){
  if(!code){ showToast('代码为空', 'warning'); return; }
  if(navigator.clipboard) navigator.clipboard.writeText(code).then(()=>showToast('已复制：'+code,'success'),()=>showToast('复制失败','warning'));
  else showToast('请手动复制：'+code, 'info');
}

function fmSetRunBusy(busy){
  const btn = document.getElementById('fm-run-btn');
  if(btn) btn.disabled = !!busy;
}

function fmTaskStatusText(task){
  if(task.status === 'queued') return '任务已排队，等待开始…';
  const started = task.started_at ? '（开始于 '+task.started_at+'）' : '';
  return '后台挖掘中'+started+'，页面可保持打开或稍后返回查看…';
}

function fmSleep(ms){
  return new Promise(resolve=>setTimeout(resolve, ms));
}

async function fmPollFactorMining(taskId){
  if(!taskId) return;
  FM_ACTIVE_TASK_ID = taskId;
  const generation = ++FM_POLL_GENERATION;
  const msg = document.getElementById('fm-msg');
  fmSetRunBusy(true);

  while(FM_ACTIVE_TASK_ID === taskId && generation === FM_POLL_GENERATION){
    const task = await _annFetch('GET', '/admin/factor-mining/status?task_id='+encodeURIComponent(taskId), null, 30000);
    if(FM_ACTIVE_TASK_ID !== taskId || generation !== FM_POLL_GENERATION) return;
    if(!task || !task.ok || !task.found){
      const error = (task && task.error) || '无法读取挖掘任务状态';
      if(msg){ msg.style.color = 'var(--red)'; msg.textContent = error; }
      showToast(error, 'error');
      FM_ACTIVE_TASK_ID = '';
      fmSetRunBusy(false);
      return;
    }
    if(task.status === 'queued' || task.status === 'running'){
      if(msg){ msg.style.color = 'var(--text2)'; msg.textContent = fmTaskStatusText(task); }
      await fmSleep(2000);
      continue;
    }
    FM_ACTIVE_TASK_ID = '';
    fmSetRunBusy(false);
    if(task.status === 'done'){
      if(msg){ msg.style.color = 'var(--green)'; msg.textContent = '挖掘完成：'+(task.finished_at || ''); }
      showToast('AI因子挖掘完成', 'success');
      renderFactorMining({ok:true, found:true, run_id:task.run_id, report:task.report});
      fmLoadWatchlist();
      return;
    }
    const error = task.error || '挖掘任务失败';
    if(msg){ msg.style.color = 'var(--red)'; msg.textContent = error; }
    showToast(error, 'error');
    return;
  }
}

async function runFactorMining(){
  const msg = document.getElementById('fm-msg');
  const body = {
    n_stocks: fmReadInt('fm-n-stocks', 800),
    days: fmReadInt('fm-days', 180),
    train_days: fmReadInt('fm-train-days', 60),
    test_days: fmReadInt('fm-test-days', 10),
    hold_days: fmReadInt('fm-hold-days', 2),
    top_k: fmReadInt('fm-top-k', 10),
    candidate_pool_size: fmReadInt('fm-candidate-pool', 100),
    mining_rules: fmReadMiningRules(),
  };
  fmSetRunBusy(true);
  if(msg){ msg.style.color = 'var(--text2)'; msg.textContent = '正在创建后台挖掘任务…'; }
  const r = await _annFetch('POST', '/admin/factor-mining/run', body, 60000);
  if(r && r.ok && r.task_id){
    if(r.reused) showToast('已有挖掘任务运行中，已恢复进度', 'info');
    fmPollFactorMining(r.task_id);
  }else{
    fmSetRunBusy(false);
    const error = (r && r.error) || '挖掘任务创建失败';
    if(msg){ msg.style.color = 'var(--red)'; msg.textContent = error; }
    showToast(error,'error');
  }
}

function atdActionTag(action){
  const a = String(action || '').toUpperCase();
  const map = {
    BUY: ['买入', 'var(--green)'],
    SELL: ['卖出', 'var(--red)'],
    HOLD: ['观望', 'var(--text2)'],
    SKIP: ['跳过', 'var(--text3)'],
    ERROR: ['异常', 'var(--red)'],
  };
  const item = map[a] || [a || '—', 'var(--text3)'];
  return `<span style="font-weight:700;color:${item[1]}">${item[0]}</span>`;
}

function atdTokenText(v){
  if(!v || typeof v !== 'object') return '—';
  const total = Number(v.total_tokens ?? v.total ?? 0);
  const prompt = Number(v.prompt_tokens ?? v.input_tokens ?? 0);
  const completion = Number(v.completion_tokens ?? v.output_tokens ?? 0);
  if(Number.isFinite(total) && total > 0) return `${total.toLocaleString('zh-CN')} (${prompt || 0}/${completion || 0})`;
  return '—';
}

function atdMoneyText(v){
  const n = Number(v);
  if(!Number.isFinite(n) || n <= 0) return '—';
  if(n >= 100000000) return `${(n / 100000000).toFixed(2)}亿`;
  if(n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  return n.toLocaleString('zh-CN');
}

function atdLevelText(price, vol){
  const p = Number(price);
  const v = Number(vol);
  if(!Number.isFinite(p) || p <= 0) return '—';
  return `${p.toFixed(2)} / ${Number.isFinite(v) && v > 0 ? v.toLocaleString('zh-CN') : '0'}`;
}

function atdDepthTitle(snap){
  const lines = [];
  const bids = Array.isArray(snap?.bids) ? snap.bids : [];
  const asks = Array.isArray(snap?.asks) ? snap.asks : [];
  for(let i=0;i<5;i++){
    const b = bids[i] || {};
    const a = asks[i] || {};
    lines.push(`买${i+1}: ${atdLevelText(b.price, b.vol)}    卖${i+1}: ${atdLevelText(a.price, a.vol)}`);
  }
  return lines.join('\n');
}

function atdAuctionHtml(item){
  const snap = item?.auction_snapshot || item?.quote_snapshot || {};
  if(!snap || typeof snap !== 'object' || !Object.keys(snap).length) return '<span style="color:var(--text3)">—</span>';
  const change = Number(snap.change_pct);
  const changeText = Number.isFinite(change) ? `${change >= 0 ? '+' : ''}${change.toFixed(2)}%` : '—';
  const changeColor = Number.isFinite(change) ? (change >= 0 ? 'var(--red)' : 'var(--green)') : 'var(--text3)';
  const price = Number(snap.price);
  const open = Number(snap.open);
  const bidPrice = snap.bid1_price ?? snap.bids?.[0]?.price;
  const bidVol = snap.bid1_vol ?? snap.bids?.[0]?.vol;
  const askPrice = snap.ask1_price ?? snap.asks?.[0]?.price;
  const askVol = snap.ask1_vol ?? snap.asks?.[0]?.vol;
  const time = snap.server_time || snap.captured_at || '';
  const title = atdDepthTitle(snap);
  return `
    <div title="${esc(title)}" style="min-width:138px;font-size:12px;line-height:1.55;color:var(--text2)">
      <div><b>额</b> ${esc(atdMoneyText(snap.amount))} <span style="color:${changeColor};font-weight:700">${esc(changeText)}</span></div>
      <div>现 ${Number.isFinite(price) && price > 0 ? price.toFixed(2) : '—'} / 开 ${Number.isFinite(open) && open > 0 ? open.toFixed(2) : '—'}</div>
      <div>买一 ${esc(atdLevelText(bidPrice, bidVol))}</div>
      <div>卖一 ${esc(atdLevelText(askPrice, askVol))}</div>
      ${time ? `<div style="color:var(--text3)">${esc(time)}</div>` : ''}
    </div>`;
}

function setAiTradeDecisionPolling(active){
  if(!active){
    if(aiTradeDecisionRefreshTimer){
      clearInterval(aiTradeDecisionRefreshTimer);
      aiTradeDecisionRefreshTimer = null;
    }
    return;
  }
  if(aiTradeDecisionRefreshTimer) return;
  aiTradeDecisionRefreshTimer = setInterval(()=>{
    if(currentPage === 'ai-trade-decision') loadAiTradeDecision({silent:true});
  }, 25000);
}

function renderAiTradeDecision(data){
  const msg = document.getElementById('atd-msg');
  const cBody = document.getElementById('atd-candidates');
  const rBody = document.getElementById('atd-results');
  const summary = document.getElementById('atd-summary');
  const candidates = Array.isArray(data?.candidates) ? data.candidates : [];
  const decision = data?.decision && typeof data.decision === 'object' ? data.decision : data;
  const results = Array.isArray(decision?.results) ? decision.results : [];
  if(msg){
    msg.style.color = 'var(--text2)';
    msg.textContent = decision?.generated_at ? `最近决策 ${decision.generated_at}` : `候选 ${candidates.length} 只`;
  }
  if(cBody){
    if(!candidates.length){
      cBody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:28px;color:var(--text3)">暂无候选，请先在AI因子挖掘页导入次日候选池</td></tr>';
    }else{
      cBody.innerHTML = candidates.map(item=>`
        <tr>
          <td>${esc(item.trade_date || '')}</td>
          <td><b>${esc(item.ts_code || item.code || '')}</b></td>
          <td>${esc(item.name || '')}</td>
          <td>${fmNumText(item.candidate_rank)}</td>
          <td>${fmRateText(item.confidence)}</td>
          <td>${esc(fmWatchStatusText(item.status || 'pending'))}</td>
          <td>${esc(item.strategy_name || item.source_run_id || '')}</td>
          <td>${fmFactorExplainHtml(item, true)}</td>
        </tr>`).join('');
    }
  }
  if(summary){
    const buys = results.filter(x=>String(x.action || '').toUpperCase()==='BUY').length;
    const ready = results.filter(x=>x.risk_ok).length;
    summary.textContent = results.length ? `共 ${results.length} 个决策，买入 ${buys}，风控通过 ${ready}，Token ${atdTokenText(decision.token_usage)}` : '暂无决策结果';
  }
  if(rBody){
    if(!results.length){
      rBody.innerHTML = '<tr><td colspan="9" style="text-align:center;padding:36px;color:var(--text3)">暂无决策结果</td></tr>';
    }else{
      rBody.innerHTML = results.map(item=>`
        <tr>
          <td><b>${esc(fmStockLabel(item))}</b></td>
          <td>${atdActionTag(item.action)}</td>
          <td>${fmRateText(item.confidence)}</td>
          <td>${fmNumText(item.price || item.entry)}</td>
          <td>${atdAuctionHtml(item)}</td>
          <td>${item.risk_ok ? '<span style="color:var(--green);font-weight:700">通过</span>' : `<span style="color:var(--red)">${esc(item.blocked || '未通过')}</span>`}</td>
          <td>${item.executed ? '<span style="color:var(--green);font-weight:700">已写入</span>' : '<span style="color:var(--text3)">未执行</span>'}</td>
          <td style="min-width:220px">${esc(item.reason || item.blocked || '')}</td>
          <td>${atdTokenText(item.token_usage)}</td>
        </tr>`).join('');
    }
  }
}

function renderAiTradeAutoStatus(data){
  const box = document.getElementById('atd-auto-status');
  if(!box) return;
  const next = data?.next_runs || {};
  const history = Array.isArray(data?.history) ? data.history.slice(0, 5) : [];
  const state = data?.running ? `运行中：${esc(data.current_job || '')}` : (data?.enabled ? '已启用' : '已停用');
  const rows = history.length ? history.map(item=>{
    const ok = item.ok ? '<span style="color:var(--green);font-weight:700">成功</span>' : '<span style="color:var(--red);font-weight:700">失败</span>';
    const result = item.result || {};
    const extra = result.run_id || result.error || result.reason || (result.candidate_count!==undefined ? `候选 ${result.candidate_count}` : '');
    return `<div>${esc(item.time || '')} / ${esc(item.job || '')}${item.slot?`(${esc(item.slot)})`:''} / ${ok} ${esc(extra || '')}</div>`;
  }).join('') : '<div style="color:var(--text3)">暂无自动任务记录</div>';
  box.innerHTML = `
    <div><b>${state}</b>；下次数据刷新：${esc(next.data_refresh || '—')}；下次因子挖掘：${esc(next.factor_mining || '—')}；下次AI决策：${esc(next.ai_decision || '—')}</div>
    <div style="margin-top:8px;color:var(--text3)">默认盘后自动刷新市场/龙虎榜数据，17:05 自动挖掘并导入次日候选池，09:15/09:25 自动用 admin 的AI交易配置做竞价判断；实盘不会自动买入。</div>
    <div style="margin-top:8px">${rows}</div>`;
}

async function loadAiTradeAutoStatus(opts={}){
  if(!isRootAdmin()) return;
  const box = document.getElementById('atd-auto-status');
  if(box && !opts.silent) box.textContent = '加载中...';
  const r = await _annFetch('GET', '/admin/ai-trade-decision/auto/status');
  if(r && r.ok) renderAiTradeAutoStatus(r);
  else if(box) box.innerHTML = `<span style="color:var(--red)">${esc((r && r.error) || '自动任务状态加载失败')}</span>`;
}

async function runAiTradeAutoJob(job){
  if(!isRootAdmin()){
    navigate('403');
    return;
  }
  const labelMap = {data_refresh:'刷新数据', factor_mining:'自动挖掘', ai_decision:'AI决策', full_after_close:'盘后全流程'};
  if(!confirm(`确认立即运行「${labelMap[job] || job}」？`)) return;
  const box = document.getElementById('atd-auto-status');
  if(box) box.textContent = '任务运行中，请等待...';
  const r = await _annFetch('POST', '/admin/ai-trade-decision/auto/run', {job}, 300000);
  if(r && r.ok){
    showToast(`${labelMap[job] || job} 已完成`, 'success');
    loadAiTradeAutoStatus();
    loadAiTradeDecision();
  }else{
    const error = (r && (r.error || r.message)) || '自动任务执行失败';
    showToast(error, 'error');
    loadAiTradeAutoStatus();
  }
}

async function loadAiTradeDecision(opts={}){
  if(!isRootAdmin()){
    navigate('403');
    return;
  }
  if(aiTradeDecisionLoading) return;
  aiTradeDecisionLoading = true;
  const silent = !!opts.silent;
  const maxCandidates = fmReadInt('atd-max', 6);
  loadAiTradeAutoStatus({silent});
  const cBody = document.getElementById('atd-candidates');
  if(cBody && !silent) cBody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:28px;color:var(--text3)">加载中...</td></tr>';
  try{
    const r = await _annFetch('GET', `/admin/ai-trade-decision/latest?max_candidates=${encodeURIComponent(maxCandidates)}`);
    if(r && r.ok){
      renderAiTradeDecision(r);
    }else{
      const error = (r && (r.error || r.message)) || '加载失败';
      if(cBody && !silent) cBody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:28px;color:var(--red)">${esc(error)}</td></tr>`;
      if(!silent) showToast(error, 'error');
    }
  }finally{
    aiTradeDecisionLoading = false;
  }
}

async function runAiTradeDecision(){
  if(!isRootAdmin()){
    navigate('403');
    return;
  }
  const btn = document.getElementById('atd-run-btn');
  const msg = document.getElementById('atd-msg');
  const body = {
    max_candidates: fmReadInt('atd-max', 6),
    execute_paper: !!document.getElementById('atd-execute-paper')?.checked,
    latest_only: true,
  };
  if(btn) btn.disabled = true;
  if(msg){ msg.style.color = 'var(--text2)'; msg.textContent = 'AI正在读取候选池并决策...'; }
  const r = await _annFetch('POST', '/admin/ai-trade-decision/run', body, 180000);
  if(btn) btn.disabled = false;
  if(r && r.ok){
    showToast('AI交易决策已完成', 'success');
    renderAiTradeDecision({candidates: [], decision: r});
    loadAiTradeDecision();
  }else{
    const error = (r && (r.error || r.message)) || 'AI交易决策失败';
    if(msg){ msg.style.color = 'var(--red)'; msg.textContent = error; }
    showToast(error, 'error');
  }
}

// ================================================================
// 行情数据源配置（第一数据源 / 第二数据源）
// ================================================================
async function loadDatasourceConfig(){
  const msg = document.getElementById('dsc-msg');
  if(msg) msg.textContent = '加载中…';
  const r = await _annFetch('GET', '/admin/datasource/config');
  if(!r || !r.ok){
    if(msg){ msg.style.color = 'var(--red)'; msg.textContent = (r&&r.error)||'加载失败'; }
    return;
  }
  document.getElementById('dsc-provider').value = r.provider || 'current';
  document.getElementById('dsc-mode').value = r.tushare_mode || 'replay';
  document.getElementById('dsc-token').value = '';
  document.getElementById('dsc-tushare-enabled').checked = !!r.tushare_enabled;
  document.getElementById('dsc-fallback-enabled').checked = r.fallback_enabled !== false;
  document.getElementById('dsc-current').textContent = '当前 Token/API Key: ' + (r.tushare_token_masked || '未配置');
  dscProviderChanged();
  if(msg){
    msg.style.color = 'var(--text2)';
    msg.textContent = `当前第一数据源：${r.provider === 'tushare' ? 'Tushare Pro' : '当前数据源'}；第二数据源：${r.fallback_enabled !== false ? '自动降级启用' : '禁用'}`;
  }
}

function dscProviderChanged(){
  const provider = document.getElementById('dsc-provider').value;
  if(provider === 'tushare') document.getElementById('dsc-tushare-enabled').checked = true;
}

async function dscSave(){
  const body = {
    provider: document.getElementById('dsc-provider').value,
    tushare_mode: document.getElementById('dsc-mode').value,
    tushare_enabled: document.getElementById('dsc-tushare-enabled').checked,
    fallback_enabled: document.getElementById('dsc-fallback-enabled').checked,
  };
  const token = document.getElementById('dsc-token').value.trim();
  if(token) body.tushare_token = token;
  const r = await _annFetch('POST', '/admin/datasource/config', body);
  if(r && r.ok){ showToast('数据源配置已保存，即时生效','success'); loadDatasourceConfig(); }
  else showToast((r&&r.error)||'保存失败','error');
}

async function dscTest(){
  const msg = document.getElementById('dsc-msg');
  msg.style.color = 'var(--text2)'; msg.textContent = '正在测试 Tushare 连接…';
  const token = document.getElementById('dsc-token').value.trim();
  const body = {tushare_mode: document.getElementById('dsc-mode').value};
  if(token) body.tushare_token = token;
  const r = await _annFetch('POST', '/admin/datasource/test', body, 30000);
  if(r && r.ok){
    msg.style.color = 'var(--green)';
    msg.textContent = `Tushare ${r.mode || 'replay'} 连接正常，样例 ${r.sample || '000001.SZ'} 返回 ${r.rows || 0} 条日线`;
  }else{
    msg.style.color = 'var(--red)';
    msg.textContent = (r&&r.error)||'测试失败';
  }
}

// ================================================================
// AI 大模型接口配置（全局唯一权威，保存即对所有AI功能生效）
// ================================================================
let _aicPresets = {};

async function loadAiConfig(){
  const r = await _annFetch('GET', '/ai/config');
  if(!r || r.error){ showToast((r&&r.error)||'加载失败（需管理员权限）','error'); return; }
  _aicPresets = r.presets || {};
  const sel = document.getElementById('aic-provider');
  const keys = Object.keys(_aicPresets);
  sel.innerHTML = keys.map(p=>`<option value="${p}">${(_aicPresets[p]||{}).name||p}</option>`).join('')
    || `<option value="${r.provider||''}">${r.provider||''}</option>`;
  if(r.provider) sel.value = r.provider;
  document.getElementById('aic-base').value  = r.base_url || '';
  document.getElementById('aic-model').value = r.model || '';
  document.getElementById('aic-key').value   = '';
  document.getElementById('aic-enabled').checked = !!r.enabled;
  document.getElementById('aic-cur').textContent = '当前 Key: ' + (r.api_key_masked || '未配置');
}

function aicPreset(){
  const p = _aicPresets[document.getElementById('aic-provider').value] || {};
  if(p.base_url) document.getElementById('aic-base').value  = p.base_url;
  if(p.model)    document.getElementById('aic-model').value = p.model;
}

async function aicSave(){
  const body = {
    provider: document.getElementById('aic-provider').value,
    base_url: document.getElementById('aic-base').value.trim(),
    model:    document.getElementById('aic-model').value.trim(),
    enabled:  document.getElementById('aic-enabled').checked,
  };
  const key = document.getElementById('aic-key').value.trim();
  if(key) body.api_key = key;
  const r = await _annFetch('POST', '/ai/config', body);
  if(r && r.ok){ showToast('已保存，全部AI功能即时生效','success'); loadAiConfig(); }
  else showToast((r&&r.error)||'保存失败','error');
}

async function aicDetect(){
  const msg = document.getElementById('aic-msg');
  msg.style.color = 'var(--text2)'; msg.textContent = '🔍 检测可用模型中…';
  const body = { base_url: document.getElementById('aic-base').value.trim() };
  const key = document.getElementById('aic-key').value.trim();
  if(key) body.api_key = key;
  const r = await _annFetch('POST', '/ai/detect_models', body, 30000);
  if(r && r.ok){
    // 自动回填：识别出的服务商 + 官方 Base URL（仅当对应输入框为空/可覆盖时）
    if(r.base_url && !document.getElementById('aic-base').value.trim())
      document.getElementById('aic-base').value = r.base_url;
    if(r.provider_guess && _aicPresets[r.provider_guess])
      document.getElementById('aic-provider').value = r.provider_guess;
    else if(!r.provider_guess && _aicPresets['custom'])
      document.getElementById('aic-provider').value = 'custom';
    const models = r.models || [];
    document.getElementById('aic-model-list').innerHTML =
      models.map(m=>`<option value="${esc(m)}">`).join('');
    const sel = document.getElementById('aic-model-sel');
    sel.innerHTML = models.map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('');
    sel.style.display = models.length ? '' : 'none';
    const mi = document.getElementById('aic-model');
    if(!mi.value.trim() && models.length) mi.value = models[0];
    msg.style.color = 'var(--green)';
    msg.textContent = `✅ 检测到 ${models.length} 个可用模型${r.total>models.length?`（已过滤非对话模型，原始 ${r.total} 个）`:''}，下方列表点击即可选用`;
  }else{
    document.getElementById('aic-model-sel').style.display = 'none';
    msg.style.color = 'var(--red)';
    msg.textContent = '❌ ' + ((r&&r.error)||'检测失败');
  }
}

function aicAutoDetect(){
  // key 输入完成（blur/回车）后自动探测；短于 8 位视为没填完不打扰
  const key = document.getElementById('aic-key').value.trim();
  if(key.length >= 8) aicDetect();
}

async function aicTest(){
  const msg = document.getElementById('aic-msg');
  msg.style.color = 'var(--text2)'; msg.textContent = '⏳ 测试中…';
  const body = {
    base_url: document.getElementById('aic-base').value.trim(),
    model:    document.getElementById('aic-model').value.trim(),
  };
  const key = document.getElementById('aic-key').value.trim();
  if(key) body.api_key = key;
  const r = await _annFetch('POST', '/ai/test', body);
  if(r && r.ok){ msg.style.color = 'var(--green)'; msg.textContent = `✅ 连通 (${r.model}) 回复: ${r.reply||'OK'}`; }
  else { msg.style.color = 'var(--red)'; msg.textContent = '❌ ' + ((r&&r.error)||'连接失败'); }
}

// ================================================================
// 积分设置（各功能调用消耗，保存即时生效）
// ================================================================
async function loadPointCosts(){
  const box = document.getElementById('pc-body');
  box.innerHTML = '<div class="empty" style="padding:30px">加载中…</div>';
  try{
    const r = await _annFetch('GET', '/admin/point_costs');
    if(!r || !r.ok) throw new Error((r&&r.error)||'加载失败');
    let html='';
    Object.entries(r.costs||{}).forEach(([k,v])=>{
      const lbl = (r.labels||{})[k] || k;
      html += `<div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
        <span style="flex:1">${lbl}<span style="color:var(--text3);font-size:11px;margin-left:8px;font-family:monospace">${k}</span></span>
        <input class="form-input pc-input" data-key="${k}" type="number" min="0" step="1" value="${v}" style="width:110px;text-align:right">
        <span style="color:var(--text2);font-size:12px;width:40px">分/次</span>
      </div>`;
    });
    box.innerHTML = html || '<div class="empty">无配置项</div>';
  }catch(e){
    box.innerHTML = `<div class="empty" style="padding:30px;color:#e05661">加载失败：${e.message}</div>`;
  }
}

async function savePointCosts(){
  const costs = {};
  document.querySelectorAll('.pc-input').forEach(i=>{ costs[i.dataset.key] = parseInt(i.value)||0; });
  const r = await _annFetch('POST', '/admin/point_costs', {costs});
  if(r && r.ok){ showToast('已保存，即时生效','success'); loadPointCosts(); }
  else showToast((r&&r.error)||'保存失败','error');
}

// ================================================================
// 邀请码（邀请制注册）
// ================================================================
function _copyInv(t){
  try{ navigator.clipboard.writeText(t); showToast('已复制：'+t,'success'); }
  catch(e){ showToast('复制失败，请手动复制','warning'); }
}

async function loadInvites(){
  const tb = document.getElementById('inv-tbody');
  tb.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:30px;color:var(--text3)">加载中…</td></tr>';
  try{
    const r = await _annFetch('GET', '/admin/invite_codes');
    const list = r.codes || [];
    if(!list.length){ tb.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--text3)">暂无邀请码，请在上方生成</td></tr>'; return; }
    tb.innerHTML = list.map(c=>{
      const active = c.is_active!==0;
      const full   = c.used_count>=c.max_uses;
      const st = !active ? '<span style="color:var(--red)">已作废</span>'
               : full     ? '<span style="color:var(--orange)">已用完</span>'
               :            '<span style="color:var(--green)">可用</span>';
      const act = active ? `<button class="btn btn-danger" style="padding:3px 10px;font-size:12px" data-ui-click="revokeInvite(${c.id})">作废</button>` : '—';
      return `<tr>
        <td><b style="font-family:monospace;letter-spacing:1px;cursor:pointer" title="点击复制" data-ui-click="_copyInv('${c.code}')">${c.code}</b></td>
        <td>${c.creator_name||('#'+c.created_by)}</td>
        <td>${c.used_count}/${c.max_uses}</td>
        <td>${c.reward}分</td>
        <td>${c.note||'—'}</td>
        <td>${st}</td>
        <td>${act}</td>
      </tr>`;
    }).join('');
  }catch(e){ tb.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:30px;color:var(--red)">加载失败</td></tr>'; }
}

async function createInvite(){
  const max_uses = parseInt(document.getElementById('inv-max').value)||1;
  const reward   = parseInt(document.getElementById('inv-reward').value);
  const note     = document.getElementById('inv-note').value.trim();
  const r = await _annFetch('POST', '/admin/invite_codes', {max_uses, reward:isNaN(reward)?0:reward, note});
  if(r && r.ok){
    showToast('邀请码已生成：'+r.code,'success');
    document.getElementById('inv-new').innerHTML =
      `<div style="padding:10px 14px;background:var(--green);color:#fff;border-radius:6px;font-size:14px">
        新邀请码 <b style="font-family:monospace;letter-spacing:2px;font-size:18px;cursor:pointer" title="点击复制" data-ui-click="_copyInv('${r.code}')">${r.code}</b>
        · 名额 ${r.max_uses} · 每邀请奖励 ${r.reward} 分　<span style="opacity:.85">(点码复制，发给被邀请人)</span>
      </div>`;
    document.getElementById('inv-note').value='';
    loadInvites();
  } else {
    showToast((r&&r.error)||'生成失败','error');
  }
}

function revokeInvite(id){
  showConfirm('确定作废此邀请码吗？作废后不能再用于注册。','btn-danger',async()=>{
    closeModal('modalConfirm');
    const r = await _annFetch('DELETE', '/admin/invite_codes/'+id);
    if(r && r.ok){ showToast('已作废','success'); loadInvites(); }
    else showToast((r&&r.error)||'操作失败','error');
  });
}

// ================================================================
// 问题反馈
// ================================================================
const FB_CAT_LABELS = {bug:'🐞 功能异常', suggestion:'💡 功能建议', data:'📊 数据问题', other:'💬 其他'};
const FB_STATUS_OPTS = [['open','待处理'],['read','已读'],['in_progress','处理中'],['resolved','已解决']];
const FB_STATUS_COLOR = {open:'var(--orange)', read:'var(--text3)', in_progress:'var(--blue)', resolved:'var(--green)'};

// ══════════════════════════════════════════════════
//  更新日志管理
// ══════════════════════════════════════════════════
// ================================================================
// 找回密码申请（后台审批重置）
// ================================================================
async function loadResetRequests(){
  const tb = document.getElementById('resetReqBody');
  try{
    const r = await _annFetch('GET', '/admin/reset_requests');
    const list = (r && r.requests) || [];
    if(!list.length){
      tb.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text3)">暂无待处理的找回申请</td></tr>';
      return;
    }
    tb.innerHTML = list.map(q=>`<tr>
      <td><b>${esc(q.username)}</b></td>
      <td>${esc(q.current_email || q.email || '—')}</td>
      <td style="font-size:12px;color:var(--text3)">${esc(q.created_at || '')}</td>
      <td>${q.is_active===0 ? '<span style="color:var(--red)">已禁用</span>' : '正常'}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-primary" style="padding:4px 10px;font-size:12px" data-reset-request data-reset-id="${esc(q.id)}" data-reset-name="${esc(q.username)}">重置并生成临时密码</button>
        <button class="btn btn-outline" style="padding:4px 10px;font-size:12px" data-ui-click="rejectResetReq(${q.id})">驳回</button>
      </td></tr>`).join('');
    tb.querySelectorAll('[data-reset-request]').forEach(btn=>{
      btn.addEventListener('click', ()=>resolveResetReq(Number(btn.dataset.resetId), btn.dataset.resetName));
    });
  }catch(e){
    tb.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--red)">加载失败（后端需重启一次才生效）</td></tr>';
  }
}

function resolveResetReq(id, uname){
  showConfirm(`确定为用户 <b>${esc(uname)}</b> 生成临时密码并重置吗？`, 'btn-primary', async()=>{
    const r = await _annFetch('POST', '/admin/reset_requests/'+id+'/resolve');
    if(r && r.ok){
      showResetResult(r.username, r.temp_password);
      loadResetRequests();
    } else {
      showToast((r && r.error) || '操作失败', 'error');
    }
  });
}

function rejectResetReq(id){
  showConfirm('确定驳回此找回申请吗？（不会重置密码）', 'btn-danger', async()=>{
    const r = await _annFetch('POST', '/admin/reset_requests/'+id+'/reject');
    if(r && r.ok){ showToast('已驳回', 'success'); loadResetRequests(); }
    else showToast((r && r.error) || '操作失败', 'error');
  });
}

function showResetResult(uname, pw){
  const box = document.getElementById('resetResultBox');
  box.style.display = 'block';
  box.innerHTML = `<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <i class="fas fa-check-circle" style="color:var(--green);font-size:20px"></i>
    <span>已重置 <b>${esc(uname)}</b> 的密码，临时密码（请通过 QQ/微信告知该用户，并让其登录后立即改密）：</span>
    <span id="tempPwVal" style="font-family:monospace;font-size:18px;font-weight:700;letter-spacing:1px;background:var(--theme-light,#eee);color:#222;padding:4px 12px;border-radius:6px">${esc(pw)}</span>
    <button class="btn btn-outline" style="padding:4px 12px;font-size:12px" data-ui-click="copyTempPw('${esc(pw)}')"><i class="fas fa-copy"></i> 复制</button>
  </div>`;
}

function copyTempPw(pw){
  if(navigator.clipboard){ navigator.clipboard.writeText(pw).then(()=>showToast('已复制临时密码','success'),()=>showToast('复制失败，请手动选中','warning')); }
  else { showToast('请手动选中复制','warning'); }
}

// ================================================================
// 通知设置（消息推送渠道）
// ================================================================
async function loadNotifyConfig(){
  const r = await _annFetch('GET', '/user/notify_config');
  if(!r || !r.ok){ showToast('加载失败（请重新登录）', 'error'); return; }
  const set = (id, v) => { const el = document.getElementById(id); if(el) el.value = v || ''; };
  const chk = (id, v) => { const el = document.getElementById(id); if(el) el.checked = !!v; };
  chk('nc_wecom_en',    (r.wecom||{}).enabled);    set('nc_wecom_webhook',    (r.wecom||{}).webhook);
  chk('nc_feishu_en',   (r.feishu||{}).enabled);   set('nc_feishu_webhook',   (r.feishu||{}).webhook);
  chk('nc_dingtalk_en', (r.dingtalk||{}).enabled); set('nc_dingtalk_webhook', (r.dingtalk||{}).webhook);
  set('nc_dingtalk_secret', (r.dingtalk||{}).secret);
  chk('nc_telegram_en', (r.telegram||{}).enabled); set('nc_telegram_token',   (r.telegram||{}).token);
  set('nc_telegram_chatid', (r.telegram||{}).chat_id);
}

async function saveNotifyConfig(){
  const v = id => (document.getElementById(id).value || '').trim();
  const c = id => document.getElementById(id).checked;
  const payload = {
    wecom:    { enabled: c('nc_wecom_en'),    webhook: v('nc_wecom_webhook') },
    feishu:   { enabled: c('nc_feishu_en'),   webhook: v('nc_feishu_webhook') },
    dingtalk: { enabled: c('nc_dingtalk_en'), webhook: v('nc_dingtalk_webhook'), secret: v('nc_dingtalk_secret') },
    telegram: { enabled: c('nc_telegram_en'), token: v('nc_telegram_token'), chat_id: v('nc_telegram_chatid') },
  };
  const r = await _annFetch('POST', '/user/notify_config', payload);
  const msg = document.getElementById('nc_msg');
  if(r && r.ok){ msg.style.color = 'var(--green)'; msg.textContent = '✅ 已保存。建议点「发送测试」确认能收到。'; }
  else { msg.style.color = 'var(--red)'; msg.textContent = '保存失败：' + ((r && r.error) || '未知（后端需重启一次？）'); }
}

async function testNotify(){
  const msg = document.getElementById('nc_msg');
  msg.style.color = 'var(--text3)'; msg.textContent = '发送中…';
  const r = await _annFetch('POST', '/notify/test');
  if(r && r.ok){ msg.style.color = 'var(--green)'; msg.textContent = '✅ 测试已发送，请到对应 App/群查收。没收到就核对 Webhook 是否正确、开关是否打开、是否已点保存。'; }
  else {
    const detail = (r && r.error) || (r && r.results ? JSON.stringify(r.results) : '未启用任何渠道，或 Webhook 错误');
    msg.style.color = 'var(--red)'; msg.textContent = '测试失败：' + detail;
  }
}

async function loadChangelogAdmin(){
  const box = document.getElementById('cl-list-body')
  box.innerHTML = '<div class="empty" style="padding:30px">加载中…</div>'
  try{
    const r = await (await fetch(`${API}/admin/changelog`, {credentials:'include'})).json()
    const list = r.items || []
    if(!list.length){ box.innerHTML = '<div class="empty" style="padding:40px">暂无更新日志</div>'; return }
    const _tLabel = {feature:'🆕 新功能',fix:'🐛 修复',optimize:'⚡ 优化',security:'🔒 安全',notice:'📢 公告'}
    const _tColor = {feature:'var(--green)',fix:'var(--red)',optimize:'var(--orange)',security:'var(--purple)',notice:'var(--blue)'}
    let html = '<table><thead><tr><th>类型</th><th>版本</th><th>标题</th><th>状态</th><th>发布时间</th><th>操作</th></tr></thead><tbody>'
    list.forEach(c => {
      const t = _tLabel[c.change_type] || c.change_type
      const tc = _tColor[c.change_type] || 'var(--text2)'
      const pub = c.is_published ? '<span class="tag tag-green">已发布</span>' : '<span class="tag" style="background:#fff3cd;color:#856404">草稿</span>'
      html += `<tr>
        <td><span style="color:${tc};font-weight:600">${t}</span></td>
        <td><code>${c.version||'-'}</code></td>
        <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c.title}</td>
        <td>${pub}</td>
        <td style="font-size:12px;color:var(--text3)">${c.created_at||''}</td>
        <td>
          <button class="btn btn-outline btn-sm" data-ui-click="editChangelog(${c.id})" title="编辑"><i class="fas fa-edit"></i></button>
          <button class="btn btn-danger btn-sm" data-ui-click="deleteChangelog(${c.id})" title="删除"><i class="fas fa-trash"></i></button>
        </td>
      </tr>`
    })
    html += '</tbody></table>'
    box.innerHTML = html
  }catch(e){ box.innerHTML = '<div class="empty" style="padding:40px;color:var(--red)">加载失败</div>' }
}
async function createChangelog(){
  const title = document.getElementById('cl-title').value.trim()
  if(!title){ alert('标题不能为空'); return }
  const body = {
    title,
    version: document.getElementById('cl-version').value.trim(),
    change_type: document.getElementById('cl-type').value,
    content: document.getElementById('cl-content').value.trim(),
    is_published: 1
  }
  try{
    const r = await (await fetch(`${API}/admin/changelog`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      credentials:'include', body:JSON.stringify(body)
    })).json()
    if(r.ok){
      document.getElementById('cl-title').value = ''
      document.getElementById('cl-version').value = ''
      document.getElementById('cl-content').value = ''
      loadChangelogAdmin()
    } else { alert(r.error || '发布失败') }
  }catch(e){ alert('网络错误') }
}
async function deleteChangelog(id){
  if(!confirm('确定删除这条更新日志？')) return
  try{
    await fetch(`${API}/admin/changelog/${id}`, {method:'DELETE', credentials:'include'})
    loadChangelogAdmin()
  }catch(e){ alert('删除失败') }
}
// 编辑改为「回填上方发布表单」（与公告 editAnn 同一套交互），
// 取代原来看不到原文、改不了类型/版本、留空规则靠猜的连环 prompt()。
let _clEditId = null
async function editChangelog(id){
  let item = null
  try{
    const r = await (await fetch(`${API}/admin/changelog`, {credentials:'include'})).json()
    const list = r.items || []
    item = list.find(c => String(c.id) === String(id)) || null
  }catch(e){}
  if(!item){ showToast('读取该条更新日志失败，无法回填','warning'); return }
  _clEditId = id
  document.getElementById('cl-title').value   = item.title || ''
  document.getElementById('cl-version').value = item.version || ''
  document.getElementById('cl-content').value = item.content || ''
  const typeEl = document.getElementById('cl-type')
  if(typeEl && item.change_type) typeEl.value = item.change_type
  const btn = document.querySelector('[data-ui-click="createChangelog()"]')
  if(btn){ btn.innerHTML = '<i class="fas fa-save"></i> 更新'; btn.setAttribute('data-ui-click','updateChangelog()') }
  document.getElementById('cl-title').scrollIntoView({behavior:'smooth', block:'center'})
  document.getElementById('cl-title').focus()
  showToast('已载入到上方表单，修改后点「更新」','info')
}

function _clExitEditMode(){
  _clEditId = null
  document.getElementById('cl-title').value = ''
  document.getElementById('cl-version').value = ''
  document.getElementById('cl-content').value = ''
  const btn = document.querySelector('[data-ui-click="updateChangelog()"]')
  if(btn){ btn.innerHTML = '<i class="fas fa-plus"></i> 发布'; btn.setAttribute('data-ui-click','createChangelog()') }
}

async function updateChangelog(){
  if(!_clEditId){ createChangelog(); return }
  const title = document.getElementById('cl-title').value.trim()
  if(!title){ showToast('标题不能为空','warning'); return }
  const body = {
    title,
    version: document.getElementById('cl-version').value.trim(),
    change_type: document.getElementById('cl-type').value,
    content: document.getElementById('cl-content').value.trim(),
  }
  try{
    const r = await (await fetch(`${API}/admin/changelog/${_clEditId}`, {
      method:'PUT', headers:{'Content-Type':'application/json'},
      credentials:'include', body:JSON.stringify(body)
    })).json()
    if(r && r.ok !== false){
      showToast('更新日志已更新','success')
      _clExitEditMode()
      loadChangelogAdmin()
    } else { showToast((r&&r.error)||'更新失败','error') }
  }catch(e){ showToast('更新失败','error') }
}

async function loadFeedback(){
  const box = document.getElementById('fb-list-body');
  const status = document.getElementById('fb-filter').value;
  box.innerHTML = '<div class="empty" style="padding:30px">加载中…</div>';
  try {
    const r = await (await fetch(`${API}/feedback` + (status?`?status=${status}`:''), {credentials:'include'})).json();
    const list = r.items || [];
    const c = r.counts || {};
    document.getElementById('fb-counts').innerHTML =
      `共 <b>${c.total||0}</b> 条 · 待处理 <b style="color:var(--orange)">${c.open||0}</b>` +
      ` · 处理中 <b style="color:var(--blue)">${c.in_progress||0}</b>` +
      ` · 已解决 <b style="color:var(--green)">${c.resolved||0}</b>`;
    if(!list.length){ box.innerHTML = '<div class="empty" style="padding:40px">暂无反馈</div>'; return; }
    box.innerHTML = list.map(renderFbRow).join('');
  } catch(e){
    box.innerHTML = `<div class="empty" style="padding:30px;color:#e74c3c">加载失败：${e.message}</div>`;
  }
}

function renderFbRow(f){
  const cat  = FB_CAT_LABELS[f.category] || f.category;
  const opts = FB_STATUS_OPTS.map(([v,l])=>`<option value="${v}"${f.status===v?' selected':''}>${l}</option>`).join('');
  const stColor = FB_STATUS_COLOR[f.status] || 'var(--text3)';
  const stLabel = (FB_STATUS_OPTS.find(o=>o[0]===f.status)||[,f.status])[1];
  const who  = f.username ? esc(f.username) : '匿名';
  return `<div id="fb-row-${f.id}" style="border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:12px;background:var(--card-bg)">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--text2)">
      <b style="color:var(--text1)">#${f.id}</b>
      <span>${cat}</span>
      <span id="fb-badge-${f.id}" style="color:${stColor};font-weight:600">● ${stLabel}</span>
      <span>· 👤 ${who}</span>
      <span>· 📄 ${f.page?esc(f.page):'-'}</span>
      <span>· 🕐 ${String(f.created_at||'').slice(0,16)}</span>
      ${f.contact?`<span>· ✉ ${esc(f.contact)}</span>`:''}
    </div>
    <div style="color:var(--text1);font-size:13px;margin:10px 0;white-space:pre-wrap;line-height:1.7">${esc(f.content)}</div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <select id="fb-st-${f.id}" class="form-input" style="width:auto;padding:6px 10px;font-size:12px">${opts}</select>
      <input id="fb-note-${f.id}" class="form-input" style="flex:1;min-width:180px;padding:6px 10px;font-size:12px"
             placeholder="处理备注 / 回复用户（用户在前端可见）" value="${esc(f.admin_note)}">
      <button class="btn btn-primary btn-sm" data-ui-click="saveFeedback(${f.id})" style="padding:6px 16px">
        <i class="fas fa-save"></i> 保存
      </button>
    </div>
    ${f.ua?`<div style="font-size:10px;color:var(--text3);margin-top:8px">UA：${esc(f.ua)}</div>`:''}
  </div>`;
}

async function saveFeedback(id){
  const status = document.getElementById('fb-st-'+id).value;
  const admin_note = document.getElementById('fb-note-'+id).value;
  try {
    const r = await (await fetch(`${API}/feedback/${id}/status`, {
      method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({status, admin_note})
    })).json();
    if(r && r.ok){
      showToast('已保存','success');
      // 只更新该行的状态徽标，不再整表重载 —— 原来 loadFeedback() 会让滚动位置回到顶部，
      // 连续处理多条反馈时每保存一条都要重新滚回原位。
      const badge = document.getElementById('fb-badge-'+id);
      if(badge){
        const label = (FB_STATUS_OPTS.find(o=>o[0]===status)||[,status])[1];
        badge.style.color = FB_STATUS_COLOR[status] || 'var(--text3)';
        badge.textContent = '● ' + label;
      }
      const row = document.getElementById('fb-row-'+id);
      if(row){   // 轻微高亮一下，明确"这条已保存"
        row.style.transition = 'background .25s';
        const bg = row.style.background;
        row.style.background = 'rgba(103,194,58,.12)';
        setTimeout(()=>{ row.style.background = bg; }, 700);
      }
    }
    else showToast('保存失败','warning');
  } catch(e){ showToast('保存失败：'+e.message,'warning'); }
}

async function loadAnnouncements(){
  const box = document.getElementById('ann-list-body');
  box.innerHTML = '<div class="empty" style="padding:30px">加载中…</div>';
  try {
    const r    = await _annFetch('GET', '/admin/announcements');
    const list = r.announcements || [];
    if(!list.length){
      box.innerHTML = '<div class="empty" style="padding:40px">暂无公告，请在上方发布第一条</div>';
      return;
    }
    box.innerHTML = list.map(a => {
      const typeLabel = ANN_TYPE_LABELS[a.type] || a.type;
      const typeCls   = a.type || 'info';
      const active    = a.is_active !== 0;
      const expires   = a.expires_at ? `· 过期：${String(a.expires_at).slice(0,16)}` : '· 永久有效';
      const creator   = a.creator   ? `· 发布者：${a.creator}` : '';
      return `<div class="ann-row" id="ann-row-${a.id}">
        <span class="ann-badge ${typeCls}">${typeLabel}</span>
        <div style="flex:1;min-width:0">
          <div class="ann-title">${esc(a.title)}
            <span class="${active?'ann-status-on':'ann-status-off'}" style="font-weight:400;margin-left:8px">
              ${active ? '● 显示中' : '○ 已隐藏'}
            </span>
          </div>
          <div class="ann-content">${esc(a.content)}</div>
          <div class="ann-meta">ID：${a.id} · 创建：${String(a.created_at||'').slice(0,16)} ${expires} ${creator}</div>
        </div>
        <div class="ann-actions">
          <button class="btn btn-outline btn-sm" data-ui-click="toggleAnn(${a.id},${active?0:1})" title="${active?'隐藏':'显示'}">
            <i class="fas fa-${active?'eye-slash':'eye'}"></i>
          </button>
          <button class="btn btn-outline btn-sm" data-ui-click="editAnn(${a.id})" title="编辑">
            <i class="fas fa-edit"></i>
          </button>
          <button class="btn btn-danger btn-sm" data-ui-click="deleteAnn(${a.id})" title="删除">
            <i class="fas fa-trash"></i>
          </button>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    box.innerHTML = `<div class="empty" style="padding:30px;color:#e74c3c">加载失败：${e.message}</div>`;
  }
}

async function createAnnouncement(){
  const title   = document.getElementById('ann-title').value.trim();
  const content = document.getElementById('ann-content').value.trim();
  const type    = document.getElementById('ann-type').value;
  const expires = document.getElementById('ann-expires').value;
  if(!title)  { showToast('请输入公告标题','warning'); return; }
  if(!content){ showToast('请输入公告内容','warning'); return; }
  const r = await _annFetch('POST', '/admin/announcements', {
    title, content, type,
    expires_at: expires ? expires.replace('T',' ')+':00' : null
  });
  if(r && r.ok){
    showToast('公告已发布','success');
    document.getElementById('ann-title').value   = '';
    document.getElementById('ann-content').value = '';
    document.getElementById('ann-expires').value = '';
    loadAnnouncements();
  } else {
    showToast((r&&r.error)||'发布失败','error');
  }
}

async function toggleAnn(id, active){
  const r = await _annFetch('PUT', `/admin/announcements/${id}`, {active});
  if(r && r.ok){ showToast(active?'已设为显示':'已隐藏','success'); loadAnnouncements(); }
  else showToast((r&&r.error)||'操作失败','error');
}

async function deleteAnn(id){
  if(!confirm('确定删除该公告？删除后不可恢复。')) return;
  const r = await _annFetch('DELETE', `/admin/announcements/${id}`);
  if(r && r.ok){ showToast('已删除','success'); loadAnnouncements(); }
  else showToast((r&&r.error)||'删除失败','error');
}

function editAnn(id){
  const row = document.getElementById(`ann-row-${id}`);
  if(!row) return;
  const title   = row.querySelector('.ann-title').childNodes[0].textContent.trim();
  const content = row.querySelector('.ann-content').textContent;
  document.getElementById('ann-title').value         = title;
  document.getElementById('ann-content').value       = content;
  document.getElementById('ann-title').dataset.editId = id;
  const btn = document.querySelector('[data-ui-click="createAnnouncement()"]');
  if(btn){ btn.innerHTML='<i class="fas fa-save"></i> 更新公告'; btn.setAttribute('data-ui-click','updateAnnouncement()'); }
  document.getElementById('ann-title').scrollIntoView({behavior:'smooth'});
}

async function updateAnnouncement(){
  const titleEl = document.getElementById('ann-title');
  const id      = titleEl.dataset.editId;
  if(!id){ createAnnouncement(); return; }
  const title   = titleEl.value.trim();
  const content = document.getElementById('ann-content').value.trim();
  const type    = document.getElementById('ann-type').value;
  const expires = document.getElementById('ann-expires').value;
  if(!title||!content){ showToast('标题和内容不能为空','warning'); return; }
  const r = await _annFetch('PUT', `/admin/announcements/${id}`, {
    title, content, type,
    expires_at: expires ? expires.replace('T',' ')+':00' : null
  });
  if(r && r.ok){
    showToast('公告已更新','success');
    titleEl.value = ''; titleEl.dataset.editId = '';
    document.getElementById('ann-content').value = '';
    document.getElementById('ann-expires').value = '';
    const btn = document.querySelector('[data-ui-click="updateAnnouncement()"]');
    if(btn){ btn.innerHTML='<i class="fas fa-paper-plane"></i> 发布公告'; btn.setAttribute('data-ui-click','createAnnouncement()'); }
    loadAnnouncements();
  } else {
    showToast((r&&r.error)||'更新失败','error');
  }
}
