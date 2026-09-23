/* 验证「删除会话」入口（CDP 驱动无头 Edge）：
   1) 先发一条消息造一个**一次性会话**（避免误删真实历史）
   2) 找到它的「删」按钮 → 点一下应变「确认」（两步确认）
   3) 再点一下 → 该会话从列表消失、条数 -1
   用法：node scripts/_conv_delete_check.js <token> [outDir]
*/
const fs = require('fs');
const path = require('path');

const ARGV = process.argv.slice(2).filter(a => !a.startsWith('--'));
const TOK = ARGV[0];
const OUT = ARGV[1] || path.join(__dirname, '..', '_reports', 'conv_del');
const BASE = 'http://127.0.0.1:8178';
const CDP = 'http://127.0.0.1:9222';
const MARK = '删测一次性会话';

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function pickPageTarget() {
  for (let i = 0; i < 20; i++) {
    try {
      const list = await (await fetch(CDP + '/json/list')).json();
      const p = list.filter(t => t.type === 'page').pop();
      if (p && p.webSocketDebuggerUrl) return p;
    } catch (_) {}
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
    ws.addEventListener('open', res); ws.addEventListener('error', rej);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id; pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  return { ready, send, close: () => ws.close() };
}

const SEND = (text) => `(() => {
  const ta = document.querySelector('textarea');
  if (!ta) return 'no-textarea';
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, ${JSON.stringify(text)});
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  const btn = [...document.querySelectorAll('button')].find(b => /发\\s*送|排\\s*队/.test(b.textContent) && !b.disabled);
  if (!btn) return 'no-send';
  btn.click(); return 'sent';
})()`;

/* 会话行 = 含「删除」按钮的那个容器；用按钮文案「删」定位 */
const ROWS = `(() => {
  const btns = [...document.querySelectorAll('button')].filter(b => b.textContent.trim() === '删' || b.textContent.trim() === '确认');
  const titles = btns.map(b => {
    const row = b.closest('div.group') || b.parentElement;
    const t = row ? row.textContent.trim() : '';
    return t.replace(/\\d+ 条消息|未开始/g, '').trim();
  });
  return JSON.stringify({ n: btns.length, labels: btns.map(b => b.textContent.trim()), titles });
})()`;

const CLICK_DEL_BY_TITLE = (title) => `(() => {
  const btns = [...document.querySelectorAll('button')].filter(b => b.textContent.trim() === '删' || b.textContent.trim() === '确认');
  const hit = btns.find(b => {
    const row = b.closest('div.group') || b.parentElement;
    return row && row.textContent.includes(${JSON.stringify(title)});
  });
  if (!hit) return 'not-found';
  hit.click();
  return 'clicked:' + hit.textContent.trim();
})()`;

/* 切到"新会话"，避免删的正是当前打开的那条（那样会连带触发新会话，干扰断言） */
const NEW_CHAT = `(() => {
  const b = [...document.querySelectorAll('button')].find(e => e.textContent.trim() === '新建');
  if (!b) return 'no-newbtn';
  b.click(); return 'ok';
})()`;

(async () => {
  if (!TOK) throw new Error('missing token');
  fs.mkdirSync(OUT, { recursive: true });
  const target = await pickPageTarget();
  const { ready, send, close } = cdp(target.webSocketDebuggerUrl);
  await ready;
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });

  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(4000);
  await send('Runtime.evaluate', {
    expression: `localStorage.setItem('xb_token', ${JSON.stringify(TOK)});
                 localStorage.setItem('xb_user', 'user1');
                 localStorage.setItem('xb_digital_human', '0');
                 localStorage.removeItem('xb_thread'); 'seeded'`,
  });
  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(7000);

  // 造一个一次性会话
  console.log('[1] 发一条消息造一次性会话…');
  console.log('   ', (await send('Runtime.evaluate', { expression: SEND(MARK), returnByValue: true })).result.value);
  await sleep(14000);
  // ★ 必须重新加载：会话标题由后端在收到第一条消息后生成，列表要重新拉一次才看得到
  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(7000);
  const before = JSON.parse((await send('Runtime.evaluate', { expression: ROWS, returnByValue: true })).result.value);

  // ★ fetch 探针：确认「点删除」到底有没有把请求发出去（前端 catch 是静默的，出问题看不到）
  await send('Runtime.evaluate', {
    expression: `(() => {
      window.__fetchLog = [];
      const of = window.fetch;
      window.fetch = function (u, o) {
        try { window.__fetchLog.push(String(u) + ' ' + ((o && o.method) || 'GET')); } catch (e) {}
        return of.apply(this, arguments);
      };
      return 'patched';
    })()`, returnByValue: true,
  });
  console.log('[2] 删除按钮数', before.n, '| 文案样本', JSON.stringify(before.labels.slice(0, 3)));
  console.log('    含一次性会话:', before.titles.some(t => t.includes(MARK)));
  const hasMark = before.titles.some(t => t.includes(MARK));
  console.log(hasMark ? '[PASS] 一次性会话已出现在列表' : '[FAIL] 列表里找不到一次性会话（无法安全测试）');
  if (!hasMark) { close(); process.exit(1); }

  const title = MARK;
  const c1 = await send('Runtime.evaluate', { expression: CLICK_DEL_BY_TITLE(title), returnByValue: true });
  console.log('[3] 第一次点删除 →', c1.result.value, '（期望变成「确认」）');
  await sleep(800);
  const armed = JSON.parse((await send('Runtime.evaluate', { expression: ROWS, returnByValue: true })).result.value);
  const nConfirm = armed.labels.filter(l => l === '确认').length;
  console.log('    当前「确认」按钮数:', nConfirm, nConfirm === 1 ? '[PASS] 已进入两步确认' : '[WARN] 期望恰好 1 个');

  const c2 = await send('Runtime.evaluate', { expression: CLICK_DEL_BY_TITLE(title), returnByValue: true });
  console.log('[4] 第二次点确认 →', c2.result.value);
  await sleep(3000);
  const flog = await send('Runtime.evaluate', { expression: 'JSON.stringify((window.__fetchLog||[]).filter(x => /DELETE/i.test(x)))', returnByValue: true });
  console.log('    前端发出的 DELETE 请求:', flog.result.value);
  const after = JSON.parse((await send('Runtime.evaluate', { expression: ROWS, returnByValue: true })).result.value);
  console.log('[5] 删除后：按钮数', after.n, '| 仍含该会话:', after.titles.some(t => t.includes(title)));

  const shot = await send('Page.captureScreenshot', { format: 'png' });
  const png = path.join(OUT, 'after-delete.png');
  fs.writeFileSync(png, Buffer.from(shot.data, 'base64'));
  console.log('    截图：', png);

  console.log('---');
  /* ★ 不能用「条数 -1」判定：列表接口有 limit=50 上限，删掉一条会立刻补上下一条，
     条数不变。真正的判据是：发出了 DELETE 请求 + 该标题从列表消失。 */
  const issued = /DELETE/i.test(flog.result.value);
  const gone = !after.titles.some(t => t.includes(title));
  console.log(issued ? '[PASS] 前端已发出 DELETE 请求' : '[FAIL] 未发出 DELETE 请求');
  console.log(gone ? '[PASS] 该会话已从列表消失' : '[FAIL] 该会话仍在列表');
  console.log((nConfirm === 1) ? '[PASS] 两步确认（删 → 确认）生效' : '[FAIL] 未进入两步确认');
  console.log((issued && gone && nConfirm === 1) ? '全部通过' : '存在未通过项');
  close();
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
