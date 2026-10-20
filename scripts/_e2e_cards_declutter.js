/* 页面降噪 · 第 2 批验收（真机 CDP）：#/cards 新结构
   用法：XB_TOKEN=<token> node scripts/_e2e_cards_declutter.js
   日志：_reports/_e2e_cards_declutter.log
   ★ fixed 定位元素的 offsetParent 恒为 null → 可见性一律用 (offsetParent || getClientRects().length) */
const H = require('./_e2e_lib.js');
const fs = require('fs');

const TOKEN = process.env.XB_TOKEN || '';
const LOG = '_reports/_e2e_cards_declutter.log';
function log(...a) { const s = a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' '); try { fs.appendFileSync(LOG, s + '\n'); } catch (e) {} }

const VISFN = 'e=>(e.offsetParent||e.getClientRects().length)';
const MODAL_JS = `(() => {
  const el=[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).pop();
  if(!el) return JSON.stringify({err:'no modal'});
  return JSON.stringify({
    title:(el.innerText||'').trim().slice(0,80),
    btns:[...el.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean),
    fields:[...el.querySelectorAll('input,select')].map(e=>e.name||e.type),
  });
})()`;

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1280, height: 800 });
  const R = {};
  try {
    log('innerWidth=' + await h.eval('innerWidth') + ' innerHeight=' + await h.eval('innerHeight'));
    await h.goto(H.BASE + '/');
    await h.clearToken();
    await h.eval("localStorage.setItem('xb_user','user1')");
    await h.setToken(TOKEN);
    await h.eval("localStorage.removeItem('xb:u:user1:cards_tab')");   // 验默认落「我的卡片」
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.nav('/cards');
    await h.waitForText('我的卡片', 12000);

    R.blocks = JSON.parse(await h.eval(`(() => {
      const shell = document.querySelector('.overflow-y-auto.space-y-5');
      if (!shell) return JSON.stringify({err:'no shell'});
      const kids=[...shell.children].filter(${VISFN}).map(e=>{const r=e.getBoundingClientRect();
        return {top:Math.round(r.top),h:Math.round(r.height),head:(e.innerText||'').trim().slice(0,20).replace(/\\n/g,'|')};});
      return JSON.stringify({innerHeight:innerHeight,kids});
    })()`));
    R.goldBtns = JSON.parse(await h.eval(`(() => JSON.stringify([...document.querySelectorAll('button')].filter(${VISFN}).filter(e=>/bg-brand-600/.test(e.className)).map(e=>(e.innerText||'').trim().slice(0,16))))()`));
    R.inputs = JSON.parse(await h.eval(`(() => JSON.stringify([...document.querySelectorAll('input,select')].filter(${VISFN}).map(e=>e.name||e.type||e.tagName)))()`));
    R.tabs = JSON.parse(await h.eval(`(() => JSON.stringify([...document.querySelectorAll('button')].filter(${VISFN}).filter(e=>/我的卡片|已注销/.test(e.innerText||'')).map(e=>e.innerText.trim())))()`));
    await h.screenshot('_reports/_e2e_cards_1_mine.png');
    log('LIVE=' + JSON.stringify(R));

    /* 详情抽屉 */
    log('clickDetail=' + await h.clickByText('详情', { tag: 'button', visibleOnly: true }));
    await H.sleep(800);
    R.detail = JSON.parse(await h.eval(MODAL_JS));
    await h.screenshot('_reports/_e2e_cards_2_detail.png');
    log('DETAIL=' + JSON.stringify(R.detail));

    /* 冻结 → 安全流程（gate）→ 取消（净副作用 0） */
    if (R.detail && R.detail.btns && R.detail.btns.includes('冻结卡片')) {
      await h.clickByText('冻结卡片', { tag: 'button', visibleOnly: true });
      await H.sleep(1100);
      R.freezeFlow = JSON.parse(await h.eval(MODAL_JS));
      await h.screenshot('_reports/_e2e_cards_3_freeze.png');
      await h.eval(`(()=>{const el=[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).pop();if(el){const b=[...el.querySelectorAll('button')].find(x=>/取消/.test(x.innerText||''));if(b)b.click();}})()`);
      await H.sleep(700);
      R.afterCancel = await h.eval(`(()=>[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).length)()`);
    }

    /* 申请新卡弹窗 */
    log('clickApply=' + await h.clickByText('+ 申请新卡', { tag: 'button', visibleOnly: true }));
    await H.sleep(900);
    R.apply = JSON.parse(await h.eval(MODAL_JS));
    await h.screenshot('_reports/_e2e_cards_4_apply.png');
    await h.eval(`(()=>{const el=[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).pop();if(el)el.dispatchEvent(new MouseEvent('click',{bubbles:true}));})()`);
    await H.sleep(700);

    /* 已注销 Tab */
    await h.clickByText('已注销', { tag: 'button', visibleOnly: true });
    await H.sleep(900);
    await h.screenshot('_reports/_e2e_cards_5_closed.png');

    log('APPLY=' + JSON.stringify(R.apply));
    log('FREEZE=' + JSON.stringify(R.freezeFlow) + ' afterCancel=' + R.afterCancel);
    log('FINAL=' + JSON.stringify({ gold: R.goldBtns, inputs: R.inputs, tab: R.tabs, blocks: R.blocks.kids }, null, 1));
    await h.close();
    process.exit(0);
  } catch (e) {
    try { fs.appendFileSync(LOG, 'ERR ' + (e && e.stack || e) + '\n'); } catch (_) {}
    process.exit(1);
  }
})();
