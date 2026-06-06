// Vite 模块入口。业务逻辑仍在 public/app/js 的经典脚本里以保功能对等；
// 这里挂载糖果主题的新增 ES 模块（吉祥物等），逐步把经典脚本迁成模块。
import { initMascot } from './mascot.js'
console.info('%c见微 Alpha %c糖果主题 · Vite build', 'color:#FF7B93;font-weight:700', 'color:#9C8B7A')

function _ready(fn){ if(document.readyState!=='loading') fn(); else document.addEventListener('DOMContentLoaded', fn) }
_ready(() => { try { initMascot() } catch(e){ console.warn('mascot init', e) } })
