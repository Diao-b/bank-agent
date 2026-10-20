/* 全站 UX 评审 · 取证脚本 2（补齐：内部滚动容器分段截图 / 取消弹窗 / 管理端）
 * 用法：node scripts/_ux_audit_shots2.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;

const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
fs.mkdirSync(OUT, { recursive: true });
const log = H.makeLogger(path.join(OUT, '_run2.log'));

// 找页面里真正的滚动容器（overflow-y auto/scroll 且 scrollHeight > clientHeight）
const SCROLLER = `(() => {
  const cands = [...document.querySelectorAll('*')].filter(e => {
    const cs = getComputedStyle(e);
    return /(auto|scroll)/.test(cs.overflowY) && e.scrollHeight - e.clientHeight > 40;
  }).map(e => ({ cls: (e.className||'').toString().slice(0,70), h: e.scrollHeight, c: e.clientHeight }))
    .sort((a,b) => b.h - a.h);
  return JSON.stringify(cands.slice(0, 6));
})()`;

const scrollMain = (y) => `(() => {
  const c = [...document.querySelectorAll('*')].filter(e => {
    const cs = getComputedStyle(e);
    return /(auto|scroll)/.test(cs.overflowY) && e.scrollHeight - e.clientHeight > 40;
  }).sort((a,b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
  if (!c) { window.scrollTo(0, ${y}); return 'win'; }
  c.scrollTop = ${y};
  return 'cls:' + (c.className||'').toString().slice(0,50) + ' top=' + c.scrollTop;
})()`;

async function loginAs(h, user, pwd) {
  await h.eval(`localStorage.removeItem('xb_token')`);
  await h.goto(BASE + '/login');
  await h.type('input[placeholder="请输入用户名"]', user);
  await h.type('input[placeholder="请输入密码"]', pwd);
  await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
  await sleep(3600);
}

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  try {
    /* ========== 1. 登录 user1，逐页滚动分段截图 ========== */
    await h.goto(BASE + '/login');
    await h.eval(`localStorage.clear()`);
    await loginAs(h, 'user1', '123456');
    log('NAV', await h.eval('location.href'));

    for (const [name, hash] of [['subs', '#/subs'], ['cards', '#/cards'], ['wealth', '#/wealth'], ['safe', '#/safe'], ['bills', '#/bills'], ['chat', '#/chat']]) {
      await h.eval(`location.hash = ${JSON.stringify(hash)}`);
      await sleep(2500);
      const sc = await h.eval(SCROLLER);
      log('SCROLLER', name + ' ' + sc);
      for (const y of [0, 400, 800, 1200, 1600]) {
        const r = await h.eval(scrollMain(y));
        await sleep(700);
        await h.screenshot(path.join(OUT, `${name}_s${y}.png`));
        if (String(r).startsWith('win') && y > 0) break;
      }
      log('SHOT', name + ' segmented');
    }

    /* ========== 2. 订阅页取消：真点「取消」按钮 + 弹窗 + ESC ========== */
    await h.eval(`location.hash = '#/subs'`);
    await sleep(2500);
    await h.eval(scrollMain(900));
    await sleep(800);
    await h.screenshot(path.join(OUT, 'subs_list_scrolled.png'));
    const c1 = await h.clickByText('取消', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(1400);
    await h.screenshot(path.join(OUT, 'subs_modal1.png'));
    log('CLICK', '点「取消」→ ' + c1);
    // 弹窗内推进（走三阶段，观察文案）
    const t1 = (await h.bodyText()) || '';
    const c2 = await h.clickByText('继续取消', { tag: 'button', nth: 0 });
    await sleep(1200);
    await h.screenshot(path.join(OUT, 'subs_modal2.png'));
    log('CLICK', '点「继续取消」→ ' + c2);
    // ESC 关闭
    await h.eval(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',keyCode:27,bubbles:true}));document.dispatchEvent(new KeyboardEvent('keyup',{key:'Escape',keyCode:27,bubbles:true}));`);
    await sleep(1000);
    const hasModal = await h.eval(`!!document.querySelector('[class*="fixed"][class*="z-"]') && /继续取消|确认取消|终止/.test(document.body.innerText)`);
    log('CHECK', 'ESC 后弹窗仍在=' + hasModal);
    await h.screenshot(path.join(OUT, 'subs_after_esc2.png'));
    fs.writeFileSync(path.join(OUT, 'subs_modal_text.txt'), (await h.bodyText()) || '');

    /* ========== 3. 转账页：第 1/2 步是否显示手续费（真填表） ========== */
    await h.eval(`location.hash = '#/transfer'`);
    await sleep(2600);
    await h.screenshot(path.join(OUT, 'transfer_s1.png'));
    const t = await h.bodyText();
    fs.writeFileSync(path.join(OUT, 'transfer_step1_text.txt'), t || '');
    log('CHECK', '转账第1步含「手续费」=' + /手续费/.test(t || '') + ' 含「费率」=' + /费率/.test(t || ''));

    /* ========== 4. 卡片页危险操作按钮取证 ========== */
    await h.eval(`location.hash = '#/cards'`);
    await sleep(2500);
    await h.eval(scrollMain(600));
    await sleep(800);
    await h.screenshot(path.join(OUT, 'cards_ops.png'));
    const cardBtns = await h.eval(`(()=>{const bs=[...document.querySelectorAll('button')].filter(b=>/查状态|归集|冻结|解冻|注销|问小微/.test(b.innerText||'')).map(b=>{const cs=getComputedStyle(b);return {t:b.innerText.trim().slice(0,8),color:cs.color,bg:cs.backgroundColor,border:cs.borderColor,fs:cs.fontSize}});return JSON.stringify(bs.slice(0,14))})()`);
    log('BTN', 'cards 危险操作样式 ' + cardBtns);

    /* ========== 5. 管理端登录（修选择器） ========== */
    await h.goto(BASE + '/admin/login');
    await h.type('input[placeholder="如：admin"]', 'admin');
    await h.type('input[type="password"]', 'admin123');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3800);
    log('NAV', 'admin → ' + (await h.eval('location.href')));
    await h.screenshot(path.join(OUT, 'admin_dash.png'));
    fs.writeFileSync(path.join(OUT, 'admin_dash.txt'), (await h.bodyText()) || '');
    await h.setViewport(900, 800);
    await h.screenshot(path.join(OUT, 'admin_900.png'));
    log('DONE', 'ok2');
  } catch (e) {
    log('EXCEPTION', String((e && e.message) || e));
  } finally {
    await h.close().catch(() => {});
  }
  process.exit(0);
})();
