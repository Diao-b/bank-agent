/* 探针：URL 能否携带身份 / 全新浏览器是否被拦去登录
 * 场景还原：浏览器A 登录后复制出的 URL，粘到「全新浏览器」是否直接进号。
 * 用 CDP 的 createBrowserContext 造一个隔离存储的上下文（等价于另一台浏览器/隐身窗）。
 * 用法：node scripts/_probe_url_auth.js
 */
const CDP = 'http://127.0.0.1:9222';
const TARGET = 'http://127.0.0.1:8178/chat#/chat';   // 复制的网址（带 hash 路由）
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function connect() {
  const v = await (await fetch(CDP + '/json/version')).json();
  const ws = new WebSocket(v.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0; const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { resolve, reject } = pending.get(m.id); pending.delete(m.id);
      m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
    }
  };
  const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
    const mid = ++id; pending.set(mid, { resolve, reject });
    const p = { id: mid, method, params }; if (sessionId) p.sessionId = sessionId;
    ws.send(JSON.stringify(p));
  });
  return send;
}

(async () => {
  const send = await connect();

  // 用一个全新的隔离浏览器上下文（空白 localStorage，等价于"另一台浏览器"）
  const { browserContextId } = await send('Target.createBrowserContext');
  const { targetId } = await send('Target.createTarget', { url: 'about:blank', browserContextId });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  await send('Page.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);

  const ev = async (js) => (await send('Runtime.evaluate',
    { expression: js, returnByValue: true, awaitPromise: true }, sessionId)).result.value;

  console.log('[1] 全新浏览器 · localStorage 初始 token =',
    await ev(`localStorage.getItem('xb_token')`));

  console.log('[2] 打开复制的网址：', TARGET);
  await send('Page.navigate', { url: TARGET }, sessionId);
  await sleep(4500);
  const after = await ev(`location.pathname + location.search + location.hash`);
  console.log('    → 实际落点：', after);
  console.log('    → 是否被要求登录(/login)：', after.startsWith('/login') ? '是 ✔（URL 未携带身份）' : '否 ✘（存在越权！）');

  // 反向对照：同一浏览器内"已登录"再打开同一 URL
  await send('Page.navigate', { url: 'http://127.0.0.1:8178/login' }, sessionId);
  await sleep(2500);
  const tok = await ev(`(async()=>{const r=await fetch('/api/auth/guest',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"username":"guest"}'});const d=await r.json();return d.token||''})()`);
  await ev(`localStorage.setItem('xb_token', ${JSON.stringify(tok)}); localStorage.setItem('xb_user','guest');`);
  await send('Page.navigate', { url: TARGET }, sessionId);
  await sleep(4500);
  const after2 = await ev(`location.pathname + location.hash`);
  console.log('[3] 同浏览器（带 token）打开同一网址 → 落点：', after2,
    after2.startsWith('/chat') ? '（直进，符合预期）' : '');

  await send('Target.disposeBrowserContext', { browserContextId });
  process.exit(0);
})().catch(e => { console.error('ERR', e); process.exit(1); });
