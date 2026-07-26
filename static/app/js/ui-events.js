;(function(){
  // ══════════════════════════════════════════════════════════════════
  //  data-ui-* 事件委托层（无 eval 版）
  //  ------------------------------------------------------------------
  //  历史：原实现用 `new Function('event', 'with(window){...}')` 执行属性里的
  //        JS 源码，因此 CSP 必须放行 'unsafe-eval'。一旦 CSP 收紧，全站点击层
  //        会静默失效（登录/切页/买卖全点不动）——这曾是真实事故。
  //  现在：两级安全求值，**完全不使用 eval / new Function**：
  //    ① 安全调用解析器：处理 `fn(...)`、`a.b.c(...)`，参数只允许
  //       字符串/数字/布尔/null/this/event/全局标识符，多语句用 ; 分隔。
  //       覆盖绝大多数处理器（含运行时插值后的 `sellPos('600519',12.5)`）。
  //    ② 静态映射表 ui-handlers.gen.js：把 HTML 里那些带 if/赋值等复杂语法的
  //       固定字符串，预先编译成真实函数（由 tools/gen_ui_handlers.py 生成）。
  //  两者都不命中 → console.error 显式报错，绝不静默吞掉。
  // ══════════════════════════════════════════════════════════════════
  const EVENT_ATTRS = ['click','change','input','keydown','keyup','blur','focus','submit','contextmenu']

  // ── ① 安全调用解析器 ────────────────────────────────────────────
  function splitTop(s, sep){
    const out = []; let depth = 0, q = null, cur = ''
    for(let i = 0; i < s.length; i++){
      const c = s[i]
      if(q){
        cur += c
        if(c === '\\' && i + 1 < s.length){ cur += s[++i]; continue }
        if(c === q) q = null
        continue
      }
      if(c === '"' || c === "'"){ q = c; cur += c; continue }
      if(c === '(' || c === '[' || c === '{') depth++
      else if(c === ')' || c === ']' || c === '}') depth--
      if(c === sep && depth === 0){ out.push(cur); cur = ''; continue }
      cur += c
    }
    out.push(cur)
    return out
  }

  const STR_RE = /^(["'])((?:[^\\]|\\.)*?)\1$/s
  const NUM_RE = /^-?\d+(?:\.\d+)?$/
  const IDENT_RE = /^[A-Za-z_$][\w$]*$/

  function unquote(str){
    // 只还原常见转义，不做任何代码求值
    return str.replace(/\\(['"\\nrt])/g, (m, c) =>
      c === 'n' ? '\n' : c === 'r' ? '\r' : c === 't' ? '\t' : c)
  }

  function evalArg(raw, el, event){
    const s = raw.trim()
    const m = STR_RE.exec(s)
    if(m) return unquote(m[2])
    if(NUM_RE.test(s)) return parseFloat(s)
    if(s === 'true') return true
    if(s === 'false') return false
    if(s === 'null') return null
    if(s === 'undefined') return undefined
    if(s === 'this') return el
    if(s === 'event') return event
    if(IDENT_RE.test(s)) return window[s]     // 只读全局变量，不执行任何代码
    throw new Error('unsupported arg: ' + s)
  }

  function resolvePath(path){
    const parts = path.split('.')
    let ctx = window, fn = window[parts[0]]
    for(let i = 1; i < parts.length; i++){
      if(fn == null) return null
      ctx = fn
      fn = fn[parts[i]]
    }
    return typeof fn === 'function' ? {fn, ctx} : null
  }

  const CALL_RE = /^\s*([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(([\s\S]*)\)\s*$/

  function runSafeCalls(code, el, event){
    const stmts = splitTop(code, ';').map(s => s.trim()).filter(Boolean)
    if(!stmts.length) return false
    let last
    for(const st of stmts){
      const m = CALL_RE.exec(st)
      if(!m) return false
      const target = resolvePath(m[1])
      if(!target) return false
      const argsRaw = m[2].trim()
      let args = []
      if(argsRaw){
        try{
          args = splitTop(argsRaw, ',').map(a => evalArg(a, el, event))
        }catch(e){ return false }
      }
      last = target.fn.apply(target.ctx, args)
    }
    return {value: last}
  }

  // ── 派发 ─────────────────────────────────────────────────────────
  function runHandler(el, event, code){
    if(!code) return false
    try{
      const r = runSafeCalls(code, el, event)          // ① 安全解析
      if(r) return r.value
      const map = window.__UI_HANDLERS                  // ② 静态映射表
      const fn = map && map[code]
      if(typeof fn === 'function') return fn.call(el, event)
      console.error('[ui-events] 无法处理的处理器（既非安全调用、也不在静态映射表中）：', code, el)
      return false
    }catch(err){
      console.error('[ui-events] handler failed:', err, {element: el, event, code})
      return false
    }
  }

  function findTarget(event, attr){
    const selector = `[data-ui-${attr}]`
    return event.target && event.target.closest ? event.target.closest(selector) : null
  }

  function dispatch(attr){
    document.addEventListener(attr, event => {
      const el = findTarget(event, attr)
      if(!el) return
      const code = el.getAttribute(`data-ui-${attr}`)
      if(code == null) return
      if(attr === 'submit' || attr === 'click') event.preventDefault()
      const result = runHandler(el, event, code)
      if(result !== false){
        event.stopPropagation()
      }
    }, true)
  }

  EVENT_ATTRS.forEach(dispatch)

  // ── 键盘桥：让 tabindex 化的 div 按钮（侧栏页签/退出/管理后台等）响应 Enter / 空格 ──
  //    这些不是原生 <button>，浏览器不会自动把按键合成 click；原生控件不在此列，
  //    避免重复触发（它们自己就处理 Enter/Space）。
  const NATIVE = {BUTTON:1, A:1, INPUT:1, TEXTAREA:1, SELECT:1}
  document.addEventListener('keydown', event => {
    if(event.key !== 'Enter' && event.key !== ' ' && event.key !== 'Spacebar') return
    const el = event.target
    if(!el || !el.matches) return
    if(NATIVE[el.tagName]) return
    if(!el.matches('[tabindex]')) return
    if(!el.hasAttribute('data-ui-click') && !el.hasAttribute('data-action')) return
    event.preventDefault()      // 空格默认滚动页面
    el.click()                  // 合成真实 click，交由上面的委托统一处理
  })
})()
