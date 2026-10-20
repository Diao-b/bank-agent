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
      const svg=document.querySelectorAll('svg')[1]; // LineChart
      const chain=[]; let n=svg;
      for(let i=0;i<6&&n;i++){ const r=n.getBoundingClientRect();
        chain.push({tag:n.tagName, cls:(n.getAttribute('class')||'').slice(0,60), w:Math.round(r.width), h:Math.round(r.height)});
        n=n.parentElement; }
      return JSON.stringify({vw:innerWidth, doc:document.documentElement.clientWidth, chain}, null, 1);
    })()`));
  } finally { await h.close(); process.exit(0); }
})();
