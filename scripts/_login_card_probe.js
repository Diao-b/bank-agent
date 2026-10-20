/* 登录页旋转卡片「背面空白」诊断 v2（GPU 真机 + 像素采样 + 真实环形采样）
   1) 克隆真实 .fcard → 受控 3D 容器：rotateY(0)/180 × (dim/非 dim)，量亮度分布
   2) 冻结真实舞台 .stage-ring 于多个角度，量每张卡是否「真的空白」(maxL≈背景)
   用法：node scripts/_login_card_probe.js [CDP_PORT]
*/
const fs = require('fs');
const path = require('path');

const CDP_PORT = process.argv[2] || '9222';
const DPR = parseFloat(process.argv[3] || '1');
const CDP = 'http://127.0.0.1:' + CDP_PORT;
const BASE = 'http://127.0.0.1:8178';
const OUT = path.join(__dirname, '..', '_reports', 'login_probe');
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
  throw new Error('no CDP page target on ' + CDP);
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
  const ready = new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id; pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  return { ready, send, close: () => ws.close() };
}

const BUILD_HOST = `(() => {
  const old = document.getElementById('__probe_host'); if (old) old.remove();
  const src = document.querySelector('.stage-ring .fcard') || document.querySelector('.fcard');
  if (!src) return 'NO_CARD';
  const host = document.createElement('div');
  host.id = '__probe_host';
  host.style.cssText = 'position:fixed;left:0;top:0;width:1280px;height:800px;background:#06080c;z-index:2147483647;perspective:1200px;perspective-origin:50% 50%';
  function mk(angle, x, y, dim, label) {
    const c = src.cloneNode(true);
    c.style.cssText = 'position:absolute;left:' + x + 'px;top:' + y + 'px;margin:0;transform:rotateY(' + angle + 'deg)';
    c.setAttribute('data-probe', label);
    c.querySelectorAll('.fcard-inner,.fcard-back').forEach(f => {
      f.classList.remove('is-dim', 'is-active');
      if (dim) f.classList.add('is-dim');
    });
    return c;
  }
  host.appendChild(mk(0,   80, 180, false, 'front_clean'));
  host.appendChild(mk(180, 400, 180, false, 'back_clean'));
  host.appendChild(mk(0,   80, 420, true,  'front_dim'));
  host.appendChild(mk(180, 400, 420, true,  'back_dim'));
  document.body.appendChild(host);
  return 'OK';
})()`;

const FREEZE = (deg) => `(() => {
  let s = document.getElementById('__freeze'); if (!s) { s = document.createElement('style'); s.id='__freeze'; document.head.appendChild(s); }
  s.textContent = '.stage-ring{transform:rotateY(${deg}deg) !important;}';
  return 'FROZEN@${deg}';
})()`;

const RM_HOST = `(() => { const h=document.getElementById('__probe_host'); if(h) h.remove(); return 'REMOVED'; })()`;

// 采样：#probe_host 内的克隆卡 或 .stage-ring 内的真实卡
const SAMPLE = (b64, sel) => `(async () => {
  const img = new Image();
  img.src = 'data:image/png;base64,${b64}';
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  const g = c.getContext('2d');
  g.drawImage(img, 0, 0);
  const out = [];
  document.querySelectorAll(${JSON.stringify(sel)}).forEach((el, idx) => {
    const r = el.getBoundingClientRect();
    const x = Math.max(0, Math.round(r.x + 8));
    const y = Math.max(0, Math.round(r.y + 8));
    const w = Math.min(c.width - x, Math.round(r.width - 16));
    const h = Math.min(c.height - y, Math.round(r.height - 16));
    if (w <= 2 || h <= 2) { out.push({ label: el.getAttribute('data-probe') || ('card'+idx), offscreen: true }); return; }
    const d = g.getImageData(x, y, w, h).data;
    let maxL = 0, sumL = 0, total = 0, b35 = 0, b70 = 0, b110 = 0;
    for (let i = 0; i < d.length; i += 4) {
      const L = 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2];
      sumL += L; total++;
      if (L > maxL) maxL = L;
      if (L > 35) b35++; if (L > 70) b70++; if (L > 110) b110++;
    }
    out.push({ label: el.getAttribute('data-probe') || ('card' + idx),
      w, h, maxL: Math.round(maxL), avgL: +(sumL / total).toFixed(1), b35, b70, b110 });
  });
  return JSON.stringify(out);
})()`;

const RENDERER = `(() => {
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return 'NO_WEBGL';
    const e = gl.getExtension('WEBGL_debug_renderer_info');
    return e ? gl.getParameter(e.UNMASKED_RENDERER_WEBGL) : 'UNKNOWN';
  } catch (err) { return 'ERR:' + err.message; }
})()`;

async function evalJSON(h, expr) {
  const r = await h.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}
async function shotSample(h, sel, tag) {
  const shot = await h.send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, tag + '.png'), Buffer.from(shot.data, 'base64'));
  const res = await evalJSON(h, SAMPLE(shot.data, sel));
  try { return JSON.parse(res); } catch (_) { return []; }
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const target = await pickPageTarget();
  const h = cdp(target.webSocketDebuggerUrl);
  await h.ready;
  await h.send('Page.enable'); await h.send('Runtime.enable');
  await h.send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 800, deviceScaleFactor: DPR, mobile: false });
  await h.send('Page.navigate', { url: BASE + '/login.html' });
  await sleep(3500);

  console.log('[GPU]', await evalJSON(h, RENDERER));

  // --- 1) 克隆卡对照 ---
  console.log('[probe] host =', await evalJSON(h, BUILD_HOST));
  await sleep(500);
  const clones = await shotSample(h, '#__probe_host .fcard', 'clones');
  console.log('[clones]');
  for (const r of clones) console.log('   ', JSON.stringify(r));
  await evalJSON(h, RM_HOST);

  // --- 2) 真实环形：多角度冻结 ---
  console.log('[ring] freeze sweep (maxL<20 视为“真空白”)');
  for (const deg of [0, 45, 90, 135, 180, 225, 270, 315]) {
    await evalJSON(h, FREEZE(deg));
    await sleep(200);
    const rows = await shotSample(h, '.stage-ring .fcard', 'ring_' + deg);
    const blanks = rows.filter(r => !r.offscreen && r.maxL < 20).map(r => r.label);
    const summary = rows.filter(r => !r.offscreen).map(r => r.label + ':max' + r.maxL + '/avg' + r.avgL).join('  ');
    console.log('  deg=' + String(deg).padStart(3) + ' blanks=' + (blanks.length ? blanks.join(',') : 'none'));
    console.log('      ' + summary);
  }

  // --- 3) 真实环形：动画中采样（will-change 合成路径，与冻结态不同） ---
  await evalJSON(h, `(() => { const s = document.getElementById('__freeze'); if (s) s.textContent = ''; return 'unfrozen'; })()`);
  console.log('[anim] sampling while ring animates (compositor path)');
  let badFrames = [];
  for (let f = 0; f < 18; f++) {
    await sleep(220);
    const shot = await h.send('Page.captureScreenshot', { format: 'png' });
    const rows = JSON.parse(await evalJSON(h, SAMPLE(shot.data, '.stage-ring .fcard')));
    const vis = rows.filter(r => !r.offscreen);
    const blanks = vis.filter(r => r.maxL < 20);
    const minMax = vis.length ? Math.min(...vis.map(r => r.maxL)) : -1;
    console.log('  frame' + String(f).padStart(2) + '  minMaxL=' + minMax + '  blanks=' + blanks.length +
      (blanks.length ? '  [' + blanks.map(b => b.label + '/max' + b.maxL + '/w' + b.w + 'x' + b.h).join(', ') + ']' : ''));
    if (blanks.length) {
      fs.writeFileSync(path.join(OUT, 'anim_f' + f + '.png'), Buffer.from(shot.data, 'base64'));
      badFrames.push(f);
    }
  }
  console.log('[anim] frames with true-blank cards:', badFrames.length, badFrames.length ? '(saved anim_f*.png)' : '');

  h.close();
  process.exit(0);
})().catch(e => { console.error('FATAL', e); process.exit(1); });
