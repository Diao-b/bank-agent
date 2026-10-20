/* E6：刷新 /cards 验证页面态是否与库一致（证明"需刷新才对"）+ 尝试解冻 8631 还原 */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/E6.log');
const states = `(() => {
  const cards = [...document.querySelectorAll('div')].filter(e => typeof e.className==='string'
    && e.className.includes('rounded-2xl') && e.className.includes('text-white') && e.className.includes('p-5'));
  return JSON.stringify(cards.map((c,i) => ({ i, tail:(c.innerText||'').match(/尾号\\s*(\\d{4})/)?.[1], state:(c.innerText||'').match(/已冻结|使用中/)?.[0] })));
})()`;
const clickConfirm = `(() => {
  const ov=[...document.querySelectorAll('div')].filter(d=>typeof d.className==='string'&&d.className.includes('fixed inset-0'));
  for(let k=ov.length-1;k>=0;k--){const b=[...ov[k].querySelectorAll('button')].filter(x=>x.offsetParent!==null&&/确认/.test(x.innerText||''));if(b.length){b[0].click();return JSON.stringify({clicked:(b[0].innerText||'').trim()});}}
  return JSON.stringify({clicked:null});})()`;
// 按"最近下方"选择目标卡的操作按钮
const clickNear = (tail, text) => `(() => {
  const cards=[...document.querySelectorAll('div')].filter(e=>typeof e.className==='string'&&e.className.includes('rounded-2xl')&&e.className.includes('text-white')&&e.className.includes('p-5'));
  const card=cards.find(c=>(c.innerText||'').includes(${JSON.stringify(tail)}));
  if(!card) return JSON.stringify({err:'no card'});
  const cr=card.getBoundingClientRect();
  const btns=[...document.querySelectorAll('button')].filter(b=>(b.innerText||'').replace(/\\s+/g,'')===${JSON.stringify(text.replace(/\s+/g,''))}&&b.offsetParent!==null);
  const cand=btns.map(b=>({b,r:b.getBoundingClientRect()})).filter(o=>o.r.top>=cr.bottom-8).sort((a,b)=>(a.r.top-cr.bottom)-(b.r.top-cr.bottom));
  if(!cand.length) return JSON.stringify({err:'no near btn', btns:btns.length});
  cand[0].b.click(); return JSON.stringify({ok:true, dy:Math.round(cand[0].r.top-cr.bottom)});
})()`;

(async () => {
  const h = await launch({ watchdogMs: 180000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  // 全新加载 /cards
  await h.goto(BASE + '/chat#/cards', { reload: false }); await sleep(4000);
  log('after_fresh_load', await h.eval(states));
  await h.screenshot(SHOTS + '/E6-01-fresh-load.png');
  // 尝试解冻 8631
  log('click_unfreeze', await h.eval(clickNear('8631', '解冻')));
  await sleep(1000); log('u1', await h.eval(clickConfirm));
  await sleep(1600); log('u2', await h.eval(clickConfirm));
  await sleep(3500); log('after_unfreeze', await h.eval(states));
  await h.screenshot(SHOTS + '/E6-02-unfrozen.png');
  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
