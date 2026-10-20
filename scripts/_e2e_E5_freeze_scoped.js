/* E5：按卡号定位冻结按钮（8631），完整两步确认，观察页面+库 */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/E5_freeze.log');

const states = `(() => {
  const cards = [...document.querySelectorAll('div')].filter(e => typeof e.className==='string'
    && e.className.includes('rounded-2xl') && e.className.includes('text-white') && e.className.includes('p-5'));
  return JSON.stringify(cards.map((c,i) => ({ i, tail:(c.innerText||'').match(/尾号\\s*(\\d{4})/)?.[1], state:(c.innerText||'').match(/已冻结|使用中/)?.[0] })));
})()`;
// 在含 targetTail 的卡片所在容器里，点指定文字的按钮
const clickInCard = (tail, text) => `(() => {
  const cards = [...document.querySelectorAll('div')].filter(e => typeof e.className==='string'
    && e.className.includes('rounded-2xl') && e.className.includes('text-white') && e.className.includes('p-5'));
  const card = cards.find(c => (c.innerText||'').includes(${JSON.stringify(tail)}));
  if(!card) return JSON.stringify({err:'no card '+${JSON.stringify(tail)}});
  // 向上找同时含"冻结/解冻"按钮的祖先
  let node = card, btn=null;
  for (let k=0;k<8 && node;k++){
    const bs=[...node.querySelectorAll('button')].filter(b=>(b.innerText||'').replace(/\\s+/g,'')===${JSON.stringify(text.replace(/\s+/g,''))} && b.offsetParent!==null);
    if(bs.length){ btn=bs[0]; break; }
    node=node.parentElement;
  }
  if(!btn) return JSON.stringify({err:'no button', text:${JSON.stringify(text)}});
  btn.click(); return JSON.stringify({ok:true});
})()`;
const clickConfirm = `(() => {
  const ov = [...document.querySelectorAll('div')].filter(d => typeof d.className==='string' && d.className.includes('fixed inset-0'));
  for (let k=ov.length-1;k>=0;k--){
    const b=[...ov[k].querySelectorAll('button')].filter(x=>x.offsetParent!==null && /确认/.test(x.innerText||''));
    if(b.length){ b[0].click(); return JSON.stringify({clicked:(b[0].innerText||'').trim()}); }
  }
  return JSON.stringify({clicked:null});
})()`;

(async () => {
  const h = await launch({ watchdogMs: 200000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/cards', { reload: false }); await sleep(4000);
  log('T0', await h.eval(states));
  log('click_freeze_8631', await h.eval(clickInCard('8631', '冻结')));
  await sleep(1000); log('c1', await h.eval(clickConfirm));
  await sleep(1600); log('c2', await h.eval(clickConfirm));
  await sleep(1500); log('T1', await h.eval(states));
  await sleep(4500); log('T2', await h.eval(states));
  await h.screenshot(SHOTS + '/E5-01-frozen.png');
  // 还原：解冻 8631
  log('click_unfreeze_8631', await h.eval(clickInCard('8631', '解冻')));
  await sleep(1000); log('u1', await h.eval(clickConfirm));
  await sleep(1600); log('u2', await h.eval(clickConfirm));
  await sleep(4000); log('T3', await h.eval(states));
  await h.screenshot(SHOTS + '/E5-02-unfrozen.png');
  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
