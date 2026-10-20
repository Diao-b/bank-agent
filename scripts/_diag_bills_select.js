/* 诊断：账单页「交易流水明细」筛选下拉的渲染实况（浅色主题）
 * 目的：确认用户说的"下选符号"到底是什么 —— 是缺失的箭头（appearance-none 后没补自定义图标），
 *       还是原生三角没被隐藏。
 * 输出：computedStyle + 是否有自定义 chevron 元素 + 3x 放大截图。
 * 用法：node scripts/_diag_bills_select.js <token>
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');

(async () => {
  const h = await H.launch({ width: 1440, height: 900, watchdogMs: 120000 });
  try {
    await h.goto(H.BASE + '/');
    await h.eval("localStorage.clear(); localStorage.setItem('xb_user','user1'); localStorage.setItem('xb_theme_user1','day')");
    await h.setToken(process.env.XB_TOKEN || process.argv[2] || '');
    await h.goto(H.BASE + '/chat', { reload: true });
    console.log('hash0 =', await h.eval('location.hash'), '| theme =', await h.eval('document.documentElement.dataset.theme'));
    await h.eval("location.hash = '#/bills'");
    await H.sleep(2800);
    console.log('hash =', await h.eval('location.hash'));
    console.log('theme =', await h.eval('document.documentElement.dataset.theme'));
    console.log('--- body 前 400 字 ---');
    console.log(String(await h.eval('(document.body&&document.body.innerText||"").slice(0,400)')).replace(/\n/g, ' | '));
    console.log('--- 可见按钮 ---');
    console.log(await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      return [...document.querySelectorAll('button')].filter(vis)
        .map(b => (b.innerText || '').trim()).filter(Boolean).slice(0, 30);
    })()`));

    // 展开所有折叠区（页面上有多个「展开查看」；全部点开最简单可靠）
    const opened = await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const btns = [...document.querySelectorAll('button')].filter(vis)
        .filter(x => /展开查看/.test(x.innerText || ''));
      btns.forEach(b => b.click());
      return 'clicked ' + btns.length;
    })()`);
    console.log('expand =', opened);
    await H.sleep(1200);

    const info = await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const sels = [...document.querySelectorAll('select')].filter(vis);
      return sels.map(s => {
        const cs = getComputedStyle(s);
        const wrap = s.parentElement;
        const kids = [...(wrap ? wrap.children : [])].map(k => k.tagName + '.' + String(k.getAttribute('class') || '').slice(0, 70));
        const r = s.getBoundingClientRect();
        return {
          text: (s.options[s.selectedIndex] || {}).text || '',
          appearance: cs.appearance + ' / ' + cs.webkitAppearance,
          bgImage: cs.backgroundImage,
          padRight: cs.paddingRight,
          overflow: cs.overflow,
          box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
          wrapHtmlTail: wrap ? wrap.innerHTML.slice(-120) : '',
          wrapKids: kids,
        };
      });
    })()`);
    console.log(JSON.stringify(info, null, 2));

    // 放大截图：整个筛选条
    const box = await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const sels = [...document.querySelectorAll('select')].filter(vis);
      if (!sels.length) return null;
      const a = sels[0].getBoundingClientRect();
      const b = sels[sels.length - 1].getBoundingClientRect();
      const pad = 22;
      return { x: a.x - pad, y: a.y - pad, width: (b.right - a.left) + pad * 2, height: a.height + pad * 2 };
    })()`);
    console.log('clip =', JSON.stringify(box));
    if (box) {
      const r = await h.b.send('Page.captureScreenshot', {
        format: 'png',
        clip: { x: Math.max(0, box.x), y: Math.max(0, box.y), width: box.width, height: box.height, scale: 4 },
      }, h.sid);
      fs.writeFileSync('_reports/_diag_bills_select.png', Buffer.from(r.data, 'base64'));
      console.log('saved _reports/_diag_bills_select.png');
    }
  } catch (e) {
    console.log('ERR', e && e.stack || e);
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(0);
  }
})();
