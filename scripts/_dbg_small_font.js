/* 诊断：/bills 页 <12px 的文本节点到底是谁（区分 HTML 与缩放后的 SVG text） */
const L = require('./_e2e_lib.js');
const BASE = 'http://127.0.0.1:8178';
(async () => {
  const h = await L.launch();
  try {
    await h.goto(BASE + '/login');
    await L.sleep(900);
    await h.type('input[type="text"]', 'user1');
    await h.type('input[type="password"]', '123456');
    await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/立即登录|登\\s*录/.test(x.innerText||''));if(b)b.click();return 1})()`);
    await L.sleep(3200);
    await h.eval(`location.hash = '#/bills'`);
    await L.sleep(2600);
    const out = await h.eval(`(()=>{
      const res = [];
      document.querySelectorAll('body *').forEach(e => {
        if (!(e.offsetParent || e.getClientRects().length)) return;
        const own = [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
        if (!own) return;
        const fs = parseFloat(getComputedStyle(e).fontSize);
        if (fs >= 12) return;
        let scale = 1;
        try { if (e.ownerSVGElement || e.tagName === 'text') { const m = e.getScreenCTM(); scale = m ? m.a : 1; } } catch (err) {}
        const r = e.getBoundingClientRect();
        res.push({ tag: e.tagName, fs, scale: Number(scale.toFixed(2)), eff: Number((fs * scale).toFixed(1)),
                   boxH: Number(r.height.toFixed(1)), txt: (e.textContent||'').trim().slice(0,16),
                   cls: (e.getAttribute('class')||'').slice(0,30), html: e.outerHTML.slice(0, 110) });
      });
      return JSON.stringify(res, null, 1);
    })()`);
    console.log(out);
  } finally { await h.close(); process.exit(0); }
})();
