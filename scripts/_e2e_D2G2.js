/* D2 理财适当性拦截 + G2 转账额度/手续费 */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/D2G2.log');
// 排除收件箱浮层，取真正的业务弹层
const realModal = `(() => {
  const ov=[...document.querySelectorAll('div')].filter(d=>typeof d.className==='string'&&d.className.includes('fixed inset-0'));
  const real=ov.filter(d=>!/收件箱|标记全部已读/.test((d.innerText||'').slice(0,40)));
  if(!real.length) return JSON.stringify({open:false});
  const top=real[real.length-1];
  return JSON.stringify({open:true, text:(top.innerText||'').replace(/\\s+/g,' ').slice(0,220), btns:[...top.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean).slice(0,8)});
})()`;

(async () => {
  const h = await launch({ watchdogMs: 240000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);

  // D2 理财：点 R3 立即购买
  await h.goto(BASE + '/chat#/wealth', { reload: false }); await sleep(4000);
  const r3click = await h.eval(`(() => {
    const btns=[...document.querySelectorAll('button')].filter(b=>(b.innerText||'').trim()==='立即购买');
    if(!btns.length) return JSON.stringify({found:0});
    const target=btns.find(b=>{let n=b;for(let k=0;k<6&&n;k++){n=n.parentElement;if(n&&/R3/.test(n.innerText||''))return true;}return false;})||btns[0];
    target.click(); return JSON.stringify({found:btns.length});
  })()`);
  log('d2_click_R3_buy', r3click);
  await sleep(2500);
  log('d2_modal', await h.eval(realModal));
  await h.screenshot(SHOTS + '/D2-01-R3-buy.png');
  // 关闭弹层
  await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>/取消|关闭/.test(x.innerText||'')&&x.offsetParent!==null); if(b)b.click(); })()`);
  await sleep(800);

  // G2 转账：选跨行收款人 → 输 60000 → 看限额/手续费
  await h.goto(BASE + '/chat#/transfer', { reload: false }); await sleep(4000);
  // 选第一个收款人
  const pick = await h.eval(`(() => {
    const cands=[...document.querySelectorAll('button,div')].filter(e=>/招商银行|工商银行|尾号/.test(e.innerText||'') && (e.innerText||'').length<80 && e.offsetParent!==null);
    const t=cands.find(e=>/常用|跨行/.test(e.innerText||'')) || cands[0];
    if(!t) return JSON.stringify({found:0});
    t.click(); return JSON.stringify({clicked:(t.innerText||'').replace(/\\s+/g,' ').slice(0,60)});
  })()`);
  log('g2_pick_payee', pick);
  await sleep(1500);
  await h.screenshot(SHOTS + '/G2-01-payee-picked.png');
  // 填金额 60000
  const amt = await h.eval(`(() => { const el=[...document.querySelectorAll('input')].find(e=>/金额|amount/.test((e.placeholder||'')+(e.name||'')) || e.inputMode==='decimal' || e.type==='number') || document.querySelector('input'); if(!el) return JSON.stringify({found:false}); const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set; s.call(el,'60000'); el.dispatchEvent(new Event('input',{bubbles:true})); return JSON.stringify({found:true, ph:el.placeholder}); })()`);
  log('g2_fill_amount', amt);
  await sleep(1200);
  log('g2_after_amount', await h.eval(`(() => { const t=document.body.innerText||''; return JSON.stringify({ limit: (t.match(/[^\\n]{0,24}(限额|上限|单笔|超过|手续费|费率|0\\.1%)[^\\n]{0,30}/g)||[]).slice(0,5) }); })()`));
  await h.screenshot(SHOTS + '/G2-02-amount-60000.png');

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
