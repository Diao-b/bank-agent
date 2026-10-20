/* 全站 UX 评审 · 取证脚本（真机 CDP）
 * 目的：逐页真打开 / 真滚动 / 真点击 / 切窄屏，产出截图 + 可见文本 + 页面统计，
 *       供 UX 评审报告引用（不使用源码臆断视觉）。
 * 用法：node scripts/_ux_audit_shots.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;

const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
fs.mkdirSync(OUT, { recursive: true });
const log = H.makeLogger(path.join(OUT, '_run.log'));

const PAGES = [
  ['chat', '#/chat'],
  ['bills', '#/bills'],
  ['wealth', '#/wealth'],
  ['cards', '#/cards'],
  ['subs', '#/subs'],
  ['safe', '#/safe'],
  ['transfer', '#/transfer'],
  ['appearance', '#/appearance'],
];

const PAGE_STATS = `(() => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const all = [...document.querySelectorAll('*')].filter(vis);
  const tiny = all.filter(e => {
    const fs = parseFloat(getComputedStyle(e).fontSize);
    return fs > 0 && fs <= 11.5 && (e.innerText||'').trim();
  });
  const btns = all.filter(e => e.tagName === 'BUTTON');
  // 对比度粗算：文字色与最近不透明背景色
  const lum = c => { const [r,g,b] = c.match(/\\d+/g).slice(0,3).map(Number).map(v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)}); return 0.2126*r+0.7152*g+0.0722*b; };
  const bgOf = el => { let n = el; while (n && n !== document.body) { const bg = getComputedStyle(n).backgroundColor; if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') return bg; n = n.parentElement; } return getComputedStyle(document.body).backgroundColor; };
  const samples = [];
  const KEY = /取消订阅|赎回|注销|冻结|解冻|费用|费率|首次扣款|闲置|逾期|确认订阅|签约|验证码|短信|支付密码/;
  all.forEach(e => {
    const t = (e.innerText||'').trim();
    if (!t || t.length > 40) return;
    if (!KEY.test(t)) return;
    if (e.children.length > 0) return;
    const cs = getComputedStyle(e);
    const fg = cs.color, bg = bgOf(e);
    let ratio = null;
    try { const l1 = lum(fg), l2 = lum(bg); ratio = ((Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05)).toFixed(2); } catch(_) {}
    samples.push({ text: t.slice(0,30), fontSize: cs.fontSize, color: fg, bg, ratio, cls: (e.className||'').toString().slice(0,60) });
  });
  return JSON.stringify({
    visibleNodes: all.length,
    tinyCount: tiny.length,
    tinySample: tiny.slice(0, 25).map(e => (e.innerText||'').trim().slice(0, 28) + ' @' + getComputedStyle(e).fontSize),
    buttonCount: btns.length,
    buttonLabels: btns.map(b => (b.innerText||'').trim().replace(/\\s+/g,' ').slice(0,24)).filter(Boolean).slice(0, 30),
    keyText: samples.slice(0, 40),
    scrollH: document.documentElement.scrollHeight,
    innerH: window.innerHeight,
    mediaQueries: [...document.styleSheets].flatMap(s => { try { return [...s.cssRules].filter(r => r.type === 4).map(r => r.conditionText); } catch(_) { return []; } }),
    h1: [...document.querySelectorAll('h1')].map(e => e.innerText.slice(0,40)),
    ariaInputs: [...document.querySelectorAll('input,textarea')].map(e => ({ ph: e.placeholder || '', aria: e.getAttribute('aria-label') || '' })).slice(0, 12),
    dialogAttrs: [...document.querySelectorAll('[role="dialog"],[aria-modal]')].length,
  });
})()`;

async function shotBody(h, name, tag) {
  const txt = (await h.bodyText()) || '';
  fs.writeFileSync(path.join(OUT, `${name}${tag}.txt`), txt);
  return txt.length;
}

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  const report = {};
  try {
    /* ---------- 登录页（未登录） ---------- */
    await h.setViewport(1440, 920);
    await h.goto(BASE + '/login');
    await h.eval(`localStorage.clear()`);
    await h.goto(BASE + '/login');
    await h.screenshot(path.join(OUT, 'login_1440.png'));
    await shotBody(h, 'login', '_1440');
    log('SHOT', 'login_1440');
    await h.setViewport(900, 780);
    await h.goto(BASE + '/login');
    await h.screenshot(path.join(OUT, 'login_900.png'));
    log('SHOT', 'login_900');
    await h.setViewport(1440, 920);

    /* ---------- 登录 user1 ---------- */
    await h.goto(BASE + '/login');
    await h.type('input[placeholder="请输入用户名"]', 'user1');
    await h.type('input[placeholder="请输入密码"]', '123456');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3600);
    log('NAV', await h.eval('location.href'));

    /* ---------- 逐页：截图 + 文本 + 统计 ---------- */
    for (const [name, hash] of PAGES) {
      await h.eval(`location.hash = ${JSON.stringify(hash)}`);
      await sleep(2600);
      // 首屏
      await h.screenshot(path.join(OUT, `${name}_1440.png`));
      const stats = JSON.parse(await h.eval(PAGE_STATS));
      report[name] = stats;
      const len = await shotBody(h, name, '_1440');
      log('PAGE', `${name} text=${len} nodes=${stats.visibleNodes} tiny=${stats.tinyCount} btns=${stats.buttonCount} scrollH=${stats.scrollH}`);
      // 全页（限高，避免超大图）
      if (stats.scrollH > 1000) {
        await h.screenshot(path.join(OUT, `${name}_full.png`), { full: true });
      }
    }

    /* ---------- 订阅页：真点击关键交互（无副作用路径） ---------- */
    await h.eval(`location.hash = '#/subs'`);
    await sleep(2400);
    const before = (await h.bodyText()) || '';
    const clicked = await h.clickByText('取消订阅', { tag: 'button', nth: 0 });
    await sleep(1500);
    const after = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'subs_cancel_modal.png'));
    log('CLICK', '取消订阅 → ' + clicked + ' | 弹窗出现=' + (/确认|取消|生效/.test(after) && after !== before));
    const esc = await h.eval(`(()=>{const n=[...document.querySelectorAll('*')].length;document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));return n})()`);
    await sleep(900);
    const afterEsc = (await h.bodyText()) || '';
    log('CLICK', 'ESC 关闭取消弹窗生效=' + (afterEsc.length < after.length + 40 ? 'likely' : 'no'));
    await h.screenshot(path.join(OUT, 'subs_after_esc.png'));

    /* ---------- 转账页：走两步看费用是否提前显示 ---------- */
    await h.eval(`location.hash = '#/transfer'`);
    await sleep(2400);
    await h.screenshot(path.join(OUT, 'transfer_step1.png'));
    await shotBody(h, 'transfer_step1', '_1440');
    log('PAGE', 'transfer 首屏已截');

    /* ---------- 窄屏（900px）关键页 ---------- */
    await h.setViewport(900, 800);
    for (const [name, hash] of [['chat', '#/chat'], ['subs', '#/subs'], ['cards', '#/cards']]) {
      await h.eval(`location.hash = ${JSON.stringify(hash)}`);
      await sleep(2200);
      await h.screenshot(path.join(OUT, `${name}_900.png`));
      log('SHOT', `${name}_900`);
    }
    await h.setViewport(1440, 920);

    /* ---------- 退出 → user2（对比新号空态） ---------- */
    await h.eval(`location.hash = '#/chat'`);
    await sleep(1600);
    await h.eval(`(()=>{const b=[...document.querySelectorAll('header button')].find(x=>(x.innerText||'').includes('user1'));b&&b.click();return 1})()`);
    await sleep(700);
    await h.eval(`(()=>{const b=[...document.querySelectorAll('header button')].find(x=>(x.innerText||'').includes('退出登录'));b&&b.click();return 1})()`);
    await sleep(2600);
    await h.goto(BASE + '/login');
    await h.type('input[placeholder="请输入用户名"]', 'user2');
    await h.type('input[placeholder="请输入密码"]', '123456');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3600);
    for (const [name, hash] of [['bills', '#/bills'], ['subs', '#/subs']]) {
      await h.eval(`location.hash = ${JSON.stringify(hash)}`);
      await sleep(2400);
      await h.screenshot(path.join(OUT, `${name}_u2_1440.png`));
      await shotBody(h, `${name}_u2`, '_1440');
      log('SHOT', `${name}_u2_1440`);
    }

    /* ---------- 管理端 ---------- */
    await h.goto(BASE + '/admin/login');
    await h.type('input[placeholder="请输入用户名"], input[type="text"]', 'admin');
    await h.type('input[type="password"]', 'admin123');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3600);
    await h.screenshot(path.join(OUT, 'admin_1440.png'));
    await shotBody(h, 'admin', '_1440');
    log('NAV', 'admin → ' + (await h.eval('location.href')));

    fs.writeFileSync(path.join(OUT, '_stats.json'), JSON.stringify(report, null, 1));
    log('DONE', 'ok');
  } catch (e) {
    log('EXCEPTION', String((e && e.message) || e));
  } finally {
    await h.close().catch(() => {});
  }
  process.exit(0);
})();
