/* B2：写操作确认卡 + 流转（确认/取消按钮是否出现、点了有没有反应） */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/B2_flow.log');

async function send(h, text) {
  await h.eval(`(() => { const el=document.querySelector('textarea'); if(!el) return false;
    const s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
    s.call(el, ${JSON.stringify(text)}); el.dispatchEvent(new Event('input',{bubbles:true})); return true; })()`);
  await sleep(350);
  return await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x => (x.innerText||'').replace(/\\s/g,'')==='发送'); if(b){ b.click(); return true; } return false; })()`);
}
async function settle(h, timeout = 70000) {
  const t0 = Date.now(); let saw = false, last = '', stable = 0;
  while (Date.now() - t0 < timeout) {
    const t = await h.bodyText();
    if (/小微正在输入/.test(t)) saw = true;
    const cur = await h.eval(`(() => { const main=document.querySelector('main')||document.body; const bs=[...main.querySelectorAll('div')].filter(d=>typeof d.className==='string'&&/rounded-2xl/.test(d.className)&&(d.innerText||'').trim().length>3); const leaf=bs.filter(d=>!bs.some(o=>o!==d&&d.contains(o))); return leaf.length? (leaf[leaf.length-1].innerText||'') : ''; })()`);
    if (cur === last && cur) { stable++; if (saw && stable >= 4) return { done: true, sawTyping: saw, ms: Date.now()-t0 }; }
    else { stable = 0; last = cur; }
    await sleep(700);
  }
  return { done: false, sawTyping: saw, ms: Date.now()-t0 };
}
async function snapshot(h) {
  return await h.eval(`(() => {
    const main = document.querySelector('main') || document.body;
    const bs = [...main.querySelectorAll('div')].filter(d=>typeof d.className==='string'&&/rounded-2xl/.test(d.className)&&(d.innerText||'').trim().length>3);
    const leaf = bs.filter(d=>!bs.some(o=>o!==d&&d.contains(o)));
    const btns = [...main.querySelectorAll('button')].map(b=>(b.innerText||'').replace(/\\s+/g,' ').trim()).filter(t=>t && t.length<20);
    const overlays = [...document.querySelectorAll('div')].filter(d=>typeof d.className==='string' && d.className.includes('fixed inset-0')).map(d=>(d.innerText||'').replace(/\\s+/g,' ').slice(0,120));
    return JSON.stringify({ hash: location.hash, last: leaf.length?leaf[leaf.length-1].innerText.replace(/\\s+/g,' ').slice(0,400):'', lastBtns: btns.slice(-14), overlayTop: overlays.slice(-1)[0]||'' });
  })()`);
}
async function clickBtn(h, text) {
  return await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').replace(/\\s+/g,'')===${JSON.stringify(text.replace(/\s+/g,''))} && x.offsetParent!==null); if(b){b.click(); return true;} return false; })()`);
}

(async () => {
  const h = await launch();
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/chat', { reload: false }); await sleep(3000);
  // 新建会话，避免污染
  await clickBtn(h, '新建'); await sleep(1200);
  log('start', await snapshot(h));

  const cases = [
    { tag: 'transfer_known_payee', text: '给李雷转 100 块', shot: 'B2-01-transfer-confirm' },
    { tag: 'freeze_card', text: '冻结我的银行卡', shot: 'B2-02-freeze-confirm' },
    { tag: 'cancel_sub', text: '帮我取消一个订阅', shot: 'B2-03-cancel-sub-confirm' },
    { tag: 'redeem', text: '帮我把理财赎回', shot: 'B2-04-redeem-confirm' },
  ];
  for (const c of cases) {
    await send(h, c.text);
    const s = await settle(h);
    const snap = JSON.parse(await snapshot(h));
    log('case:' + c.tag, JSON.stringify({ sent: c.text, settled: s, hash: snap.hash, last: snap.last, lastBtns: snap.lastBtns, overlayTop: snap.overlayTop }));
    await h.screenshot(SHOTS + '/' + c.shot + '.png');
    // 若出现确认卡，点击「取消」按钮验证可交互（避免副作用）
    const clickedCancel = await clickBtn(h, '取消');
    log('case:' + c.tag + ':clickedCancel', clickedCancel);
    await sleep(1500);
    const after = JSON.parse(await snapshot(h));
    log('case:' + c.tag + ':afterCancel', JSON.stringify({ hash: after.hash, last: after.last.slice(0,200) }));
    await h.screenshot(SHOTS + '/' + c.shot + '-afterCancel.png');
  }

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
