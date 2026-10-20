/* D 理财 + F 订阅 + G 转账：页面事实与关键交互 */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/DFG.log');

(async () => {
  const h = await launch({ watchdogMs: 240000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  // 确保隐私开（还原）
  await h.goto(BASE + '/chat#/chat', { reload: false }); await sleep(2500);
  const pv = await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>/隐私/.test(x.innerText||'')); if(b && /关/.test(b.innerText)){ b.click(); return 'toggled_on'; } return (b?b.innerText:'no btn'); })()`);
  log('privacy_restore', pv);

  // D 理财
  await h.goto(BASE + '/chat#/wealth', { reload: false }); await sleep(4000);
  log('wealth', await h.eval(`(() => {
    const t=document.body.innerText||'';
    const prods=[...document.querySelectorAll('div')].filter(d=>typeof d.className==='string' && /R[123]/.test(d.innerText||'') && (d.innerText||'').length<200 && (d.innerText||'').length>20);
    const leaf=prods.filter(d=>!prods.some(o=>o!==d&&d.contains(o)));
    return JSON.stringify({
      riskLabels: (t.match(/R[123]/g)||[]).slice(0,10),
      has起购: /起购/.test(t), has年化: /年化/.test(t),
      products: leaf.slice(0,5).map(d=>(d.innerText||'').replace(/\\s+/g,' ').slice(0,90)),
      riskPrefShown: (t.match(/稳健|平衡|进取|风险偏好[^\\n]{0,10}/g)||[]).slice(0,5),
      hasBuyBtn: [...document.querySelectorAll('button')].filter(b=>/立即购买|申购/.test(b.innerText||'')).length,
      head: t.replace(/\\s+/g,' ').slice(0,300),
    });
  })()`));
  await h.screenshot(SHOTS + '/D01-wealth.png');

  // 适当性：点第一个 R3 产品的「立即购买」
  const r3 = await h.eval(`(() => {
    const btns=[...document.querySelectorAll('button')].filter(b=>/立即购买|申购/.test(b.innerText||''));
    if(!btns.length) return JSON.stringify({found:0});
    // 找其上方含 R3 的容器
    const target = btns.find(b => { let n=b; for(let k=0;k<6&&n;k++){ n=n.parentElement; if(n&&/R3/.test(n.innerText||'')) return true; } return false; }) || btns[0];
    target.click(); return JSON.stringify({found:btns.length, clicked:(target.innerText||'').trim()});
  })()`);
  log('wealth_buy_R3_click', r3);
  await sleep(2500);
  log('wealth_after_buy_click', await h.eval(`(() => { const t=document.body.innerText||'';
    const ov=[...document.querySelectorAll('div')].filter(d=>typeof d.className==='string'&&d.className.includes('fixed inset-0'));
    return JSON.stringify({ modalOpen: ov.length>0, modalText: ov.length?(ov[ov.length-1].innerText||'').replace(/\\s+/g,' ').slice(0,200):'', blockMsg:(t.match(/[^\\n]{0,30}(风险|适当性|无法|不能|不支持|请先)[^\\n]{0,40}/g)||[]).slice(0,4) }); })()`));
  await h.screenshot(SHOTS + '/D02-wealth-buy.png');

  // F 订阅
  await h.goto(BASE + '/chat#/subs', { reload: false }); await sleep(4000);
  log('subs', await h.eval(`(() => { const t=document.body.innerText||'';
    return JSON.stringify({
      hasList: /订阅|自动扣|周期/.test(t),
      count: (t.match(/每月|每周|每年|已取消|生效中|扣款/g)||[]).length,
      rows: (t.match(/[^\\n]{0,20}(生效中|已取消|已暂停|待取消)[^\\n]{0,20}/g)||[]).slice(0,6),
      discover: /识别到|发现|转为订阅|确认转为订阅/.test(t),
      head: t.replace(/\\s+/g,' ').slice(0,300),
    }); })()`));
  await h.screenshot(SHOTS + '/F01-subs.png');

  // G 转账：超额
  await h.goto(BASE + '/chat#/transfer', { reload: false }); await sleep(4000);
  log('transfer_page', await h.eval(`(() => { const t=document.body.innerText||'';
    return JSON.stringify({ hasPayee: /收款人|常用/.test(t), hasAmount: /金额/.test(t), head: t.replace(/\\s+/g,' ').slice(0,300) }); })()`));
  await h.screenshot(SHOTS + '/G01-transfer.png');

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
