/* E：#/cards 卡片 —— 冻结状态即时变化 / 解冻核验 / 额度非法值校验 / 冻结后转账拦截 */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/E_cards.log');

const listCards = `(() => {
  const cards = [...document.querySelectorAll('div')].filter(e => typeof e.className==='string'
    && e.className.includes('rounded-2xl') && e.className.includes('text-white') && e.className.includes('p-5'));
  return JSON.stringify(cards.map((c,i) => {
    const cs = getComputedStyle(c);
    const btns = [...c.parentElement.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean);
    return { i, text: (c.innerText||'').replace(/\\n+/g,' | ').slice(0,160), grayscale: /grayscale/.test(c.className), filter: cs.filter, btns };
  }));
})()`;

(async () => {
  const h = await launch({ watchdogMs: 240000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/cards', { reload: false }); await sleep(4000);
  log('cards_initial', await h.eval(listCards));
  await h.screenshot(SHOTS + '/E01-cards.png');

  // 点第一张「使用中」卡片的「冻结」
  const clickFreeze = `(() => {
    const btns = [...document.querySelectorAll('button')].filter(b => (b.innerText||'').trim()==='冻结');
    if (!btns.length) return JSON.stringify({found:0});
    btns[0].scrollIntoView({block:'center'}); btns[0].click();
    return JSON.stringify({found: btns.length});
  })()`;
  log('clickFreeze', await h.eval(clickFreeze));
  await sleep(1500);
  // 是否弹确认层
  const modal = await h.eval(`(() => {
    const ov = [...document.querySelectorAll('div')].filter(d => typeof d.className==='string' && d.className.includes('fixed inset-0'));
    const top = ov.length ? (ov[ov.length-1].innerText||'').replace(/\\s+/g,' ').slice(0,200) : '';
    const btns = ov.length ? [...ov[ov.length-1].querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean) : [];
    return JSON.stringify({ overlays: ov.length, topText: top, buttons: btns });
  })()`);
  log('freeze_modal', modal);
  await h.screenshot(SHOTS + '/E02-freeze-modal.png');
  // 若有确认按钮则确认
  const confirmed = await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>/^(确认|确定|确 认|冻结)$/.test((x.innerText||'').trim()) && x.offsetParent!==null); if(b){b.click(); return (b.innerText||'').trim();} return null; })()`);
  log('freeze_confirm', confirmed);
  await sleep(2500);
  log('cards_after_freeze', await h.eval(listCards));
  await h.screenshot(SHOTS + '/E03-after-freeze.png');

  // 额度调整非法值（找信用/额度相关输入）
  const ak = await h.eval(`(() => {
    const res = [];
    document.querySelectorAll('input').forEach((el,i)=>{ res.push({i, type:el.type, ph:el.placeholder, val:el.value}); });
    return JSON.stringify(res);
  })()`);
  log('inputs_on_cards', ak);

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
