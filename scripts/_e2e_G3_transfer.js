/* G3：转账额度/手续费/差额（精确填金额） */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/G3.log');

(async () => {
  const h = await launch({ watchdogMs: 200000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/transfer', { reload: false }); await sleep(4000);

  // 选收款人（跨行 常用）
  log('pick', await h.eval(`(() => {
    const rows=[...document.querySelectorAll('button,li,div')].filter(e=>/尾号/.test(e.innerText||'') && (e.innerText||'').length<90 && e.offsetParent!==null);
    if(!rows.length) return JSON.stringify({found:0});
    rows[0].click(); return JSON.stringify({clicked:(rows[0].innerText||'').replace(/\\s+/g,' ').slice(0,50)});
  })()`));
  await sleep(1800);
  log('inputs_after_pick', await h.eval(`(() => JSON.stringify([...document.querySelectorAll('input')].map((e,i)=>({i,type:e.type,ph:e.placeholder,val:e.value}))))()`));

  // 填 60000 到最可能的金额框（排除搜索框）
  log('fill_60000', await h.eval(`(() => {
    const ins=[...document.querySelectorAll('input')];
    const amt=ins.find(e=>!/搜索|昵称|银行/.test(e.placeholder||'') && (e.inputMode==='decimal'||e.type==='number'||/金额|0\\.00/.test(e.placeholder||''))) || ins.find(e=>!/搜索|昵称/.test(e.placeholder||''));
    if(!amt) return JSON.stringify({found:false});
    const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    s.call(amt,'60000'); amt.dispatchEvent(new Event('input',{bubbles:true}));
    return JSON.stringify({found:true, ph:amt.placeholder});
  })()`));
  await sleep(1500);
  log('msgs_60000', await h.eval(`(() => { const t=document.body.innerText||''; return JSON.stringify({ m:(t.match(/[^\\n]{0,26}(限额|上限|单笔|日累计|超过|手续费|费率|0\\.1%|最低|最高|差额|不足)[^\\n]{0,30}/g)||[]).slice(0,6) }); })()`));
  await h.screenshot(SHOTS + '/G3-01-60000.png');

  // 改成 100 看跨行手续费
  log('fill_100', await h.eval(`(() => { const ins=[...document.querySelectorAll('input')]; const amt=ins.find(e=>!/搜索|昵称|银行/.test(e.placeholder||'') && (e.inputMode==='decimal'||e.type==='number'||/金额|0\\.00/.test(e.placeholder||'')))||ins.find(e=>!/搜索|昵称/.test(e.placeholder||'')); if(!amt) return 'no'; const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set; s.call(amt,'100'); amt.dispatchEvent(new Event('input',{bubbles:true})); return 'ok'; })()`));
  await sleep(1500);
  log('msgs_100', await h.eval(`(() => { const t=document.body.innerText||''; return JSON.stringify({ fee:(t.match(/[^\\n]{0,26}(手续费|费率|0\\.1%|预计|到账|差额)[^\\n]{0,30}/g)||[]).slice(0,6) }); })()`));
  await h.screenshot(SHOTS + '/G3-02-100-fee.png');

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
