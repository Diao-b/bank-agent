const path = require('path');
const L = require('./_e2e_lib.js');
const BASE = 'http://127.0.0.1:8178';
const OUT = path.join(__dirname, '..', '_reports', 'ux_audit');
(async () => {
  const h = await L.launch({ width: 1440, height: 920 });
  try {
    await h.goto(BASE + '/login'); await L.sleep(900);
    await h.type('input[type="text"]', 'user1'); await h.type('input[type="password"]', '123456');
    await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/立即登录|登\s*录/.test(x.innerText||''));if(b)b.click();return 1})()`);
    await L.sleep(3200);
    for (const [w, hh, tag] of [[1440, 920, '1440'], [1180, 900, '1180'], [746, 800, '746']]) {
      await h.setViewport(w, hh);
      await h.eval(`location.hash = '#/bills'`); await L.sleep(2400);
      await h.screenshot(path.join(OUT, `fix8_bills_${tag}.png`));
      const m = await h.eval(`(()=>{const svg=[...document.querySelectorAll('svg')].pop();
        const t=svg.querySelector('text'); const sm=t.getScreenCTM();
        return JSON.stringify({doc:document.documentElement.clientWidth, chartW:Math.round(svg.getBoundingClientRect().width), scale:Number(sm.a.toFixed(2)), eff:Number((parseFloat(getComputedStyle(t).fontSize)*sm.a).toFixed(1))})})()`);
      console.log(tag, m);
    }
  } finally { await h.close(); process.exit(0); }
})();
