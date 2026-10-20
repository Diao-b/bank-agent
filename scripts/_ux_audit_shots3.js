/* 全站 UX 评审 · 取证脚本 3：★固定 1440 视口的分段截图（修正前两次视口不一致问题）
 * 用法：node scripts/_ux_audit_shots3.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;

const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_run3.log'));

const scrollMain = (y) => `(() => {
  const c = [...document.querySelectorAll('*')].filter(e => {
    const cs = getComputedStyle(e);
    return /(auto|scroll)/.test(cs.overflowY) && e.scrollHeight - e.clientHeight > 40;
  }).sort((a,b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
  if (!c) return 'none';
  c.scrollTop = ${y};
  return 'h=' + c.scrollHeight + ' top=' + c.scrollTop;
})()`;

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  try {
    await h.goto(BASE + '/login');
    await h.eval(`localStorage.clear()`);
    await h.setViewport(1440, 920);
    await h.goto(BASE + '/login');
    await h.type('input[placeholder="请输入用户名"]', 'user1');
    await h.type('input[placeholder="请输入密码"]', '123456');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3600);
    log('NAV', await h.eval('location.href'));
    log('VIEWPORT', await h.eval(`JSON.stringify({w:innerWidth,h:innerHeight,dpr:devicePixelRatio})`));

    for (const [name, hash, ys] of [
      ['subs', '#/subs', [0, 500, 1000, 1500]],
      ['cards', '#/cards', [0, 450]],
      ['wealth', '#/wealth', [0, 500, 1000, 1500]],
      ['safe', '#/safe', [0, 600, 1200]],
      ['bills', '#/bills', [0, 600]],
      ['transfer', '#/transfer', [0]],
      ['appearance', '#/appearance', [0]],
    ]) {
      await h.eval(`location.hash = ${JSON.stringify(hash)}`);
      await sleep(2600);
      for (const y of ys) {
        const r = await h.eval(scrollMain(y));
        await sleep(800);
        await h.screenshot(path.join(OUT, `${name}_w${y}.png`));
        log('SEG', `${name}@${y} ${r}`);
      }
    }

    /* 取消订阅弹窗（1440） */
    await h.eval(`location.hash = '#/subs'`);
    await sleep(2500);
    await h.eval(scrollMain(900));
    await sleep(800);
    await h.screenshot(path.join(OUT, 'subs_scrolled_1440.png'));
    await h.clickByText('取消', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(1500);
    await h.screenshot(path.join(OUT, 'subs_modal_1440.png'));
    log('MODAL', 'subs cancel 1440 已截');

    /* 签约代扣：真点一次看是否有二次确认（在 user1 上只观察按钮属性，不提交） */
    const sign = await h.eval(`(()=>{
      const b=[...document.querySelectorAll('button')].find(x=>/签约代扣/.test(x.innerText||''));
      if(!b) return 'none';
      const cs=getComputedStyle(b);
      return JSON.stringify({text:b.innerText.trim(),color:cs.color,bg:cs.backgroundColor,fs:cs.fontSize,
        confirm: b.getAttribute('data-confirm')||null, type: b.type||''});
    })()`);
    log('BTN', '签约代扣按钮 ' + sign);

    /* 直接购买/赎回按钮样式（理财） */
    await h.eval(`location.hash = '#/wealth'`);
    await sleep(2600);
    const wb = await h.eval(`(()=>{const bs=[...document.querySelectorAll('button')].filter(b=>/购买|赎回|买入|卖出|详情/.test(b.innerText||'')).slice(0,10).map(b=>{const cs=getComputedStyle(b);return {t:b.innerText.trim().slice(0,10),color:cs.color,bg:cs.backgroundColor,fs:cs.fontSize}});return JSON.stringify(bs)})()`);
    log('BTN', 'wealth 操作按钮 ' + wb);

    /* 账单页异常提醒三态（看是否 error 也渲染绿字） */
    const billsTxt = await h.eval(`location.hash='#/bills'`) && (await (async () => { await sleep(2400); return (await h.bodyText()) || ''; })());
    fs.writeFileSync(path.join(OUT, 'bills_w_text.txt'), billsTxt);
    log('TXT', 'bills 含「未发现异常支出」=' + /未发现异常支出/.test(billsTxt) + ' 含「上月支出」=' + /上月支出/.test(billsTxt));

    /* 安全中心：操作日志是否裸露 JSON */
    await h.eval(`location.hash = '#/safe'`);
    await sleep(2600);
    await h.eval(scrollMain(1400));
    await sleep(800);
    await h.screenshot(path.join(OUT, 'safe_logs_1440.png'));
    const safeTxt = (await h.bodyText()) || '';
    fs.writeFileSync(path.join(OUT, 'safe_w_text.txt'), safeTxt);
    log('TXT', 'safe 含「{」=' + /\{/.test(safeTxt) + ' 含 action 英文=' + /freeze_card|unfreeze_card|card_delete|subscription_deduct/.test(safeTxt));

    /* 管理端 1440 */
    await h.goto(BASE + '/admin/login');
    await h.type('input[placeholder="如：admin"]', 'admin');
    await h.type('input[type="password"]', 'admin123');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(4000);
    await h.screenshot(path.join(OUT, 'admin_w1440.png'));
    fs.writeFileSync(path.join(OUT, 'admin_w1440.txt'), (await h.bodyText()) || '');
    log('NAV', 'admin ' + (await h.eval('location.href')));

    log('DONE', 'ok3');
  } catch (e) {
    log('EXCEPTION', String((e && e.message) || e));
  } finally {
    await h.close().catch(() => {});
  }
  process.exit(0);
})();
