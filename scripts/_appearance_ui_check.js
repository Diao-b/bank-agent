/* 用 CDP 驱动无头 Edge 真实验证「数字人形象设置」页：
   1) 注入 admin token 打开 /chat#/appearance
   2) 读预览 iframe 的 src 与下拉选中值
   3) 模拟把「人物模型」改为 Hiyori（React 受控 select）→ 检查预览是否自动跟随
   4) 截图保存，供人工核对是否真的换人
   用法：node scripts/_appearance_ui_check.js <token> [outDir]
*/
const fs = require('fs');
const path = require('path');

const ARGV = process.argv.slice(2).filter(a => !a.startsWith('--'));
const TOK = ARGV[0];
const OUT = ARGV[1] || path.join(__dirname, '..', '_reports', 'l2d');
const BASE = 'http://127.0.0.1:8178';
const CDP = 'http://127.0.0.1:9222';

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function pickPageTarget() {
  for (let i = 0; i < 20; i++) {
    try {
      const list = await (await fetch(CDP + '/json/list')).json();
      const p = list.filter(t => t.type === 'page').pop();
      if (p && p.webSocketDebuggerUrl) return p;
    } catch (_) { /* retry */ }
    await sleep(500);
  }
  throw new Error('no CDP page target');
}

function cdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    }
  });
  const ready = new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', rej);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  return { ready, send, close: () => ws.close() };
}

const PROBE = `(() => {
  const f = document.querySelector('iframe[title="数字人形象预览"]');
  const sels = [...document.querySelectorAll('select')];
  return JSON.stringify({
    route: location.hash,
    selectCount: sels.length,
    selModel: sels[0] ? sels[0].value : null,
    selBg: sels[1] ? sels[1].value : null,
    iframeSrc: f ? f.src : null,
    headerText: (document.querySelector('iframe[title="数字人形象预览"]') || {}).title || null,
    bodySnippet: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 300),
  });
})()`;

// React 受控 <select>：必须用原生 value setter + 冒泡 change 事件才能触发 onChange
const SET_MODEL = (name) => `(() => {
  const sel = document.querySelectorAll('select')[0];
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
  setter.call(sel, ${JSON.stringify(name)});
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  return sel.value;
})()`;

(async () => {
  if (!TOK) throw new Error('missing token');
  fs.mkdirSync(OUT, { recursive: true });
  const target = await pickPageTarget();
  const { ready, send, close } = cdp(target.webSocketDebuggerUrl);
  await ready;
  await send('Page.enable');
  await send('Runtime.enable');

  // 1) 注入登录态
  await send('Runtime.evaluate', {
    expression: `localStorage.setItem('xb_token', ${JSON.stringify(TOK)});
                 localStorage.setItem('xb_user', 'admin');
                 localStorage.setItem('xb_digital_human', '1'); 'seeded'`,
  });

  const isAdmin = process.argv.includes('--admin');

  if (isAdmin) {
    // 管理后台：/admin 默认落在数据看板，需点左侧导航「数字人形象」切页
    await send('Page.navigate', { url: BASE + '/admin' });
    await sleep(4500);
    const clicked = await send('Runtime.evaluate', {
      expression: `(() => {
        const el = [...document.querySelectorAll('button,a,div')]
          .filter(e => e.textContent.trim() === '数字人形象' && e.children.length === 0).pop();
        if (el) { (el.closest('button,a') || el).click(); return 'clicked'; }
        return 'nav-not-found';
      })()`, returnByValue: true,
    });
    console.log('[0] 切到形象页：', clicked.result.value);
    await sleep(4500);
  } else {
    // 2) 打开形象设置页
    await send('Page.navigate', { url: BASE + '/chat#/appearance' });
    await sleep(5000);
  }
  const before = JSON.parse((await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value);
  console.log('[1] 初始：', JSON.stringify(before, null, 2));

  // 3) 切换到 Hiyori —— 不点任何按钮，看预览是否自动跟随
  const applied = await send('Runtime.evaluate', { expression: SET_MODEL('Hiyori'), returnByValue: true });
  console.log('[2] 已把下拉切到：', applied.result.value);
  await sleep(4500);
  const after = JSON.parse((await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value);
  console.log('[3] 切换后：', JSON.stringify(after, null, 2));

  const ok = /model=Hiyori/.test(after.iframeSrc || '');
  console.log(ok ? '[PASS] 预览 iframe 已自动切到 Hiyori（无需点按钮）'
                 : '[FAIL] 预览未跟随选择：' + after.iframeSrc);

  // 4) 截图
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  const png = path.join(OUT, isAdmin ? 'appearance-admin-page.png' : 'appearance-page.png');
  fs.writeFileSync(png, Buffer.from(shot.data, 'base64'));
  console.log('[4] 截图：', png, fs.statSync(png).size, 'bytes');

  // 5) 返回按钮：SPA 页应存在「返回」，点击后回到 /chat（管理后台页无此按钮，跳过）
  if (!isAdmin) {
    const back = await send('Runtime.evaluate', {
      expression: `(() => {
        const b = [...document.querySelectorAll('button')]
          .find(e => e.title === '返回对话' && e.textContent.includes('返回'));
        if (!b) return JSON.stringify({ found: false });
        const r = b.getBoundingClientRect();
        return JSON.stringify({ found: true, w: Math.round(r.width), h: Math.round(r.height), text: b.textContent.trim() });
      })()`, returnByValue: true,
    });
    const info = JSON.parse(back.result.value);
    console.log('[5] 返回按钮：', JSON.stringify(info));
    if (!info.found) { console.log('[FAIL] 未找到返回按钮'); close(); return; }
    await send('Runtime.evaluate', {
      expression: `[...document.querySelectorAll('button')]
        .find(e => e.title === '返回对话' && e.textContent.includes('返回')).click(); 'clicked'`,
      returnByValue: true,
    });
    await sleep(1200);
    const hash = (await send('Runtime.evaluate', { expression: 'location.hash', returnByValue: true })).result.value;
    console.log(hash.startsWith('#/chat')
      ? `[PASS] 点击返回后已跳转：${hash}` : `[FAIL] 返回未跳转，当前 ${hash}`);
    const shot2 = await send('Page.captureScreenshot', { format: 'png' });
    const png2 = path.join(OUT, 'appearance-after-back.png');
    fs.writeFileSync(png2, Buffer.from(shot2.data, 'base64'));
    console.log('[5] 返回后截图：', png2, fs.statSync(png2).size, 'bytes');
  }

  close();
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
