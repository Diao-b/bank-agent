/* 全 Tab 明暗主题巡检：逐个 Tab 截图，并做"文字/背景色过近"的对比度粗筛 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const log = H.makeLogger('_reports/_sweep_theme.log');
const SHOT = 'D:/Users/Dwm/Desktop/agaent/bank-agent/_reports/shots/';
const PAGES = ['数据看板', '风险监控', '卡密管理', '用户管理', '风控参数', '理财产品',
               '知识库', '系统公告', '操作审计', '验证码记录', '数字人形象'];

async function clip(h, file, x, y, w, hh, scale = 1.6) {
  const r = await h.b.send('Page.captureScreenshot', {
    format: 'png', captureBeyondViewport: true, clip: { x, y, width: w, height: hh, scale },
  }, h.sid);
  fs.writeFileSync(file, Buffer.from(r.data, 'base64'));
}

/* 在整页里找"前景≈背景"的低对比文本（粗筛，仅报可疑项） */
const CONTRAST_JS = `(() => {
  const lum = c => { const m = (c||'').match(/(\\d+)[,\\s]+(\\d+)[,\\s]+(\\d+)/); if (!m) return null;
    const f = v => { v = (+v)/255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
    return 0.2126*f(m[1]) + 0.7152*f(m[2]) + 0.0722*f(m[3]); };
  const bgOf = el => { let e = el; while (e) { const b = getComputedStyle(e).backgroundColor;
      if (b && b !== 'rgba(0, 0, 0, 0)' && b !== 'transparent') return b; e = e.parentElement; } return 'rgb(255,255,255)'; };
  const bad = [];
  document.querySelectorAll('#root *').forEach(el => {
    if (!el.innerText || el.children.length) return;
    const t = el.innerText.trim(); if (!t || t.length > 40) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity < 0.5) return;
    const l1 = lum(cs.color), l2 = lum(bgOf(el));
    if (l1 == null || l2 == null) return;
    const ratio = (Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05);
    if (ratio < 1.7) bad.push({ t: t.slice(0,24), c: cs.color, bg: bgOf(el), r: +ratio.toFixed(2) });
  });
  return JSON.stringify(bad.slice(0, 12));
})()`;

(async () => {
  const TOK = process.env.TOK;
  const h = await H.launch({ width: 1500, height: 1100, watchdogMs: 240000 });
  try {
    await h.setViewport(1500, 1100);
    await h.goto(H.BASE + '/login');
    await h.eval(`localStorage.setItem('xb_token', ${JSON.stringify(TOK)});
                  localStorage.setItem('xb_user','superadmin')`);
    for (const theme of ['dark', 'light']) {
      await h.eval(`localStorage.setItem('xb_admin_theme', ${JSON.stringify(theme)})`);
      await h.goto(H.BASE + '/admin', { reload: true });
      await h.waitForText('数据看板', 20000).catch(() => false);
      await H.sleep(1500);
      log('theme_applied', theme + ' :: ' + await h.eval('document.documentElement.className'));
      for (const p of PAGES) {
        const r = await h.clickByText(p, { tag: 'button' });
        await H.sleep(1600);
        const shot = SHOT + 'tab_' + theme + '_' + PAGES.indexOf(p) + '_' + p + '.png';
        await clip(h, shot, 200, 0, 1300, 1000);
        const bad = await h.eval(CONTRAST_JS);
        log('page', theme + ' | ' + p + ' | click=' + r + ' | lowContrast=' + bad);
      }
    }
    log('RESULT', 'DONE');
  } catch (e) {
    log('ERROR', String(e && e.stack || e));
  } finally {
    clearTimeout(h._wd);
    try { await h.close(); } catch (_) {}
    process.exit(0);
  }
})();
