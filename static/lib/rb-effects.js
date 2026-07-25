/* ════════════════════════════════════════════════════════════════
   rb-effects.js — 受 react-bits 启发的轻量交互层（vanilla JS）
   ────────────────────────────────────────────────────────────────
   设计原则：
   1. 自动绑定：DOMContentLoaded 后扫描已有 DOM 自动挂载
   2. MutationObserver：动态新增的元素也能享受效果
   3. 零依赖、零侵入：不污染全局，只暴露 window.RBEffects 一个对象
   4. 可选开关：window.RB_DISABLE = true 即关闭全部效果
   ════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";
  if (window.RB_DISABLE) return;

  // ============================================================
  // 0. 自注入最小 CSS（无需依赖 rb-effects.css 也能用 ripple/flash）
  //    rb-effects.css 已加载时这部分被覆盖，行为完全一致
  // ============================================================
  const hasBundledEffectsCss = !!document.querySelector('link[href*="rb-effects.css"]');
  if (!hasBundledEffectsCss && !document.getElementById("rb-effects-min-style")) {
    const style = document.createElement("style");
    style.id = "rb-effects-min-style";
    style.textContent = `
      /* 确保 .btn 是绝对定位的锚点，不影响原布局 */
      .btn{position:relative;overflow:hidden}
      .rb-ripple{position:absolute;border-radius:50%;background:rgba(255,255,255,.35);
        transform:scale(0);animation:rb-ripple-pop .55s ease-out forwards;pointer-events:none}
      @keyframes rb-ripple-pop{to{transform:scale(4);opacity:0}}
      .rb-flash-up{animation:rb-flash-up .65s ease}
      .rb-flash-down{animation:rb-flash-down .65s ease}
      @keyframes rb-flash-up{0%{background-color:rgba(248,81,73,.30)}100%{background-color:transparent}}
      @keyframes rb-flash-down{0%{background-color:rgba(63,185,80,.30)}100%{background-color:transparent}}
    `;
    document.head.appendChild(style);
  }

  // ============================================================
  // 1. 按钮波纹 Ripple
  // ============================================================
  function attachRipple(btn) {
    if (btn.__rbRipple) return;
    btn.__rbRipple = true;
    btn.addEventListener("click", function (e) {
      if (btn.disabled) return;
      const rect = btn.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height);
      const x = (e.clientX - rect.left) - size / 2;
      const y = (e.clientY - rect.top)  - size / 2;
      const ripple = document.createElement("span");
      ripple.className = "rb-ripple";
      ripple.style.width = ripple.style.height = size + "px";
      ripple.style.left = x + "px";
      ripple.style.top  = y + "px";
      btn.appendChild(ripple);
      setTimeout(() => ripple.remove(), 600);
    }, { passive: true });
  }

  function scanRipples(root) {
    (root || document).querySelectorAll(".btn").forEach(attachRipple);
  }


  // ============================================================
  // 2. 数字变化 Flash 与 Count-Up
  // 用法：给元素加 data-rb-tick，文本变化时自动 flash + 数字平滑过渡
  // ============================================================
  function parseNum(s) {
    if (typeof s === "number") return s;
    const cleaned = String(s).replace(/[,¥%\s]/g, "").trim();
    const n = parseFloat(cleaned);
    return isFinite(n) ? n : NaN;
  }

  function formatLike(template, n) {
    // 简单策略：保留模板里的非数字前缀/后缀（¥ % 等）
    const s = String(template);
    const prefix = (s.match(/^[^\d.-]*/) || [""])[0];
    const suffix = (s.match(/[^\d.-]*$/) || [""])[0];
    const decimals = (s.split(".")[1] || "").replace(/[^\d]/g, "").length;
    return prefix + n.toFixed(decimals) + suffix;
  }

  function countUp(el, from, to, ms) {
    const start = performance.now();
    const template = el.textContent;
    function frame(t) {
      const p = Math.min(1, (t - start) / ms);
      const eased = 1 - Math.pow(1 - p, 3);  // easeOutCubic
      const cur = from + (to - from) * eased;
      el.textContent = formatLike(template, cur);
      if (p < 1) requestAnimationFrame(frame);
      else el.textContent = formatLike(template, to);
    }
    requestAnimationFrame(frame);
  }

  function flash(el, isUp) {
    el.classList.remove("rb-flash-up", "rb-flash-down");
    void el.offsetWidth;   // 强制重排，重置动画
    el.classList.add(isUp ? "rb-flash-up" : "rb-flash-down");
  }

  function attachTick(el) {
    if (el.__rbTick) return;
    el.__rbTick = true;
    el.__rbLast = parseNum(el.textContent);
  }

  const tickObserver = new MutationObserver((mutations) => {
    for (const m of mutations) {
      const el = m.target.nodeType === 1 ? m.target : m.target.parentElement;
      if (!el || !el.hasAttribute || !el.hasAttribute("data-rb-tick")) continue;
      const newV = parseNum(el.textContent);
      const oldV = el.__rbLast;
      if (!isFinite(newV) || !isFinite(oldV)) {
        el.__rbLast = newV;
        continue;
      }
      if (Math.abs(newV - oldV) < 1e-6) continue;
      flash(el, newV > oldV);
      // Count-up 仅在差值显著时启用，避免抖动
      if (Math.abs(newV - oldV) > Math.max(0.01, Math.abs(newV) * 0.001)) {
        countUp(el, oldV, newV, 420);
      }
      el.__rbLast = newV;
    }
  });

  function scanTicks(root) {
    (root || document).querySelectorAll("[data-rb-tick]").forEach(el => {
      attachTick(el);
      tickObserver.observe(el, { characterData: true, childList: true, subtree: true });
    });
  }


  // ============================================================
  // 3. 卡片轻倾斜 (react-bits / Tilt Card) — 仅在大屏 + 鼠标设备启用
  // ============================================================
  const isCoarse = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;

  function attachTilt(card) {
    if (card.__rbTilt || isCoarse) return;
    card.__rbTilt = true;
    let rafId = 0;
    card.addEventListener("mousemove", (e) => {
      if (rafId) return;
      rafId = requestAnimationFrame(() => {
        rafId = 0;
        const r = card.getBoundingClientRect();
        const x = (e.clientX - r.left) / r.width  - 0.5;
        const y = (e.clientY - r.top)  / r.height - 0.5;
        card.style.transform = `perspective(900px) rotateY(${x * 1.2}deg) rotateX(${-y * 1.2}deg)`;
      });
    });
    card.addEventListener("mouseleave", () => {
      card.style.transform = "";
    });
  }

  function scanTilts(root) {
    // 默认仅给登录卡片这种突出的元素加 tilt，避免数据面板眩晕
    (root || document).querySelectorAll(".auth-card, .rb-tilt").forEach(attachTilt);
  }


  // ============================================================
  // 4. 全局 MutationObserver — 动态新增的 .btn / [data-rb-tick] 也自动绑定
  // ============================================================
  const globalMO = new MutationObserver((mutations) => {
    for (const m of mutations) {
      m.addedNodes.forEach((n) => {
        if (n.nodeType !== 1) return;
        if (n.matches && n.matches(".btn")) attachRipple(n);
        if (n.matches && n.matches("[data-rb-tick]")) {
          attachTick(n);
          tickObserver.observe(n, { characterData: true, childList: true, subtree: true });
        }
        if (n.querySelectorAll) {
          n.querySelectorAll(".btn").forEach(attachRipple);
          n.querySelectorAll("[data-rb-tick]").forEach(el => {
            attachTick(el);
            tickObserver.observe(el, { characterData: true, childList: true, subtree: true });
          });
        }
      });
    }
  });


  // ============================================================
  // 启动
  // ============================================================
  function init() {
    scanRipples();
    scanTicks();
    scanTilts();
    globalMO.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }


  // ============================================================
  // 暴露 API
  // ============================================================
  window.RBEffects = {
    attachRipple,
    attachTick,
    attachTilt,
    flash,
    countUp,
    rescan: () => { scanRipples(); scanTicks(); scanTilts(); },
    version: "1.0.0",
  };
})();
