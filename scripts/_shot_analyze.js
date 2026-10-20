/* 分析用户截图里「看起来空白」的卡片：是**真空白**（只有背景，maxL≈15-25）
   还是**被压暗**（有文字，maxL≈60-100）→ 区分「渲染 bug」与「非当前卡降暗设计」。
   用法：node scripts/_shot_analyze.js <imgUrlPath> [CDP_PORT]
*/
const CDP_PORT = process.argv[3] || '9222';
const CDP = 'http://127.0.0.1:' + CDP_PORT;
const IMG = process.argv[2] || '/_probe_shot.jpg';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function pickPageTarget() {
  for (let i = 0; i < 24; i++) {
    try {
      const list = await (await fetch(CDP + '/json/list')).json();
      const p = list.filter(t => t.type === 'page').pop();
      if (p && p.webSocketDebuggerUrl) return p;
    } catch (_) {}
    await sleep(500);
  }
  throw new Error('no CDP page');
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
async function evalJSON(h, expr) {
  const r = await h.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}

const ANALYZE = (url) => `(async () => {
  const img = new Image(); img.src = ${JSON.stringify(url)};
  await img.decode();
  const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
  const g = c.getContext('2d'); g.drawImage(img, 0, 0);
  const d = g.getImageData(0, 0, c.width, c.height).data;
  const L = (i) => 0.2126*d[i] + 0.7152*d[i+1] + 0.0722*d[i+2];
  const bands = { b0_20:0, b20_50:0, b50_90:0, b90_150:0, b150_255:0 };
  for (let i = 0; i < d.length; i += 4) { const l = L(i);
    if (l < 20) bands.b0_20++; else if (l < 50) bands.b20_50++; else if (l < 90) bands.b50_90++;
    else if (l < 150) bands.b90_150++; else bands.b150_255++; }
  const COLS = 32, ROWS = 18;
  const cw = Math.floor(c.width / COLS), ch = Math.floor(c.height / ROWS);
  const maxmap = [];
  for (let r = 0; r < ROWS; r++) { const row = [];
    for (let cc = 0; cc < COLS; cc++) {
      const px = Math.min(c.width - 1, cc*cw), py = Math.min(c.height - 1, r*ch);
      const w = Math.min(cw, c.width - px), h = Math.min(ch, c.height - py);
      const cd = g.getImageData(px, py, w, h).data;
      let mx = 0; for (let k = 0; k < cd.length; k += 4) { const l = 0.2126*cd[k]+0.7152*cd[k+1]+0.0722*cd[k+2]; if (l > mx) mx = l; }
      row.push(Math.round(mx));
    }
    maxmap.push(row);
  }
  return JSON.stringify({ w: c.width, h: c.height, bands, maxmap });
})()`;

(async () => {
  const t = await pickPageTarget();
  const h = cdp(t.webSocketDebuggerUrl); await h.ready;
  await h.send('Runtime.enable');
  const res = await evalJSON(h, ANALYZE(IMG));
  const o = JSON.parse(res);
  console.log('size =', o.w + 'x' + o.h);
  console.log('histogram (pixels by luminance) =', JSON.stringify(o.bands));
  const total = Object.values(o.bands).reduce((a, b) => a + b, 0);
  for (const k of Object.keys(o.bands)) console.log('   ' + k.padEnd(8) + (100 * o.bands[k] / total).toFixed(1) + '%');
  console.log('per-cell MAX luminance map (32x18):');
  for (const row of o.maxmap) console.log('   ' + row.map(v => String(v).padStart(3)).join(' '));
  h.close(); process.exit(0);
})().catch(e => { console.error('FATAL', e); process.exit(1); });
