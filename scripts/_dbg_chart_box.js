const L = require('./_e2e_lib.js');
const BASE = 'http://127.0.0.1:8178';
(async () => {
  const h = await L.launch();
  try {
    await h.goto(BASE + '/login'); await L.sleep(900);
    await h.type('input[type="text"]', 'user1'); await h.type('input[type="password"]', '123456');
    await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/立即登录|登\s*录/.test(x.innerText||''));if(b)b.click();return 1})()`);
    await L.sleep(3200);
    await h.eval(`location.hash = '#/bills'`); await L.sleep(2600);
    console.log(await h.eval(`(()=>{
      const out=[];
      document.querySelectorAll('svg').forEach(sv=>{
        const r=sv.getBoundingClientRect(); const vb=sv.getAttribute('viewBox');
        const t=sv.querySelector('text'); const m=t?t.getScreenCTM():null;
        out.push({vb, box:[Math.round(r.width),Math.round(r.height)], scale:m?Number(m.a.toFixed(3)):null,
                  parentW: sv.parentElement?Math.round(sv.parentElement.getBoundingClientRect().width):null});
      });
      return JSON.stringify(out,null,1);
    })()`));
  } finally { await h.close(); process.exit(0); }
})();
