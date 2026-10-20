/* E3：干净的冻结/解冻（只针对 8631；日志含"点击后 1s / 3s"页面态，判断是否即时更新） */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/E3_freeze.log');

const states = `(() => {
  const cards = [...document.querySelectorAll('div')].filter(e => typeof e.className==='string'
    && e.className.includes('rounded-2xl') && e.className.includes('text-white') && e.className.includes('p-5'));
  return JSON.stringify(cards.map((c,i) => ({ i, tail:(c.innerText||'').match(/尾号\\s*(\\d{4})/)?.[1], state:(c.innerText||'').match(/已冻结|使用中/)?.[0], frozen:/grayscale/.test(c.className) })));
})()`;
const clickGlobal = (text) => `(() => {
  const btns = [...document.querySelectorAll('button')].filter(b => (b.innerText||'').replace(/\\s+/g,'')===${JSON.stringify(text.replace(/\s+/g,''))} && b.offsetParent!==null);
  if(!btns.length) return JSON.stringify({found:0});
  btns[0].scrollIntoView({block:'center'}); btns[0].click(); return JSON.stringify({found:btns.length});
})()`;
const confirmModal = `(() => {
  const ov = [...document.querySelectorAll('div')].filter(d => typeof d.className==='string' && d.className.includes('fixed inset-0') && /直接冻结|解冻|确认/.test(d.innerText||''));
  if(!ov.length) return JSON.stringify({err:'no modal'});
  const btns = [...ov[ov.length-1].querySelectorAll('button')].filter(b=>b.offsetParent!==null);
  const conf = btns.find(b=>/确认/.test((b.innerText||'').trim()));
  if(!conf) return JSON.stringify({err:'no confirm', all:btns.map(b=>(b.innerText||'').trim())});
  conf.click(); return JSON.stringify({clicked:(conf.innerText||'').trim()});
})()`;

(async () => {
  const h = await launch({ watchdogMs: 200000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/cards', { reload: false }); await sleep(4000);
  log('T0_loaded', await h.eval(states));
  await h.screenshot(SHOTS + '/E3-00-loaded.png');

  // 冻结
  log('click_freeze', await h.eval(clickGlobal('冻结')));
  await sleep(900); log('freeze_modal', await h.eval(confirmModal));
  await sleep(1000); log('T1_after_confirm_1s', await h.eval(states));
  await sleep(2500); log('T2_after_confirm_3s', await h.eval(states));
  await h.screenshot(SHOTS + '/E3-01-after-freeze.png');

  await sleep(2000);
  // 解冻还原
  log('click_unfreeze', await h.eval(clickGlobal('解冻')));
  await sleep(900); log('unfreeze_modal', await h.eval(confirmModal));
  await sleep(3000); log('T3_after_unfreeze', await h.eval(states));
  await h.screenshot(SHOTS + '/E3-02-after-unfreeze.png');

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
