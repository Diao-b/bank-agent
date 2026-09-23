/* 验证「连续发多条消息」的三条规则（CDP 驱动无头 Edge）：
   1) 连发 A/B/C 三条 → 气泡都出现，B/C 标记「排队中 · 尚未提问」
   2) 只提交最后一条 C；B 被顶掉 → 等确认
   3) 补答完出现固定话术卡「是否还要我继续回答上面的问题？」且**不含** B 的具体内容
   4) 点「继续」→ B 被补答
   用法：node scripts/_multi_ask_check.js <token> [outDir]
*/
const fs = require('fs');
const path = require('path');

const ARGV = process.argv.slice(2).filter(a => !a.startsWith('--'));
const TOK = ARGV[0];
const OUT = ARGV[1] || path.join(__dirname, '..', '_reports', 'multi_ask');
const BASE = 'http://127.0.0.1:8178';
const CDP = 'http://127.0.0.1:9222';

// ★ 文案要避开前端 NAV_INTENTS 的话题词（账单/支出/花/银行卡/理财/订阅/安全…），
//   否则短句会被判定为「打开某页」意图，直接把页面从 /chat 导走，测的就不是对话页了。
const A = '你好呀';
const B = '你叫什么名字';
const C = '你能做什么';
const ASK = '是否还要我继续回答上面的问题？';

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

// 一次性连发三条（同一轮 JS 内完成，确保 inFlightRef 时序确定）
const SEND_THREE = `(() => {
  const ta = document.querySelector('textarea');
  if (!ta) return 'no-textarea';
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  const out = [];
  ${JSON.stringify([A, B, C])}.forEach((t) => {
    setter.call(ta, t);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    const btn = [...document.querySelectorAll('button')].find(b => /发\\s*送|排\\s*队/.test(b.textContent) && !b.disabled);
    if (!btn) { out.push('no-btn:' + t); return; }
    btn.click();
    out.push('sent:' + t);
  });
  return out.join(' | ');
})()`;

const PROBE = `(() => {
  const txt = document.body.innerText.replace(/\\r/g, '');
  const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '继续');
  const cardEl = btn ? (btn.closest('div.rounded-2xl') || btn.parentElement.parentElement) : null;
  return JSON.stringify({
    hash: location.hash,
    queued: (txt.match(/排队中 · 尚未提问/g) || []).length,
    hasAsk: !!btn,
    askCardText: cardEl ? cardEl.textContent.trim() : '',
    hint: (txt.match(/已排队 \\d+ 条[^\\n]*/) || [''])[0],
  });
})()`;

const CLICK_CONTINUE = `(() => {
  const b = [...document.querySelectorAll('button')].find(e => e.textContent.trim() === '继续');
  if (!b) return 'no-continue-btn';
  b.click();
  return 'clicked';
})()`;

const shot = async (send, name) => {
  const r = await send('Page.captureScreenshot', { format: 'png' });
  const p = path.join(OUT, name);
  fs.writeFileSync(p, Buffer.from(r.data, 'base64'));
  console.log('  截图：', p, fs.statSync(p).size, 'bytes');
};

(async () => {
  if (!TOK) throw new Error('missing token');
  fs.mkdirSync(OUT, { recursive: true });
  const target = await pickPageTarget();
  const { ready, send, close } = cdp(target.webSocketDebuggerUrl);
  await ready;
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 980, deviceScaleFactor: 1, mobile: false });

  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(4000);
  await send('Runtime.evaluate', {
    expression: `localStorage.setItem('xb_token', ${JSON.stringify(TOK)});
                 localStorage.setItem('xb_user', 'user1');
                 localStorage.setItem('xb_digital_human', '0'); 'seeded'`,
  });
  // 新会话，避免历史干扰
  await send('Runtime.evaluate', {
    expression: `localStorage.removeItem('xb_thread'); 'cleared'`,
  });
  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(7000);

  const r1 = await send('Runtime.evaluate', { expression: SEND_THREE, returnByValue: true });
  console.log('[1] 连发三条：', r1.result.value);

  // ★ 必须在第一轮答完之前探测（一轮可能只要几秒），否则队列已被 drain 走
  await sleep(500);
  let st = JSON.parse((await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value);
  console.log('[2] 即时状态：', JSON.stringify(st));
  const okHash = st.hash.includes('/chat');
  const okQueued = st.queued === 2;
  console.log(okHash ? '[PASS] 仍在对话页（未被跳转意图导走）' : `[FAIL] 已被导到 ${st.hash}`);
  console.log(okQueued ? '[PASS] B/C 两条气泡标记「排队中 · 尚未提问」' : `[FAIL] 排队标记数 ${st.queued}（期望 2）`);
  console.log(st.hint ? `[PASS] 输入区出现排队提示：${st.hint}` : '[WARN] 未见输入区排队提示');
  await shot(send, '02-queued.png');

  // 等收口卡出现（A、C 各约十几秒）
  const t0 = Date.now();
  while (Date.now() - t0 < 150000) {
    await sleep(2500);
    st = JSON.parse((await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value);
    if (st.hasAsk) break;
  }
  console.log('[3] 等待', ((Date.now() - t0) / 1000).toFixed(1), 's 后：', JSON.stringify(st));
  const cardOk = st.askCardText.includes(ASK);
  const noLeak = !!st.askCardText && !st.askCardText.includes(B);
  console.log(st.hasAsk ? '[PASS] 出现收口卡' : '[FAIL] 未出现收口卡');
  console.log(cardOk ? `[PASS] 卡内话术为固定文案「${ASK}」` : `[FAIL] 卡内话术不符：${st.askCardText}`);
  console.log(noLeak ? '[PASS] 卡内未列出被跳过问题的具体内容' : `[FAIL] 卡内泄露／缺内容：${st.askCardText}`);
  console.log(st.queued === 0 ? '[PASS] 排队标记已清除' : `[WARN] 仍有 ${st.queued} 条标记排队中`);
  await shot(send, '03-ask-more.png');

  const c = await send('Runtime.evaluate', { expression: CLICK_CONTINUE, returnByValue: true });
  console.log('[4] 点「继续」：', c.result.value);
  await sleep(2500);
  const st2 = JSON.parse((await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value);
  console.log('[5] 继续后：', JSON.stringify(st2));
  await shot(send, '05-after-continue.png');

  console.log('---');
  console.log(okHash && okQueued && st.hasAsk && cardOk && noLeak
    ? '全部核心断言通过' : '存在未通过项，见上');

  close();
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
