/* B3：前端关键词导航劫持专项（咨询/业务指令 是否被错误跳转） */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/B3_nav.log');

(async () => {
  const h = await launch({ watchdogMs: 300000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/chat', { reload: false }); await sleep(3000);

  // 直接在页面里对 detectNavIntent 做"纯函数"复算，作为根因证据（读真实源码实现）
  const fn = await h.eval(`(() => {
    const NAV_VERB_RE = /(打开|进入|去|到|看|查|显示|帮我|我想|我要|切换|回到|返回|跳到|前往)/;
    const NAV_INTENTS = [
      { re: /(账单|明细|支出|消费|流水|花[的了钱]|收支|还款)/, to:'/bills' },
      { re: /(转账|汇款|打钱|转钱|付款)/, to:'/transfer' },
      { re: /(理财|基金|持仓|存款|收益|投资|定期)/, to:'/wealth' },
      { re: /(卡片|银行卡|信用卡|尾号|额度)/, to:'/cards' },
      { re: /(订阅|自动扣费|自动续费|会员)/, to:'/subs' },
      { re: /(安全|安全中心|密码|冻结|挂失|锁定)/, to:'/safe' },
    ];
    const d = (text) => { const t=(text||'').trim(); if(!t) return null;
      for (const it of NAV_INTENTS){ if(it.re.test(t)&&NAV_VERB_RE.test(t)) return {to:it.to, by:'verb+topic'};
        if(it.re.test(t)&&t.length<=10) return {to:it.to, by:'short<=10'}; } return null; };
    const cases = ['可以查看我最近的流水账单吗?','冻结我的银行卡','把我的闲置订阅都取消掉，省下来的钱帮我买理财','帮我取消一个订阅','帮我把理财赎回','打开账单页','我想了解下理财','查询我的余额'];
    return JSON.stringify(cases.map(c => ({ text:c, len:c.length, nav:d(c) })));
  })()`);
  log('detectNavIntent_sim', fn);

  // 实测：发送后 hash 是否被前端跳走
  const PROBES = [
    { tag: 'consult_bill', text: '可以查看我最近的流水账单吗?' },
    { tag: 'task_freeze',  text: '冻结我的银行卡' },
    { tag: 'task_cancel_sub', text: '帮我取消一个订阅' },
    { tag: 'task_redeem',  text: '帮我把理财赎回' },
    { tag: 'explicit_open', text: '打开账单页' },
  ];
  for (const p of PROBES) {
    // 回到对话页再发（避免上一轮跳转影响）
    await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
    await h.goto(BASE + '/chat#/chat', { reload: false }); await sleep(2200);
    const before = await h.eval(`location.hash`);
    await h.eval(`(() => { const el=document.querySelector('textarea'); if(!el) return false; const s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set; s.call(el, ${JSON.stringify(p.text)}); el.dispatchEvent(new Event('input',{bubbles:true})); return true; })()`);
    await sleep(350);
    await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x => (x.innerText||'').replace(/\\s/g,'')==='发送'); if(b) b.click(); return true; })()`);
    await sleep(4000);
    const after = await h.eval(`location.hash`);
    const bodyHasConfirm = await h.eval(`/确认|取消|核验|验证码/.test(document.body.innerText||'')`);
    log('probe:' + p.tag, JSON.stringify({ text: p.text, hashBefore: before, hashAfter: after, navigated: before !== after, pageHasConfirmOrVerify: bodyHasConfirm }));
    await h.screenshot(SHOTS + '/B3-' + p.tag + '.png');
  }

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
