/* 看板局部放大 + 计算样式探针（验证图表颜色确实走主题变量、侧栏文字不重叠） */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const log = H.makeLogger('_reports/_probe_dash.log');
const SHOT = 'D:/Users/Dwm/Desktop/agaent/bank-agent/_reports/shots/';

async function clip(h, file, x, y, w, hh, scale = 2) {
  const r = await h.b.send('Page.captureScreenshot', {
    format: 'png', captureBeyondViewport: true,
    clip: { x, y, width: w, height: hh, scale },
  }, h.sid);
  fs.writeFileSync(file, Buffer.from(r.data, 'base64'));
}

(async () => {
  const TOK = process.env.TOK;
  const h = await H.launch({ width: 1500, height: 1200, watchdogMs: 120000 });
  try {
    await h.setViewport(1500, 1200);
    await h.goto(H.BASE + '/login');
    await h.eval(`localStorage.setItem('xb_token', ${JSON.stringify(TOK)});
                  localStorage.setItem('xb_user','admin');
                  localStorage.removeItem('xb_admin_theme')`);
    await h.goto(H.BASE + '/admin');
    await h.waitForText('数据看板', 20000).catch(() => false);
    await H.sleep(3000);

    // --- 图表描边颜色（应分别是 var 解析后的真实色） ---
    const strokes = await h.eval(`(()=>{
      const out = [];
      document.querySelectorAll('#root svg').forEach((s, i) => {
        const p = s.querySelector('path[stroke]');
        const bar = s.querySelector('rect[fill]');
        out.push({ i, stroke: p ? getComputedStyle(p).stroke : null,
                   barFill: bar ? getComputedStyle(bar).fill : null });
      });
      return JSON.stringify(out);
    })()`);
    log('svg_strokes_dark', strokes);

    // --- 侧栏底部文字盒模型（查重叠） ---
    const navBox = await h.eval(`(()=>{
      const nav = document.querySelector('nav');
      const f = nav && nav.lastElementChild;
      if (!f) return 'no nav footer';
      const r = f.getBoundingClientRect();
      return JSON.stringify({ text: (f.innerText||'').replace(/\\n/g,' | '),
        box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
        fontSize: getComputedStyle(f).fontSize, lineHeight: getComputedStyle(f).lineHeight,
        color: getComputedStyle(f).color });
    })()`);
    log('nav_footer', navBox);

    // --- 顶栏文字颜色对比 ---
    const headColors = await h.eval(`(()=>{
      const hs = [...document.querySelectorAll('header button, header span')].map(e=>({
        t:(e.innerText||'').trim().slice(0,12), c:getComputedStyle(e).color, bg:getComputedStyle(e).backgroundColor}));
      return JSON.stringify(hs);
    })()`);
    log('header_colors_dark', headColors);

    await clip(h, SHOT + 'probe_nav_dark.png', 0, 0, 500, 760);
    await clip(h, SHOT + 'probe_msgchart_dark.png', 880, 430, 560, 200);

    // 切亮色
    await h.clickByText('暗色', { tag: 'button' });
    await H.sleep(1500);
    const strokes2 = await h.eval(`(()=>{
      const out = [];
      document.querySelectorAll('#root svg').forEach((s, i) => {
        const p = s.querySelector('path[stroke]');
        out.push({ i, stroke: p ? getComputedStyle(p).stroke : null });
      });
      return JSON.stringify(out);
    })()`);
    log('svg_strokes_light', strokes2);
    const headColors2 = await h.eval(`(()=>{
      const hs = [...document.querySelectorAll('header button, header span')].map(e=>({
        t:(e.innerText||'').trim().slice(0,12), c:getComputedStyle(e).color }));
      return JSON.stringify(hs);
    })()`);
    log('header_colors_light', headColors2);
    await clip(h, SHOT + 'probe_nav_light.png', 0, 0, 500, 760);
    await clip(h, SHOT + 'probe_donut_light.png', 200, 600, 760, 300);
    await clip(h, SHOT + 'probe_bottom_light.png', 150, 780, 1100, 400);
    await clip(h, SHOT + 'probe_bottom_dark.png', 150, 780, 1100, 400);
    log('RESULT', 'DONE');
  } catch (e) {
    log('ERROR', String(e && e.stack || e));
  } finally {
    clearTimeout(h._wd);
    try { await h.close(); } catch (_) {}
    process.exit(0);
  }
})();
