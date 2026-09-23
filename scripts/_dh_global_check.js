/* 用 CDP 驱动无头 Edge 真实验证「数字柜员小微 = 全局浮层 + 语音/文字意图驱动跳转」：
   1) 注入登录态 + 打开数字人开关，进 /chat
   2) 断言浮层存在（header / iframe），并给浮层 DOM 节点打上 __probeTag
   3) 在输入框输入「打开我的账单详情」→ 点「发 送」
   4) 断言 location.hash 变成 #/bills，且浮层仍在、__probeTag 未变（证明跨路由未卸载/未重建）
   5) 两张截图落盘，供人工核对
   用法：node scripts/_dh_global_check.js <token> [outDir]
*/
const fs = require('fs');
const path = require('path');

const ARGV = process.argv.slice(2).filter(a => !a.startsWith('--'));
const TOK = ARGV[0];
const OUT = ARGV[1] || path.join(__dirname, '..', '_reports', 'dh_global');
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

// 探测浮层：header 文案 / iframe / 折叠气泡 / 节点身份标记
const PROBE = `(() => {
  const hdr = [...document.querySelectorAll('span')].find(s => s.textContent.trim() === '数字柜员 · 小微');
  const frame = document.querySelector('iframe[title="数字柜员小微"]');
  const bubble = document.querySelector('button[title="展开数字柜员小微"]');
  let panel = null;
  if (hdr) { let n = hdr; while (n && n !== document.body) { if (n.className && String(n.className).includes('fixed')) { panel = n; break; } n = n.parentElement; } }
  if (panel && !panel.__probeTag) panel.__probeTag = 'KEEP-' + Date.now();
  return JSON.stringify({
    route: location.hash,
    header: !!hdr,
    iframe: !!frame,
    iframeSrc: frame ? frame.src : null,
    bubble: !!bubble,
    panelTag: panel ? (panel.__probeTag || null) : null,
    panelFixed: panel ? getComputedStyle(panel).position : null,
    offline: document.body.innerText.includes('数字柜员离线'),
  });
})()`;

// React 受控 textarea：原生 value setter + input 事件
const TYPE_SEND = (text) => `(() => {
  const ta = document.querySelector('textarea');
  if (!ta) return 'no-textarea';
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, ${JSON.stringify('')} + ${JSON.stringify(text)});
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  const btn = [...document.querySelectorAll('button')].find(b => /发\\s*送/.test(b.textContent) && !b.disabled);
  if (!btn) return 'no-send-button';
  btn.click();
  return 'sent';
})()`;

const shot = async (send, name) => {
  const r = await send('Page.captureScreenshot', { format: 'png' });
  const p = path.join(OUT, name);
  fs.writeFileSync(p, Buffer.from(r.data, 'base64'));
  console.log('  截图：', p, fs.statSync(p).size, 'bytes');
  return p;
};

(async () => {
  if (!TOK) throw new Error('missing token');
  fs.mkdirSync(OUT, { recursive: true });
  const target = await pickPageTarget();
  const { ready, send, close } = cdp(target.webSocketDebuggerUrl);
  await ready;
  await send('Page.enable');
  await send('Runtime.enable');
  // 强制视口宽 ≥1024（数字人浮层窄屏不渲染）
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
  });

  // ★ 必须先落在应用源上再写 localStorage —— 在 about:blank 上播种不会落到 127.0.0.1:8178
  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(4000);
  await send('Runtime.evaluate', {
    expression: `localStorage.setItem('xb_token', ${JSON.stringify(TOK)});
                 localStorage.setItem('xb_user', 'user1');
                 localStorage.setItem('xb_digital_human', '1'); 'seeded'`,
  });

  // 1) 对话页
  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(7000);
  const a = JSON.parse((await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value);
  console.log('[1] /chat 浮层：', JSON.stringify(a));
  await shot(send, '01-chat-panel.png');

  // 2) 输入「打开我的账单详情」并发送
  const typed = await send('Runtime.evaluate', { expression: TYPE_SEND('打开我的账单详情'), returnByValue: true });
  console.log('[2] 发送指令：', typed.result.value);

  // 3) 等待前端兜底跳转 + LLM 回复
  let b = null;
  for (let i = 0; i < 12; i++) {
    await sleep(2500);
    b = JSON.parse((await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value);
    if (b.route.includes('/bills')) break;
  }
  console.log('[3] 跳转后：', JSON.stringify(b));
  await shot(send, '02-bills-panel.png');

  // 4) 断言
  const okRoute = b.route.includes('/bills');
  const okPanel = b.header && b.iframe;
  const okKeep = a.panelTag && b.panelTag && a.panelTag === b.panelTag;
  const okFixed = b.panelFixed === 'fixed';
  console.log(okRoute ? '[PASS] 意图驱动跳转 → /bills' : '[FAIL] 未跳转，当前 ' + b.route);
  console.log(okPanel ? '[PASS] 浮层跨页仍在（header + iframe 均存在）' : '[FAIL] 浮层丢失');
  console.log(okKeep ? '[PASS] 浮层 DOM 节点未重建（tag 一致 ' + b.panelTag + '）' : '[FAIL] 节点被重挂载');
  console.log(okFixed ? '[PASS] 浮层为 fixed 定位' : '[FAIL] 定位异常 ' + b.panelFixed);
  console.log(okPanel ? (b.offline ? '[INFO] 面板显示「数字柜员离线」（Fay/5173 未连）' : '[INFO] 数字人已连接')
                      : '');

  close();
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
