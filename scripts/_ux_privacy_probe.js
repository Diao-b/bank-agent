/* 隐私模式明文金额 · 精确定位（带可见性过滤 + 上下文 + 用户数据对照）
 * 目的：区分「用户资产明文（真泄漏）」与「静态目录文案（非泄漏）」
 * 用法：node scripts/_ux_privacy_probe.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_privacy_probe.log'));

const PROBE = `(() => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const out = [];
  document.querySelectorAll('*').forEach(e => {
    if (e.children.length) return;
    if (!vis(e)) return;
    const t = (e.innerText || '').trim();
    if (!t || !/¥\\s?[0-9]/.test(t) || /·/.test(t)) return;
    const ctx = (e.closest('li,tr,div[class*=rounded]') || e.parentElement || e);
    out.push({
      text: t.slice(0, 30),
      cls: (e.className || '').toString().slice(0, 45),
      fs: getComputedStyle(e).fontSize,
      ctx: (ctx.innerText || '').replace(/\\s+/g, ' ').slice(0, 70),
    });
  });
  return JSON.stringify(out.slice(0, 25));
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
    const priv = await h.eval(`(()=>{const b=[...document.querySelectorAll('header button')].find(x=>/隐私/.test(x.innerText||''));return b?b.innerText.trim():'none'})()`);
    log('PRIVACY', priv);

    for (const [name, hash] of [['chat', '#/chat'], ['wealth', '#/wealth'], ['cards', '#/cards'], ['bills', '#/bills']]) {
      await h.eval(`location.hash = ${JSON.stringify(hash)}`);
      await sleep(2600);
      const rows = JSON.parse(await h.eval(PROBE));
      log('=== ' + name + ' 可见明文金额 ' + rows.length + ' 处', '');
      rows.forEach(r => log('   ', r.text + '  [' + r.fs + '] ' + r.cls + '  ctx=' + r.ctx));
    }

    /* 对照：关掉隐私后 chat 会话列表是否同样显示金额（判断是否与隐私相关） */
    await h.eval(`location.hash = '#/chat'`);
    await sleep(2400);
    const chatPrivOn = JSON.parse(await h.eval(PROBE)).map(r => r.text);
    log('CHAT 隐私开', JSON.stringify(chatPrivOn));
    await h.eval(`(()=>{const b=[...document.querySelectorAll('header button')].find(x=>/隐私/.test(x.innerText||''));b&&b.click();return 1})()`);
    await sleep(1600);
    const chatPrivOff = JSON.parse(await h.eval(PROBE)).map(r => r.text);
    log('CHAT 隐私关', JSON.stringify(chatPrivOff));
    log('DONE', 'ok');
  } catch (e) {
    log('EXCEPTION', String((e && e.message) || e));
  } finally {
    await h.close().catch(() => {});
  }
  process.exit(0);
})();
