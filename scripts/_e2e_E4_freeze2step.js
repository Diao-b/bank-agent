/* E4：冻结两步确认完整走完 → 观察页面即时性 → 解冻两步还原 */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/E4_freeze.log');

const states = `(() => {
  const cards = [...document.querySelectorAll('div')].filter(e => typeof e.className==='string'
    && e.className.includes('rounded-2xl') && e.className.includes('text-white') && e.className.includes('p-5'));
  return JSON.stringify(cards.map((c,i) => ({ i, tail:(c.innerText||'').match(/尾号\\s*(\\d{4})/)?.[1], state:(c.innerText||'').match(/已冻结|使用中/)?.[0] })));
})()`;
const clickGlobal = (text) => `(() => {
  const btns = [...document.querySelectorAll('button')].filter(b => (b.innerText||'').replace(/\\s+/g,'')===${JSON.stringify(text.replace(/\s+/g,''))} && b.offsetParent!==null);
  if(!btns.length) return JSON.stringify({found:0});
  btns[0].click(); return JSON.stringify({found:btns.length});
})()`;
// 点弹层里可见的「确认*」按钮
const clickConfirm = `(() => {
  const ov = [...document.querySelectorAll('div')].filter(d => typeof d.className==='string' && d.className.includes('fixed inset-0'));
  for (let k = ov.length-1; k>=0; k--) {
    const btns = [...ov[k].querySelectorAll('button')].filter(b=>b.offsetParent!==null && /确认/.test((b.innerText||'')));
    if (btns.length) { btns[0].click(); return JSON.stringify({clicked:(btns[0].innerText||'').trim()}); }
  }
  return JSON.stringify({clicked:null});
})()`;

(async () => {
  const h = await launch({ watchdogMs: 200000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/cards', { reload: false }); await sleep(4000);
  log('T0', await h.eval(states));

  // 冻结：点冻结 → 二次确认
  log('click_freeze', await h.eval(clickGlobal('冻结')));
  await sleep(1000); log('freeze_confirm1', await h.eval(clickConfirm));
  await sleep(1500); log('freeze_confirm2', await h.eval(clickConfirm));
  await sleep(1200); log('T1_after1.2s', await h.eval(states));
  await sleep(2500); log('T2_after3.7s', await h.eval(states));
  await h.screenshot(SHOTS + '/E4-01-frozen.png');

  await sleep(1500);
  // 解冻还原
  log('click_unfreeze', await h.eval(clickGlobal('解冻')));
  await sleep(1000); log('unfreeze_confirm1', await h.eval(clickConfirm));
  await sleep(1500); log('unfreeze_confirm2', await h.eval(clickConfirm));
  await sleep(3000); log('T3_after_unfreeze', await h.eval(states));
  await h.screenshot(SHOTS + '/E4-02-unfrozen.png');

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
