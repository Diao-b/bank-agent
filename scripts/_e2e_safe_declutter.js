/* 页面降噪 · 第 3 批验收（真机 CDP）：#/safe 新结构
   用法：XB_TOKEN=<token> node scripts/_e2e_safe_declutter.js
   日志：_reports/_e2e_safe_declutter.log
   ★ fixed 弹层的可见性一律用 (offsetParent || getClientRects().length) */
const H = require('./_e2e_lib.js');
const fs = require('fs');

const TOKEN = process.env.XB_TOKEN || '';
const LOG = '_reports/_e2e_safe_declutter.log';
function log(...a) { const s = a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' '); try { fs.appendFileSync(LOG, s + '\n'); } catch (e) {} }

const VISFN = 'e=>(e.offsetParent||e.getClientRects().length)';
const MODAL_JS = `(() => {
  const el=[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).pop();
  if(!el) return JSON.stringify({err:'no modal'});
  return JSON.stringify({ title:(el.innerText||'').trim().slice(0,80),
    btns:[...el.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean) });
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
    await h.eval("localStorage.removeItem('xb:u:user1:safe_tab')");   // 验默认落「安全设置」
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.nav('/safe');
    await h.waitForText('安全设置', 12000);

    R.blocks = JSON.parse(await h.eval(`(() => {
      const shell = document.querySelector('.overflow-y-auto.space-y-5');
      if (!shell) return JSON.stringify({err:'no shell'});
      const kids=[...shell.children].filter(${VISFN}).map(e=>{const r=e.getBoundingClientRect();
        return {top:Math.round(r.top),h:Math.round(r.height),head:(e.innerText||'').trim().slice(0,20).replace(/\\n/g,'|')};});
      return JSON.stringify({innerHeight:innerHeight,kids});
    })()`));
    R.goldBtns = JSON.parse(await h.eval(`(() => JSON.stringify([...document.querySelectorAll('button')].filter(${VISFN}).filter(e=>/bg-brand-600/.test(e.className)).map(e=>(e.innerText||'').trim().slice(0,16))))()`));
    R.inputs = JSON.parse(await h.eval(`(() => JSON.stringify([...document.querySelectorAll('input,select')].filter(${VISFN}).map(e=>e.name||e.type||e.tagName)))()`));
    R.tabs = JSON.parse(await h.eval(`(() => JSON.stringify([...document.querySelectorAll('button')].filter(${VISFN}).filter(e=>/^安全设置|^操作记录/.test((e.innerText||'').trim())).map(e=>e.innerText.trim())))()`));
    R.sloganGone = !(await h.bodyText()).includes('每一次操作，都有迹可循');
    await h.screenshot('_reports/_e2e_safe_1_settings.png');
    log('LIVE=' + JSON.stringify(R));

    /* 风控规则帮助弹窗 */
    log('clickRules=' + await h.clickByText('查看风控规则', { tag: 'button', visibleOnly: true }));
    await H.sleep(800);
    R.rules = JSON.parse(await h.eval(MODAL_JS));
    await h.screenshot('_reports/_e2e_safe_2_rules.png');
    await h.eval(`(()=>{const el=[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).pop();if(el)el.dispatchEvent(new MouseEvent('click',{bubbles:true}));})()`);
    await H.sleep(600);

    /* 退出登录两步确认（点一次只 arm，不真退） */
    await h.clickByText('退出登录', { tag: 'button', visibleOnly: true });
    await H.sleep(500);
    R.logoutArmed = (await h.bodyText()).includes('确认退出？');
    await h.screenshot('_reports/_e2e_safe_3_logout_armed.png');

    /* 操作记录 Tab */
    await h.clickByText('操作记录', { tag: 'button', visibleOnly: true });
    await H.sleep(1000);
    const t = (await h.bodyText()).replace(/\n+/g, ' | ');
    R.logsTab = { hasOverview: /操作记录\s*\d+\s*条/.test(t), hasPwdLog: t.includes('支付密码核验日志'), hasTimeline: t.includes('操作日志') };
    await h.screenshot('_reports/_e2e_safe_4_logs.png');

    log('RULES=' + JSON.stringify(R.rules));
    log('LOGOUT_ARMED=' + R.logoutArmed + ' SLOGAN_GONE=' + R.sloganGone);
    log('LOGS_TAB=' + JSON.stringify(R.logsTab));
    log('FINAL=' + JSON.stringify({ gold: R.goldBtns, inputs: R.inputs, tab: R.tabs, blocks: R.blocks.kids }, null, 1));
    await h.close();
    process.exit(0);
  } catch (e) {
    try { fs.appendFileSync(LOG, 'ERR ' + (e && e.stack || e) + '\n'); } catch (_) {}
    process.exit(1);
  }
})();
