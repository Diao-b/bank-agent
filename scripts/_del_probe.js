/* 诊断：在页面里用真实 fetch 打 DELETE /api/conversations/{id}，看返回什么。
   用法：node scripts/_del_probe.js <token> <convId>
*/
const ARGV = process.argv.slice(2).filter(a => !a.startsWith('--'));
const TOK = ARGV[0], CID = ARGV[1];
const BASE = 'http://127.0.0.1:8178', CDP = 'http://127.0.0.1:9222';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  const list = await (await fetch(CDP + '/json/list')).json();
  const target = list.filter(t => t.type === 'page').pop();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result); }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params = {}) => new Promise((resolve, reject) => { const mid = ++id; pending.set(mid, { resolve, reject }); ws.send(JSON.stringify({ id: mid, method, params })); });
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(5000);
  await send('Runtime.evaluate', { expression: `localStorage.setItem('xb_token', ${JSON.stringify(TOK)}); localStorage.setItem('xb_user','user1'); 'ok'` });
  await send('Page.navigate', { url: BASE + '/chat' });
  await sleep(6000);

  const r = await send('Runtime.evaluate', {
    expression: `(async () => {
      const t = localStorage.getItem('xb_token') || '';
      const out = { tokenLen: t.length, url: '/api/conversations/${CID}' };
      try {
        const res = await fetch('/api/conversations/${CID}', {
          method: 'DELETE',
          headers: { 'Authorization': 'Bearer ' + t, 'Content-Type': 'application/json' },
        });
        out.status = res.status;
        out.body = (await res.text()).slice(0, 200);
      } catch (e) { out.err = String(e); }
      return JSON.stringify(out);
    })()`,
    awaitPromise: true, returnByValue: true,
  });
  console.log('页面内 DELETE 结果:', r.result.value);
  ws.close();
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
