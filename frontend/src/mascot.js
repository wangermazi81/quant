// 见微 Alpha 糖果主题 · 牛熊吉祥物招牌横幅
// 设计还原自样稿（金牛 / 学院派泰迪熊），并接【真实大盘行情态】：
// 观察现有 #mktTrendMon(class: mkt-bull/mkt-neutral/mkt-bear) + #mktStrengthNumMon + #mktAdviceMon，
// 完全解耦——不改任何交易/行情逻辑，只读取它们已经渲染好的结果。

const BULL_SVG = `
<svg viewBox="0 0 100 100" class="w-full h-full" style="width:100%;height:100%">
  <ellipse cx="50" cy="88" rx="30" ry="6" fill="#FFCCD3" opacity="0.4"/>
  <path d="M 12 36 Q 2 34 8 46 Q 16 46 16 38 Z" fill="#FFA5A5" stroke="#4A2F25" stroke-width="2.5" stroke-linejoin="round"/>
  <path d="M 88 36 Q 98 34 92 46 Q 84 46 84 38 Z" fill="#FFA5A5" stroke="#4A2F25" stroke-width="2.5" stroke-linejoin="round"/>
  <path d="M 28 24 Q 14 16 18 34 Q 24 34 30 28 Z" fill="url(#mGold)" stroke="#4A2F25" stroke-width="2.5" stroke-linejoin="round"/>
  <path d="M 72 24 Q 86 16 82 34 Q 76 34 70 28 Z" fill="url(#mGold)" stroke="#4A2F25" stroke-width="2.5" stroke-linejoin="round"/>
  <ellipse cx="50" cy="55" rx="36" ry="32" fill="#FFEEDB" stroke="#4A2F25" stroke-width="3"/>
  <ellipse cx="23" cy="62" rx="7" ry="5" fill="#FF7B93" opacity="0.65"/>
  <ellipse cx="77" cy="62" rx="7" ry="5" fill="#FF7B93" opacity="0.65"/>
  <circle cx="34" cy="48" r="8.5" fill="#3D2B1F"/><circle cx="32" cy="44.5" r="3.2" fill="#FFF"/><circle cx="36.5" cy="51" r="1.5" fill="#FFF"/>
  <circle cx="66" cy="48" r="8.5" fill="#3D2B1F"/><circle cx="64" cy="44.5" r="3.2" fill="#FFF"/><circle cx="68.5" cy="51" r="1.5" fill="#FFF"/>
  <circle cx="34" cy="48" r="12" fill="none" stroke="#52B788" stroke-width="3"/>
  <circle cx="66" cy="48" r="12" fill="none" stroke="#52B788" stroke-width="3"/>
  <line x1="46" y1="48" x2="54" y2="48" stroke="#52B788" stroke-width="3"/>
  <ellipse cx="50" cy="69" rx="20" ry="12" fill="#FFD2D8" stroke="#4A2F25" stroke-width="2.5"/>
  <ellipse cx="44" cy="67" rx="2.5" ry="1.8" fill="#4A2F25"/><ellipse cx="56" cy="67" rx="2.5" ry="1.8" fill="#4A2F25"/>
  <path d="M 46 73 C 46 80, 54 80, 54 73" fill="none" stroke="url(#mGold)" stroke-width="3.5" stroke-linecap="round"/>
  <defs><linearGradient id="mGold" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#FFF280"/><stop offset="50%" stop-color="#FFC93C"/><stop offset="100%" stop-color="#D89200"/></linearGradient></defs>
</svg>`;

const BEAR_SVG = `
<svg viewBox="0 0 100 100" class="w-full h-full" style="width:100%;height:100%">
  <ellipse cx="50" cy="88" rx="28" ry="5" fill="#E6DFD5" opacity="0.6"/>
  <path d="M 18 38 C 12 26, 28 18, 34 32" fill="#B07D62" stroke="#4A3525" stroke-width="2.5" stroke-linecap="round"/>
  <path d="M 21 36 C 16 28, 26 24, 29 32" fill="#FFCCD3"/>
  <path d="M 82 38 C 88 26, 72 18, 66 32" fill="#B07D62" stroke="#4A3525" stroke-width="2.5" stroke-linecap="round"/>
  <path d="M 79 36 C 84 28, 74 24, 71 32" fill="#FFCCD3"/>
  <ellipse cx="50" cy="56" rx="36" ry="30" fill="#DDB892" stroke="#4A3525" stroke-width="2.5"/>
  <ellipse cx="23" cy="62" rx="6" ry="4" fill="#FF8C9F" opacity="0.6"/>
  <ellipse cx="77" cy="62" rx="6" ry="4" fill="#FF8C9F" opacity="0.6"/>
  <circle cx="35" cy="48" r="8.2" fill="#4A3525"/><circle cx="33" cy="45" r="3" fill="#FFF"/><circle cx="37" cy="51" r="1.2" fill="#FFF"/>
  <circle cx="65" cy="48" r="8.2" fill="#4A3525"/><circle cx="63" cy="45" r="3" fill="#FFF"/><circle cx="67" cy="51" r="1.2" fill="#FFF"/>
  <circle cx="35" cy="48" r="11" fill="none" stroke="#D08C60" stroke-width="2"/>
  <circle cx="65" cy="48" r="11" fill="none" stroke="#D08C60" stroke-width="2"/>
  <line x1="46" y1="48" x2="54" y2="48" stroke="#D08C60" stroke-width="2"/>
  <ellipse cx="50" cy="65" rx="14" ry="9" fill="#FFF2E6" stroke="#4A3525" stroke-width="2"/>
  <path d="M 45 61 Q 50 57, 55 61 Q 50 64, 45 61 Z" fill="#4A3525"/>
  <path d="M 50 63 Q 50 67, 47 68 M 50 63 Q 50 67, 53 68" fill="none" stroke="#4A3525" stroke-width="1.8" stroke-linecap="round"/>
  <path d="M 22 74 C 22 74, 50 86, 78 74 C 82 68, 78 66, 78 66 C 78 66, 50 76, 22 66 C 22 66, 18 68, 22 74 Z" fill="#4A7C59" stroke="#4A3525" stroke-width="2"/>
  <path d="M 64 73 C 64 85, 72 92, 72 92 L 79 88 C 79 88, 73 80, 73 72" fill="#385F43" stroke="#4A3525" stroke-width="2" stroke-linejoin="round"/>
</svg>`;

const STATES = {
  bull:    { svg: BULL_SVG, tag: '🚀 牛市突围 · 情绪拉满', cls: 'mas-bull',
             msg: '多头气势如虹！牛牛已启动 L2 高频扫单与超额获利模式，盯紧封板强度别恋战~ 🐂' },
  neutral: { svg: BULL_SVG, tag: '🌤️ 震荡分歧 · 谨慎观望', cls: 'mas-neutral',
             msg: '行情分歧、量能未定。牛牛建议轻仓试错、等待方向选择，别在中枢里追高杀跌。' },
  bear:    { svg: BEAR_SVG, tag: '🛡️ 熊市避险 · 防御模式', cls: 'mas-bear',
             msg: '熊熊观测到行情承压，防御因子全面亮起。缩减波动暴露、守护好钱袋子 🧣' },
};

function pickState() {
  const t = document.getElementById('mktTrendMon');
  const cls = t ? t.className : '';
  if (cls.includes('mkt-bull')) return 'bull';
  if (cls.includes('mkt-bear')) return 'bear';
  if (cls.includes('mkt-neutral')) return 'neutral';
  // 兜底：用强度数字
  const s = parseInt((document.getElementById('mktStrengthNumMon') || {}).textContent, 10);
  if (!isNaN(s)) return s >= 70 ? 'bull' : s >= 45 ? 'neutral' : 'bear';
  return 'bull';
}

function build() {
  const page = document.getElementById('page-monitor');
  if (!page || document.getElementById('masBanner')) return false;
  const el = document.createElement('div');
  el.id = 'masBanner';
  el.className = 'mas-banner';
  el.innerHTML = `
    <div class="mas-avatar" id="masAvatar"></div>
    <div class="mas-body">
      <div class="mas-tags">
        <span class="mas-pill" id="masPill"></span>
        <span class="mas-sub">见微 AI 实时风控核心已绑定 · 强度 <b id="masStrength">—</b>/100</span>
      </div>
      <p class="mas-msg" id="masMsg"></p>
    </div>`;
  // 插到监控页最顶部（行情横幅之上）
  page.insertBefore(el, page.firstChild);
  return true;
}

let _last = '';
function update() {
  const banner = document.getElementById('masBanner');
  if (!banner) return;
  const key = pickState();
  const st = STATES[key];
  const strength = (document.getElementById('mktStrengthNumMon') || {}).textContent || '—';
  const advice = (document.getElementById('mktAdviceMon') || {}).textContent || '';
  const sig = key + '|' + strength + '|' + advice;
  if (sig === _last) return;
  _last = sig;
  banner.className = 'mas-banner ' + st.cls;
  document.getElementById('masAvatar').innerHTML = st.svg;
  document.getElementById('masPill').textContent = st.tag;
  document.getElementById('masStrength').textContent = strength;
  document.getElementById('masMsg').textContent = advice ? (st.msg + '　｜　' + advice) : st.msg;
}

export function initMascot() {
  if (!build()) {
    // 监控页可能稍后才出现，重试几次
    let tries = 0;
    const t = setInterval(() => { if (build() || ++tries > 20) { clearInterval(t); update(); } }, 300);
  }
  update();
  // 观察行情趋势元素变化，实时跟随牛/熊/中性
  const trend = document.getElementById('mktTrendMon');
  if (trend && window.MutationObserver) {
    new MutationObserver(update).observe(trend, { attributes: true, childList: true, characterData: true, subtree: true });
  }
  setInterval(update, 15000); // 兜底轮询
}
