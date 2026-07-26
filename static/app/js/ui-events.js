;(function(){
  const EVENT_ATTRS = ['click','change','input','keydown','keyup','blur','focus','submit','contextmenu']

  function runHandler(el, event, code){
    if(!code) return false
    try{
      const fn = new Function('event', `with(window){ ${code} }`)
      return fn.call(el, event)
    }catch(err){
      console.error('[ui-events] handler failed:', err, { element: el, event, code })
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
})()