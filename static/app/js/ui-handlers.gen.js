// ⚠️ 本文件由 tools/gen_ui_handlers.py 自动生成，请勿手工编辑。
// 作用：把 data-ui-* 里那些含 if / 赋值 / 比较等复杂语法的固定处理器，
//      预编译成真实函数，使 ui-events.js 无需 eval（CSP 可去掉 'unsafe-eval'）。
// 函数体逐字复制自原属性，行为与改造前完全一致。
// 改动这类处理器后请重跑：python tools/gen_ui_handlers.py
;(function(){
  window.__UI_HANDLERS = {
    "cfCallback && cfCallback()": function(event){ cfCallback && cfCallback() },
    "chainSearch(document.getElementById('chainQ').value)": function(event){ chainSearch(document.getElementById('chainQ').value) },
    "document.getElementById('aic-model').value=this.value": function(event){ document.getElementById('aic-model').value=this.value },
    "document.getElementById('aitDecModal').classList.remove('show')": function(event){ document.getElementById('aitDecModal').classList.remove('show') },
    "document.getElementById('logBody').innerHTML='<div class=empty>已清空</div>'": function(event){ document.getElementById('logBody').innerHTML='<div class=empty>已清空</div>' },
    "document.getElementById('repQ').value='';loadReports()": function(event){ document.getElementById('repQ').value='';loadReports() },
    "document.getElementById('vibeMaxCodes').value=0": function(event){ document.getElementById('vibeMaxCodes').value=0 },
    "document.getElementById('vibeMaxCodes').value=100": function(event){ document.getElementById('vibeMaxCodes').value=100 },
    "document.getElementById('vibeMaxCodes').value=500": function(event){ document.getElementById('vibeMaxCodes').value=500 },
    "if(event.key==='Enter')aitAddWatch()": function(event){ if(event.key==='Enter')aitAddWatch() },
    "if(event.key==='Enter')aitDetectModels()": function(event){ if(event.key==='Enter')aitDetectModels() },
    "if(event.key==='Enter')chainSearch(this.value)": function(event){ if(event.key==='Enter')chainSearch(this.value) },
    "if(event.key==='Enter')doForgot()": function(event){ if(event.key==='Enter')doForgot() },
    "if(event.key==='Enter')doLogin()": function(event){ if(event.key==='Enter')doLogin() },
    "if(event.key==='Enter')doRegister()": function(event){ if(event.key==='Enter')doRegister() },
    "if(event.key==='Enter')doRename()": function(event){ if(event.key==='Enter')doRename() },
    "if(event.key==='Enter')doReset()": function(event){ if(event.key==='Enter')doReset() },
    "if(event.key==='Enter')doSearch()": function(event){ if(event.key==='Enter')doSearch() },
    "if(event.key==='Enter')loadNews()": function(event){ if(event.key==='Enter')loadNews() },
    "if(event.key==='Enter')loadReports()": function(event){ if(event.key==='Enter')loadReports() },
    "if(event.key==='Enter')loadUsers(1)": function(event){ if(event.key==='Enter')loadUsers(1) },
    "if(event.target===this)closeAiGen()": function(event){ if(event.target===this)closeAiGen() },
    "if(event.target===this)closeKL()": function(event){ if(event.target===this)closeKL() },
    "if(event.target===this)closeOpt()": function(event){ if(event.target===this)closeOpt() },
    "if(event.target===this)closePaperBuy()": function(event){ if(event.target===this)closePaperBuy() },
    "if(event.target===this)this.classList.remove('show')": function(event){ if(event.target===this)this.classList.remove('show') },
    "if(this.value)document.getElementById('aitCfgModel').value=this.value": function(event){ if(this.value)document.getElementById('aitCfgModel').value=this.value },
    "loadTransactions(txState.page)": function(event){ loadTransactions(txState.page) },
    "loadUsers(usersState.page)": function(event){ loadUsers(usersState.page) },
    "location.href='/'": function(event){ location.href='/' },
    "location.href='/admin'": function(event){ location.href='/admin' },
    "navigate(defaultPage())": function(event){ navigate(defaultPage()) },
    "switchPage('aitrader',document.querySelector('.ntab[data-page=\"aitrader\"]'));initAiTrader();setTimeout(aitOpenSettings,400)": function(event){ switchPage('aitrader',document.querySelector('.ntab[data-page="aitrader"]'));initAiTrader();setTimeout(aitOpenSettings,400) },
    "switchPage('execution',document.querySelector('.ntab[data-page=\"execution\"]'));refreshExecutionState()": function(event){ switchPage('execution',document.querySelector('.ntab[data-page="execution"]'));refreshExecutionState() },
    "switchPage('strategy',document.querySelector('.ntab[data-page=\"strategy\"]'))": function(event){ switchPage('strategy',document.querySelector('.ntab[data-page="strategy"]')) },
    "void 0": function(event){ void 0 },
  }
})()
