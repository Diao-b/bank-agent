/* E2：卡片冻结/解冻精确流程（冻结→状态立即变化→解冻还原，净副作用 0） */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/E2_freeze.log');

const states = `(() => {
  const cards = [...document.querySelectorAll('div')].filter(e => typeof e.className==='string'
    && e.className.includes('rounded-2xl') && e.className.includes('text-white') && e.className.includes('p-5'));
  return JSON.stringify(cards.map((c,i) => ({ i, t:(c.innerText||'').replace(/\\n+/g,' | ').slice(0,110), frozen:/grayscale/.test(c.className) })));
})()`;
// 全局按文字点按钮（取第一个可见的）
const clickCardBtn = (idx, text) => `(() => {
  const btns = [...document.querySelectorAll('button')].filter(b =>
    (b.innerText||'').replace(/\\s+/g,'')===${JSON.stringify(text.replace(/\s+/g, ''))} && b.offsetParent!==null);
  if(!btns.length) return JSON.stringify({found:0, avail:[...document.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean).slice(0,40)});
  btns[0].scrollIntoView({block:'center'}); btns[0].click();
  return JSON.stringify({found:btns.length, clicked:(btns[0].innerText||'').trim()});
})()`;
// 在"直接冻结"弹层里点确认（文案含 确认 且可见）
const confirmModal = `(() => {
  const ov = [...document.querySelectorAll('div')].filter(d => typeof d.className==='string' && d.className.includes('fixed inset-0') && /直接冻结|解冻|确认/.test(d.innerText||''));
  if(!ov.length) return JSON.stringify({err:'no modal'});
  const panel = ov[ov.length-1];
  const btns = [...panel.querySelectorAll('button')].filter(b=>b.offsetParent!==null);
  const conf = btns.find(b=>/确认|确定/.test((b.innerText||'').trim()));
  if(!conf) return JSON.stringify({err:'no confirm', all:btns.map(b=>(b.innerText||'').trim())});
  conf.click();
  return JSON.stringify({ clicked:(conf.innerText||'').trim(), all:btns.map(b=>(b.innerText||'').trim()) });
})()`;

(async () => {
  const h = await launch({ watchdogMs: 200000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/cards', { reload: false }); await sleep(4000);
  const before = JSON.parse(await h.eval(states)); log('before', JSON.stringify(before));
  // 找一张"使用中"的卡
  const normalIdx = before.findIndex(c => !c.frozen);
  log('normalIdx', normalIdx);
  if (normalIdx >= 0) {
    log('click freeze', await h.eval(clickCardBtn(normalIdx, '冻结')));
    await sleep(1200);
    await h.screenshot(SHOTS + '/E2-01-freeze-modal.png');
    log('confirm', await h.eval(confirmModal));
    await sleep(3000);
    const mid = JSON.parse(await h.eval(states)); log('after_freeze', JSON.stringify(mid));
    await h.screenshot(SHOTS + '/E2-02-after-freeze.png');
    // 解冻还原
    const frozenIdx = mid.findIndex(c => c.frozen);
    if (frozenIdx >= 0) {
      log('click unfreeze', await h.eval(clickCardBtn(frozenIdx, '解冻')));
      await sleep(1200);
      log('confirm unfreeze', await h.eval(confirmModal));
      await sleep(3000);
      log('after_unfreeze', await h.eval(states));
      await h.screenshot(SHOTS + '/E2-03-after-unfreeze.png');
    }
  }
  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
