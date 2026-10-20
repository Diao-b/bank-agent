/* 验证「数字柜员」浮层（应用内真实路径）：
 *   用户菜单点「数字柜员 开」→ iframe 加载 5173 → Fay 侧 digital_human 变 true
 * 只读 + 截图；唯一的写是用户自己的界面开关（正是用户想要的结果：打开数字人）。
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const LOG = '_reports/_e2e_dh_panel.log';
const log = (...a) => { try { fs.appendFileSync(LOG, a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {} };

const VISFN = "e=>!!(e.offsetParent||e.getClientRects().length)";
async function status() {
  const r = await fetch('http://127.0.0.1:5000/api/get-system-status?username=User');
  return await r.json();
}

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1440, height: 900, watchdogMs: 150000 });
  try {
    await h.goto(H.BASE + '/');
    await h.eval("localStorage.clear(); localStorage.setItem('xb_user','user1')");
    await h.setToken(process.env.XB_TOKEN || '');
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.eval("localStorage.removeItem('xb:u:user1:digital_human')");
    await h.goto(H.BASE + '/chat', { reload: true });
    await H.sleep(1500);

    R_off = await h.eval(`JSON.stringify({ frames: [...document.querySelectorAll('iframe')].filter(${VISFN}).map(f=>f.getAttribute('src')||'').filter(s=>/5173/.test(s)) })`);
    log('BEFORE(开关关): iframes=' + R_off + '  fay=' + JSON.stringify(await status()));

    /* 1) 打开用户菜单（顶栏含圆形头像的那个按钮） */
    const openMenu = await h.eval(`(() => {
      const b = [...document.querySelectorAll('button')].filter(${VISFN}).find(x => x.querySelector('div.rounded-full'));
      if (!b) return 'no-trigger';
      b.click(); return 'ok';
    })()`);
    await H.sleep(600);
    log('OPEN_MENU=' + openMenu);

    /* 2) 点「数字柜员 开」 */
    const hit = await h.eval(`(() => {
      const b = [...document.querySelectorAll('button')].filter(${VISFN}).find(x => /^数字柜员/.test((x.innerText||'').trim()));
      if (!b) return 'not-found';
      const before = (b.innerText||'').trim();
      b.click();
      return before;
    })()`);
    await H.sleep(4000);
    log('CLICKED=' + hit);
    log('AFTER(开关开): fay=' + JSON.stringify(await status()));

    const dom = await h.eval(`JSON.stringify({
      frames: [...document.querySelectorAll('iframe')].filter(${VISFN}).map(f=>f.getAttribute('src')||'').filter(s=>/5173/.test(s)),
      panelText: (() => { const f=[...document.querySelectorAll('iframe')].filter(${VISFN}).find(x=>/5173/.test(x.getAttribute('src')||'')); if(!f) return null; let p=f.parentElement; for(let i=0;i<5&&p;i++){ const t=(p.innerText||'').trim(); if(t) return t.slice(0,80); p=p.parentElement;} return null; })(),
      stored: localStorage.getItem('xb:u:user1:digital_human')
    })`);
    log('DOM=' + dom);
    await h.screenshot('_reports/_e2e_dh_panel.png');
    log('SHOT=_reports/_e2e_dh_panel.png');
  } catch (e) {
    log('ERR ' + (e && e.stack || e));
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(0);
  }
})();
