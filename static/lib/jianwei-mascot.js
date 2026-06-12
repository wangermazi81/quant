/* 见微 Alpha 糖果主题 · 牛熊吉祥物（独立经典脚本，供 / 的 index.html 直接加载，免重启）
   接真实大盘行情态：观察 #mktTrendMon(class) + #mktStrengthNumMon + #mktAdviceMon，解耦不改逻辑。*/
(function(){
  var BULL='<svg viewBox="0 0 100 100" style="width:100%;height:100%">'+
    '<ellipse cx="50" cy="88" rx="30" ry="6" fill="#FFCCD3" opacity="0.4"/>'+
    '<path d="M 12 36 Q 2 34 8 46 Q 16 46 16 38 Z" fill="#FFA5A5" stroke="#4A2F25" stroke-width="2.5" stroke-linejoin="round"/>'+
    '<path d="M 88 36 Q 98 34 92 46 Q 84 46 84 38 Z" fill="#FFA5A5" stroke="#4A2F25" stroke-width="2.5" stroke-linejoin="round"/>'+
    '<path d="M 28 24 Q 14 16 18 34 Q 24 34 30 28 Z" fill="url(#mG)" stroke="#4A2F25" stroke-width="2.5" stroke-linejoin="round"/>'+
    '<path d="M 72 24 Q 86 16 82 34 Q 76 34 70 28 Z" fill="url(#mG)" stroke="#4A2F25" stroke-width="2.5" stroke-linejoin="round"/>'+
    '<ellipse cx="50" cy="55" rx="36" ry="32" fill="#FFEEDB" stroke="#4A2F25" stroke-width="3"/>'+
    '<ellipse cx="23" cy="62" rx="7" ry="5" fill="#FF7B93" opacity="0.65"/><ellipse cx="77" cy="62" rx="7" ry="5" fill="#FF7B93" opacity="0.65"/>'+
    '<circle cx="34" cy="48" r="8.5" fill="#3D2B1F"/><circle cx="32" cy="44.5" r="3.2" fill="#FFF"/><circle cx="36.5" cy="51" r="1.5" fill="#FFF"/>'+
    '<circle cx="66" cy="48" r="8.5" fill="#3D2B1F"/><circle cx="64" cy="44.5" r="3.2" fill="#FFF"/><circle cx="68.5" cy="51" r="1.5" fill="#FFF"/>'+
    '<circle cx="34" cy="48" r="12" fill="none" stroke="#52B788" stroke-width="3"/><circle cx="66" cy="48" r="12" fill="none" stroke="#52B788" stroke-width="3"/><line x1="46" y1="48" x2="54" y2="48" stroke="#52B788" stroke-width="3"/>'+
    '<ellipse cx="50" cy="69" rx="20" ry="12" fill="#FFD2D8" stroke="#4A2F25" stroke-width="2.5"/>'+
    '<ellipse cx="44" cy="67" rx="2.5" ry="1.8" fill="#4A2F25"/><ellipse cx="56" cy="67" rx="2.5" ry="1.8" fill="#4A2F25"/>'+
    '<path d="M 46 73 C 46 80, 54 80, 54 73" fill="none" stroke="url(#mG)" stroke-width="3.5" stroke-linecap="round"/>'+
    '<defs><linearGradient id="mG" x1="0%" y1="0%" x2="100%" y2="100%"><stop offset="0%" stop-color="#FFF280"/><stop offset="50%" stop-color="#FFC93C"/><stop offset="100%" stop-color="#D89200"/></linearGradient></defs></svg>';
  var BEAR='<svg viewBox="0 0 100 100" style="width:100%;height:100%">'+
    '<ellipse cx="50" cy="88" rx="28" ry="5" fill="#E6DFD5" opacity="0.6"/>'+
    '<path d="M 18 38 C 12 26, 28 18, 34 32" fill="#B07D62" stroke="#4A3525" stroke-width="2.5" stroke-linecap="round"/><path d="M 21 36 C 16 28, 26 24, 29 32" fill="#FFCCD3"/>'+
    '<path d="M 82 38 C 88 26, 72 18, 66 32" fill="#B07D62" stroke="#4A3525" stroke-width="2.5" stroke-linecap="round"/><path d="M 79 36 C 84 28, 74 24, 71 32" fill="#FFCCD3"/>'+
    '<ellipse cx="50" cy="56" rx="36" ry="30" fill="#DDB892" stroke="#4A3525" stroke-width="2.5"/>'+
    '<ellipse cx="23" cy="62" rx="6" ry="4" fill="#FF8C9F" opacity="0.6"/><ellipse cx="77" cy="62" rx="6" ry="4" fill="#FF8C9F" opacity="0.6"/>'+
    '<circle cx="35" cy="48" r="8.2" fill="#4A3525"/><circle cx="33" cy="45" r="3" fill="#FFF"/><circle cx="37" cy="51" r="1.2" fill="#FFF"/>'+
    '<circle cx="65" cy="48" r="8.2" fill="#4A3525"/><circle cx="63" cy="45" r="3" fill="#FFF"/><circle cx="67" cy="51" r="1.2" fill="#FFF"/>'+
    '<circle cx="35" cy="48" r="11" fill="none" stroke="#D08C60" stroke-width="2"/><circle cx="65" cy="48" r="11" fill="none" stroke="#D08C60" stroke-width="2"/><line x1="46" y1="48" x2="54" y2="48" stroke="#D08C60" stroke-width="2"/>'+
    '<ellipse cx="50" cy="65" rx="14" ry="9" fill="#FFF2E6" stroke="#4A3525" stroke-width="2"/>'+
    '<path d="M 45 61 Q 50 57, 55 61 Q 50 64, 45 61 Z" fill="#4A3525"/>'+
    '<path d="M 50 63 Q 50 67, 47 68 M 50 63 Q 50 67, 53 68" fill="none" stroke="#4A3525" stroke-width="1.8" stroke-linecap="round"/>'+
    '<path d="M 22 74 C 22 74, 50 86, 78 74 C 82 68, 78 66, 78 66 C 78 66, 50 76, 22 66 C 22 66, 18 68, 22 74 Z" fill="#4A7C59" stroke="#4A3525" stroke-width="2"/>'+
    '<path d="M 64 73 C 64 85, 72 92, 72 92 L 79 88 C 79 88, 73 80, 73 72" fill="#385F43" stroke="#4A3525" stroke-width="2" stroke-linejoin="round"/></svg>';
  /* 5 档行情状态 × 多条台词轮换：强度分驱动，行情好转/恶化时牛熊和台词都跟着换 */
  var ST={
    bull:{svg:BULL,tag:'🚀 牛市突围 · 情绪拉满',cls:'mas-bull',msgs:[
      '多头气势如虹！牛牛已启动高频扫单与超额获利模式，盯紧封板强度别恋战~ 🐂',
      '趋势全面走多、量价齐升！牛牛建议聚焦主线龙头，强者恒强但记得带好止盈 🎯',
      '情绪高潮期，连板梯队完整。牛牛提醒：鱼吃身段，最后一段留给别人 🐟']},
    bull2:{svg:BULL,tag:'🌞 震荡偏多 · 顺势而为',cls:'mas-bull',msgs:[
      '行情回暖，赚钱效应扩散中。牛牛建议顺势低吸强势股，不追高、不重仓单票 🌱',
      '多头略占上风，板块轮动加快。牛牛提示跟紧热点主线，弱势股反弹坚决换仓 🔄',
      '反弹窗口开启！牛牛把仓位系数上调一档：先锋打首板、稳健等回踩 📈']},
    neutral:{svg:BULL,tag:'🌤️ 震荡分歧 · 谨慎观望',cls:'mas-neutral',msgs:[
      '行情分歧、量能未定。牛牛建议轻仓试错、等待方向选择，别在中枢里追高杀跌',
      '多空拉锯，指数上下两难。牛牛提醒控制频率：少动多看，留够子弹等机会 🧊',
      '结构性行情，赚指数不赚钱。牛牛建议只做最强主线，杂毛票一律不碰 🎯']},
    bear2:{svg:BEAR,tag:'⛅ 震荡偏弱 · 控制仓位',cls:'mas-bear',msgs:[
      '行情承压但未失控，熊熊建议降低仓位、只留底仓观察，反弹是减仓的机会 ⚖️',
      '空头略占上风，亏钱效应蔓延。熊熊提醒：现金也是仓位，活下来才有下一波 💰',
      '弱势震荡别抄底摸顶，熊熊已收紧风控阈值：破位票坚决止损不犹豫 ✂️']},
    bear:{svg:BEAR,tag:'🛡️ 熊市避险 · 防御模式',cls:'mas-bear',msgs:[
      '熊熊观测到行情承压，防御因子全面亮起。缩减波动暴露、守护好钱袋子 🧣',
      '空头主导，普跌格局。熊熊建议空仓观望，等右侧信号确认再进场，别接飞刀 🔪',
      '至暗时刻更要守纪律：熊熊已锁紧开仓闸门。留得现金在，不怕没行情 🔒']}
  };
  function pick(){
    // 数值强度分5档优先（后端已含当日实时行情修正）
    var s=parseInt((document.getElementById('mktStrengthNumMon')||{}).textContent,10);
    if(!isNaN(s)){
      if(s>=70)return'bull';
      if(s>=55)return'bull2';
      if(s>=45)return'neutral';
      if(s>=30)return'bear2';
      return'bear';
    }
    var t=document.getElementById('mktTrendMon'),c=t?t.className:'';
    if(c.indexOf('mkt-bull')>=0)return'bull';
    if(c.indexOf('mkt-bear')>=0)return'bear';
    return'neutral';
  }
  function build(){
    var page=document.getElementById('page-monitor');
    if(!page||document.getElementById('masBanner'))return!!document.getElementById('masBanner');
    var mkt=document.getElementById('mktBannerMon');
    var el=document.createElement('div');el.id='masBanner';el.className='mas-banner';
    el.innerHTML='<div class="mas-avatar" id="masAvatar"></div><div class="mas-body">'+
      '<div class="mas-tags"><span class="mas-pill" id="masPill"></span>'+
      '<span class="mas-sub">见微 AI 实时风控核心已绑定 · 强度 <b id="masStrength">—</b>/100</span></div>'+
      '<p class="mas-msg" id="masMsg"></p></div>';
    // 把行情板块嵌到熊右边空位（复用现有 #mktBannerMon 的实时数据/刷新按钮）
    if(mkt&&mkt.parentNode===page){ page.insertBefore(el,mkt); el.appendChild(mkt); }
    else { page.insertBefore(el,page.firstChild); }
    return true;
  }
  var last='';
  function update(){
    var b=document.getElementById('masBanner');if(!b)return;
    var k=pick(),st=ST[k];
    var s=(document.getElementById('mktStrengthNumMon')||{}).textContent||'—';
    var a=(document.getElementById('mktAdviceMon')||{}).textContent||'';
    // 台词轮换：同一状态每 5 分钟换一条，状态切换立即换
    var idx=Math.floor(Date.now()/300000)%st.msgs.length;
    var msg=st.msgs[idx];
    var sig=k+'|'+s+'|'+a+'|'+idx;if(sig===last)return;last=sig;
    b.className='mas-banner '+st.cls;
    document.getElementById('masAvatar').innerHTML=st.svg;
    document.getElementById('masPill').textContent=st.tag;
    document.getElementById('masStrength').textContent=s;
    document.getElementById('masMsg').textContent=a?(msg+'　｜　'+a):msg;
  }
  function init(){
    if(!build()){var n=0,t=setInterval(function(){if(build()||++n>20){clearInterval(t);update();}},300);}
    update();
    var tr=document.getElementById('mktTrendMon');
    if(tr&&window.MutationObserver)new MutationObserver(update).observe(tr,{attributes:true,childList:true,characterData:true,subtree:true});
    setInterval(update,15000);
  }
  if(document.readyState!=='loading')init();else document.addEventListener('DOMContentLoaded',init);
})();
