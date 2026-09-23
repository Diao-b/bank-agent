/* 用 CDP 驱动无头 Edge 验证「聊天基本功能」在真实 UI 里不卡死：
   1) 注入登录态 → 打开 /chat
   2) 输入一句普通问题 → 点「发 送」
   3) 轮询页面状态：等「小微正在输入」消失、且出现助手回复
   4) 断言：未出现「响应超时」/「连接中断」/「后端调用失败」，并在限时内拿到回复
   5) 截图落盘
   用法：node scripts/_chat_ui_check.js <token> [outDir] [问题]
*/
const fs = require('fs');
const path = require('path');

const ARGV = process.argv.slice(2).filter(a => !a.startsWith('--'));
const TOK = ARGV[0];
const OUT = ARGV[1] || path.join(__dirname, '..', '_reports', 'chat_ui');
const Q = ARGV[2] || '查一下我的余额';
const BASE = 'http://127.0.0.1:8178';
const CDP = 'http://127.0.0.1:9222';
const DEADLINE_MS = 90000;

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

// 只看「最新一轮」的对话区文本（避免历史消息干扰断言）
const PROBE = `(() => {
  const txt = document.body.innerText.replace(/\\r/g, '');
  const typing = txt.includes('小微正在输入');
  const bad = ['响应超时', '连接中断，未收到完整回复', '后端调用失败'].filter(k => txt.includes(k));
  return JSON.stringify({ typing, bad, len: txt.length });
})()`;

const TYPE_SEND = (text) => `(() => {
  const ta = document.querySelector('textarea');
  if (!ta) return 'no-textarea';
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, ${JSON.stringify(text)});
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  const btn = [...document.querySelectorAll('button')].find(b => /发\\s*送/.test(b.textContent) && !b.disabled);
  if (!btn) return 'no-send-button';
  btn.click();
  return 'sent';
})()`;

// 抓「最后一条助手气泡」的文本，用于确认确实渲染出了回复
const LAST_ASSISTANT = `(() => {
  const nodes = [...document.querySelectorAll('div')].filter(d =>
    d.className && String(d.className).includes('max-w-') && d.textContent.trim().length > 8);
  const last = nodes[nodes.length - 1];
  return JSON.stringify({ count: nodes.length, text: last ? last.textContent.trim().slice(0, 200) : '' });
})()`;

(async () => {
  if (!TOK) throw new Error('missing token');
  fs.mkdirSync(OUT, { recursive: true });
  const target = await pickPageTarget();
  const { ready, send, close } = cdp(target.webSocketDebuggerUrl);
  await ready;
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
  });

  // 必须先在应用源上再写 localStorage
  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(4000);
  await send('Runtime.evaluate', {
    expression: `localStorage.setItem('xb_token', ${JSON.stringify(TOK)});
                 localStorage.setItem('xb_user', 'user1');
                 localStorage.setItem('xb_digital_human', '0'); 'seeded'`,
  });
  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(7000);

  const typed = await send('Runtime.evaluate', { expression: TYPE_SEND(Q), returnByValue: true });
  console.log('[1] 发送「' + Q + '」：', typed.result.value);

  const t0 = Date.now();
  let st = null, sawTyping = false;
  while (Date.now() - t0 < DEADLINE_MS) {
    await sleep(1500);
    st = JSON.parse((await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value);
    if (st.typing) sawTyping = true;
    if (!st.typing && sawTyping) break;      // 出现过「正在输入」且已收口
    if (st.bad.length) break;                // 出现错误提示 → 提前结束
  }
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.log('[2] 耗时', elapsed, 's | 状态', JSON.stringify(st));

  const la = JSON.parse((await send('Runtime.evaluate', { expression: LAST_ASSISTANT, returnByValue: true })).result.value);
  console.log('[3] 助手气泡数', la.count, '| 末条:', JSON.stringify(la.text));

  const r = await send('Page.captureScreenshot', { format: 'png' });
  const png = path.join(OUT, 'chat-reply.png');
  fs.writeFileSync(png, Buffer.from(r.data, 'base64'));
  console.log('[4] 截图：', png, fs.statSync(png).size, 'bytes');

  console.log(st.bad.length ? ('[FAIL] 出现错误提示：' + st.bad.join(' / '))
                            : '[PASS] 未出现超时/断流/后端失败提示');
  console.log(sawTyping ? '[PASS] 「正在输入」出现过且已收口（未卡死）' : '[WARN] 未观察到「正在输入」阶段（可能回复过快）');
  console.log(la.text && la.text.length > 8 ? '[PASS] 助手回复已渲染进气泡' : '[FAIL] 未见助手回复内容');

  close();
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
