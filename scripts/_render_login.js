/* 以 1920x907（与用户截图同尺寸）渲染当前 login.html，冻结舞台于指定角度，
   截图存 web/_render_shot.png，供 _shot_analyze.js 与用户截图做同格对照。
   用法：node scripts/_render_login.js [CDP_PORT] [DEG]
*/
const fs = require('fs');
const path = require('path');
const CDP_PORT = process.argv[2] || '9222';
const DEG = process.argv[3] || '0';
const CDP = 'http://127.0.0.1:' + CDP_PORT;
const BASE = 'http://127.0.0.1:8178';
const OUTWEB = path.join(__dirname, '..', 'web', '_render_shot.png');
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function pickPageTarget() {
  for (let i = 0; i < 24; i++) {
    try { const l = await (await fetch(CDP + '/json/list')).json();
      const p = l.filter(t => t.type === 'page').pop(); if (p && p.webSocketDebuggerUrl) return p; } catch (_) {}
    await sleep(500);
  } throw new Error('no CDP page');
}
function cdp(wsUrl) {
  const ws = new WebSocket(wsUrl); let id = 0; const pending = new Map();
  ws.addEventListener('message', (ev) => { const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const { resolve, reject } = pending.get(m.id); pending.delete(m.id);
      m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result); } });
  const ready = new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id; pending.set(mid, { resolve, reject }); ws.send(JSON.stringify({ id: mid, method, params })); });
  return { ready, send, close: () => ws.close() };
}
async function ev(h, expr) {
  const r = await h.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}
(async () => {
  const t = await pickPageTarget(); const h = cdp(t.webSocketDebuggerUrl); await h.ready;
  await h.send('Page.enable'); await h.send('Runtime.enable');
  await h.send('Emulation.setDeviceMetricsOverride', { width: 1920, height: 907, deviceScaleFactor: 1, mobile: false });
  await h.send('Page.navigate', { url: BASE + '/login.html' });
  await sleep(3500);
  await ev(h, `(() => { let s=document.getElementById('__freeze'); if(!s){s=document.createElement('style');s.id='__freeze';document.head.appendChild(s);} s.textContent='.stage-ring{transform:rotateY(${DEG}deg) !important;}'; return 'ok'; })()`);
  await sleep(400);
  const shot = await h.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(OUTWEB, Buffer.from(shot.data, 'base64'));
  console.log('saved', OUTWEB, 'deg=' + DEG);
  h.close(); process.exit(0);
})().catch(e => { console.error('FATAL', e); process.exit(1); });
