/* 页面降噪 · 第 4 批验收（真机 CDP）：#/wealth + 用户菜单（#/appearance 入口）
   用法：XB_TOKEN=<token> node scripts/_e2e_wealth_declutter.js
   日志：_reports/_e2e_wealth_declutter.log */
const H = require('./_e2e_lib.js');
const fs = require('fs');

const TOKEN = process.env.XB_TOKEN || '';
const LOG = '_reports/_e2e_wealth_declutter.log';
function log(...a) { const s = a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' '); try { fs.appendFileSync(LOG, s + '\n'); } catch (e) {} }
const VISFN = 'e=>(e.offsetParent||e.getClientRects().length)';
const MODAL_JS = `(() => {
  const el=[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).pop();
  if(!el) return JSON.stringify({err:'no modal'});
  return JSON.stringify({ title:(el.innerText||'').trim().slice(0,90),
    fields:[...el.querySelectorAll('input,select')].map(e=>e.name||e.type) });
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
    await h.eval("localStorage.removeItem('xb:u:user1:wealth_tab')");   // 验默认落「我的持仓」
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.nav('/wealth');
    await h.waitForText('我的持仓', 12000);

    R.blocks = JSON.parse(await h.eval(`(() => {
      const shell = document.querySelector('.overflow-y-auto.space-y-5');
      if (!shell) return JSON.stringify({err:'no shell'});
      const kids=[...shell.children].filter(${VISFN}).map(e=>{const r=e.getBoundingClientRect();
        return {top:Math.round(r.top),h:Math.round(r.height),head:(e.innerText||'').trim().slice(0,20).replace(/\\n/g,'|')};});
      return JSON.stringify({innerHeight:innerHeight,kids});
    })()`));
    R.goldBtns = JSON.parse(await h.eval(`(() => JSON.stringify([...document.querySelectorAll('button')].filter(${VISFN}).filter(e=>/bg-brand-600/.test(e.className)).map(e=>(e.innerText||'').trim().slice(0,16))))()`));
    R.inputs = JSON.parse(await h.eval(`(() => JSON.stringify([...document.querySelectorAll('input,select')].filter(${VISFN}).map(e=>e.name||e.type||e.tagName)))()`));
    R.tabs = JSON.parse(await h.eval(`(() => JSON.stringify([...document.querySelectorAll('button')].filter(${VISFN}).filter(e=>/^在售产品|^我的持仓|^已赎回/.test((e.innerText||'').trim())).map(e=>e.innerText.trim())))()`));
    R.hasTinyBanner = await h.eval(`(()=>document.querySelectorAll('[class*="text-[9px]"]').length)()`);
    await h.screenshot('_reports/_e2e_wealth_1_holding.png');
    log('LIVE=' + JSON.stringify(R));

    /* 持仓行详情 */
    log('clickDetail=' + await h.clickByText('详情', { tag: 'button', visibleOnly: true }));
    await H.sleep(600);
    const dt = await h.bodyText();
    R.detailOpen = dt.includes('买入日') && dt.includes('期限') && dt.includes('年化收益率');
    await h.screenshot('_reports/_e2e_wealth_2_detail.png');

    /* 在售产品 Tab：无小字横幅 + 有 R3 徽标 */
    await h.clickByText('在售产品', { tag: 'button', visibleOnly: true });
    await H.sleep(900);
    R.productTab = JSON.parse(await h.eval(`(() => {
      const t=(document.body.innerText||'');
      return JSON.stringify({ hasR3: t.includes('R3'), hasLegend: t.includes('风险等级说明'), tinyBanners: document.querySelectorAll('[class*="text-[9px]"]').length });
    })()`));
    await h.screenshot('_reports/_e2e_wealth_3_product.png');

    /* 购买弹窗 */
    log('clickBuy=' + await h.clickByText('立即购买', { tag: 'button', visibleOnly: true }));
    await H.sleep(900);
    R.buyModal = JSON.parse(await h.eval(MODAL_JS));
    await h.screenshot('_reports/_e2e_wealth_4_buy.png');
    await h.eval(`(()=>{const el=[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).pop();if(el)el.dispatchEvent(new MouseEvent('click',{bubbles:true}));})()`);
    await H.sleep(600);

    /* 已赎回 Tab */
    await h.clickByText('已赎回', { tag: 'button', visibleOnly: true });
    await H.sleep(900);
    await h.screenshot('_reports/_e2e_wealth_5_redeemed.png');

    /* 用户菜单：形象入口应已消失，数字柜员开关仍在 */
    R.menuOpen = await h.eval(`(() => {const b=[...document.querySelectorAll('button')].find(x=>/pl-5/.test(x.className)&&/items-center gap-3/.test(x.className));if(!b)return 'no-btn';b.click();return 'ok';})()`);
    await H.sleep(700);
    const mt = await h.bodyText();
    R.menuHasDhToggle = mt.includes('数字柜员');
    R.menuAppearanceGone = !mt.includes('设置数字人形象');
    await h.screenshot('_reports/_e2e_wealth_6_menu.png');

    log('DETAIL_OPEN=' + R.detailOpen + ' PRODUCT=' + JSON.stringify(R.productTab) + ' TINY_BANNER=' + R.hasTinyBanner);
    log('BUY=' + JSON.stringify(R.buyModal));
    log('MENU dh_toggle=' + R.menuHasDhToggle + ' appearance_gone=' + R.menuAppearanceGone + ' open=' + R.menuOpen);
    log('FINAL=' + JSON.stringify({ gold: R.goldBtns, inputs: R.inputs, tab: R.tabs, blocks: R.blocks.kids }, null, 1));
    await h.close();
    process.exit(0);
  } catch (e) {
    try { fs.appendFileSync(LOG, 'ERR ' + (e && e.stack || e) + '\n'); } catch (_) {}
    process.exit(1);
  }
})();
