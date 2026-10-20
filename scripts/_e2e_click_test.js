/* 全站逐页「真点击」深测（第二轮）：Tab（标题带数字）/ 展开收起 / 详情 / 弹窗开关 / 问小微预填
   + 全 body 范围（含会话列表）的"淡化交互"扫描与对比度测量。
   用法：XB_TOKEN=<token> node scripts/_e2e_click_test.js */
const H = require('./_e2e_lib.js');
const fs = require('fs');

const TOKEN = process.env.XB_TOKEN || '';
const LOG = '_reports/_e2e_click_test.log';
function log(...a) { const s = a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' '); try { fs.appendFileSync(LOG, s + '\n'); } catch (e) {} }
const VISFN = 'e=>(e.offsetParent||e.getClientRects().length)';

/* Tab 正则：允许「标题 + 空格 + 数字」 */
const TAB_RE = /^(生效中|待确认|已结束|我的卡片|已注销|在售产品|我的持仓|已赎回|安全设置|操作记录)(\s\d+)?$/;

async function clickTab(h, label) {
  /* ★ 修 bug：此前忽略传入的 label，用一条大正则 find()，结果每次都点到第一个 Tab。 */
  return await h.eval(`(() => {
    const vis=${VISFN};
    const lab=${JSON.stringify(label)};
    const b=[...document.querySelectorAll('button')].filter(vis).find(x => {
      const t=(x.innerText||'').trim();
      return t === lab || (t.startsWith(lab) && /^\\d+$/.test(t.slice(lab.length).trim()));
    });
    if(!b) return 'not-found';
    b.click(); return 'ok:' + (b.innerText||'').trim();
  })()`);
}
async function modalInfo(h) {
  return JSON.parse(await h.eval(`(() => {
    const el=[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).pop();
    if(!el) return JSON.stringify({open:false});
    return JSON.stringify({ open:true, title:(el.innerText||'').trim().slice(0,70),
      btns:[...el.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean) });
  })()`));
}
async function closeTopModal(h) {
  await h.eval(`(() => { const el=[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).pop();
    if(el) el.dispatchEvent(new MouseEvent('click',{bubbles:true})); })()`);
  await H.sleep(500);
}
/* 当前页面正文片段（用于确认切 Tab 后内容真的换了） */
async function snippet(h) {
  return await h.eval(`(() => { const s=document.querySelector('.overflow-y-auto.space-y-5')||document.body;
    return ((s.innerText||'').replace(/\\n+/g,'|')).slice(0,150); })()`);
}
async function cancelTopModal(h) {
  await h.eval(`(() => { const el=[...document.querySelectorAll('[data-modal-scrim]')].filter(${VISFN}).pop();
    if(!el) return; const b=[...el.querySelectorAll('button')].find(x=>/^(取消|关闭|返回)$/.test((x.innerText||'').trim())); if(b) b.click(); })()`);
  await H.sleep(600);
}

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1280, height: 800, watchdogMs: 900000 });
  const R = {};
  try {
    log('innerWidth=' + await h.eval('innerWidth') + ' innerHeight=' + await h.eval('innerHeight'));
    await h.goto(H.BASE + '/');
    await h.clearToken();
    await h.eval("localStorage.setItem('xb_user','user1')");
    await h.eval("localStorage.setItem('xb_u_user1_privacy','x')");   // 占位，下面用真实键
    await h.eval("localStorage.setItem('xb:u:user1:privacy','off')");
    await h.setToken(TOKEN);
    await h.goto(H.BASE + '/chat', { reload: true });

    /* ===== /chat：新会话 + 快捷卡预填 + 用户菜单 + 会话列表淡化按钮 ===== */
    await h.nav('/chat'); await H.sleep(2200);
    R.chat = {};
    R.chat.faintAll = JSON.parse(await h.eval(`(() => {
      const vis=${VISFN};
      const eff=e=>{let el=e,op=1;while(el&&el!==document.body){op*=parseFloat(getComputedStyle(el).opacity||'1');el=el.parentElement;}return op;};
      return JSON.stringify([...document.body.querySelectorAll('button,a,select,input')].filter(vis)
        .filter(e=>!e.disabled && eff(e)<0.99)
        .map(e=>({t:(e.innerText||e.tagName).trim().slice(0,14),op:Math.round(eff(e)*100)/100})));
    })()`));
    R.chat.newChat = await h.clickByText('新会话', { tag: 'button', visibleOnly: true, exact: true });
    await H.sleep(1200);
    await h.clickByText('查余额', { tag: 'button', visibleOnly: true });
    await H.sleep(700);
    R.chat.quickPrefill = await h.eval(`(() => { const ta=document.querySelector('textarea'); return ta ? (ta.value||'').slice(0,20) : null; })()`);
    R.chat.menu = await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>/pl-5/.test(x.className)&&/items-center gap-3/.test(x.className)); if(!b) return 'no-btn'; b.click(); return 'ok'; })()`);
    await H.sleep(700);
    const mt = await h.bodyText();
    R.chat.menuItems = ['个人信息','实名认证','数字柜员','与小微对话','退出登录','管理后台'].filter(k=>mt.includes(k));
    await h.eval(`document.body.click()`);
    await H.sleep(400);

    /* ===== /bills：展开/收起 ===== */
    await h.nav('/bills'); await H.sleep(2200);
    R.bills = {};
    await h.clickByText('展开查看', { tag: 'button', visibleOnly: true, nth: 0 }); await H.sleep(700);
    await h.clickByText('展开查看', { tag: 'button', visibleOnly: true, nth: 0 }); await H.sleep(1000);
    R.bills.expanded = JSON.parse(await h.eval(`JSON.stringify({table:!!document.querySelector('table'), selects:[...document.querySelectorAll('select')].filter(${VISFN}).length})`));
    await cancelTopModal(h);
    await h.clickByText('收起', { tag: 'button', visibleOnly: true, nth: 0 }); await H.sleep(400);
    await h.clickByText('收起', { tag: 'button', visibleOnly: true, nth: 0 }); await H.sleep(800);
    R.bills.recollapsed = JSON.parse(await h.eval(`JSON.stringify({table:!!document.querySelector('table'), selects:[...document.querySelectorAll('select')].filter(${VISFN}).length})`));

    /* ===== /wealth：3 Tab + 详情 + 购买弹窗 + 赎回 gate ===== */
    await h.nav('/wealth'); await H.sleep(2600);
    R.wealth = { tabs: [] };
    for (const t of ['在售产品', '我的持仓', '已赎回']) {
      const r = await clickTab(h, t); await H.sleep(1200);
      R.wealth.tabs.push({ t, r, body: await snippet(h) });
    }
    await clickTab(h, '我的持仓'); await H.sleep(1000);
    await h.clickByText('详情', { tag: 'button', visibleOnly: true }); await H.sleep(700);
    R.wealth.detail = (await h.bodyText()).includes('买入日');
    await h.clickByText('赎回', { tag: 'button', visibleOnly: true, exact: true }); await H.sleep(1200);
    R.wealth.redeemGate = (await modalInfo(h)).title || null;
    await cancelTopModal(h);
    await clickTab(h, '在售产品'); await H.sleep(1000);
    await h.clickByText('立即购买', { tag: 'button', visibleOnly: true }); await H.sleep(1000);
    R.wealth.buyModal = (await modalInfo(h)).title || null;
    await closeTopModal(h);

    /* ===== /cards：2 Tab + 详情抽屉 + 冻结 gate + 申请弹窗 ===== */
    await h.nav('/cards'); await H.sleep(2600);
    R.cards = { tabs: [] };
    for (const t of ['我的卡片', '已注销']) { const r = await clickTab(h, t); await H.sleep(1100); R.cards.tabs.push({ t, r, body: await snippet(h) }); }
    await clickTab(h, '我的卡片'); await H.sleep(900);
    await h.clickByText('详情', { tag: 'button', visibleOnly: true }); await H.sleep(900);
    const di = await modalInfo(h);
    R.cards.detailBtns = di.btns || null;
    await h.clickByText('冻结卡片', { tag: 'button', visibleOnly: true }); await H.sleep(1300);
    R.cards.freezeGate = ((await modalInfo(h)).title || '').slice(0, 46);
    await cancelTopModal(h);
    await h.clickByText('+ 申请新卡', { tag: 'button', visibleOnly: true }); await H.sleep(1000);
    R.cards.applyModal = (await modalInfo(h)).title || null;
    await closeTopModal(h);

    /* ===== /subs：3 Tab + 详情 + 取消 gate + 签约弹窗 ===== */
    await h.nav('/subs'); await H.sleep(2600);
    R.subs = { tabs: [] };
    for (const t of ['生效中', '待确认', '已结束']) { const r = await clickTab(h, t); await H.sleep(1200); R.subs.tabs.push({ t, r, body: await snippet(h) }); }
    R.subs.pendingFold = await h.eval(`document.body.innerText.includes('项疑似订阅扣款')`) ;
    await clickTab(h, '生效中'); await H.sleep(1000);
    await h.clickByText('详情', { tag: 'button', visibleOnly: true }); await H.sleep(700);
    R.subs.detail = (await h.bodyText()).includes('上次实扣');
    await h.clickByText('取消订阅', { tag: 'button', visibleOnly: true }); await H.sleep(1300);
    R.subs.cancelGate = ((await modalInfo(h)).title || '').slice(0, 40);
    await cancelTopModal(h);
    await h.clickByText('+ 签约新订阅', { tag: 'button', visibleOnly: true }); await H.sleep(1000);
    R.subs.contractModal = (await modalInfo(h)).title || null;
    await closeTopModal(h);

    /* ===== /safe：2 Tab + 风控弹窗 ===== */
    await h.nav('/safe'); await H.sleep(2400);
    R.safe = { tabs: [] };
    for (const t of ['安全设置', '操作记录']) { const r = await clickTab(h, t); await H.sleep(1200); R.safe.tabs.push({ t, r, body: await snippet(h) }); }
    await clickTab(h, '安全设置'); await H.sleep(900);
    await h.clickByText('查看风控规则', { tag: 'button', visibleOnly: true }); await H.sleep(900);
    R.safe.rulesModal = (await modalInfo(h)).title || null;
    await closeTopModal(h);

    /* ===== /transfer：步骤 1 控件 + 前进一步（纯前端，不提交） ===== */
    await h.nav('/transfer'); await H.sleep(2400);
    R.transfer = JSON.parse(await h.eval(`(() => {
      const t=document.body.innerText||'';
      return JSON.stringify({ steps: ['收款人','金额','核对','安全执行'].filter(s=>t.includes(s)),
        hasPayee: /李雷|收款人/.test(t) });
    })()`));

    /* ===== /appearance：只读预览 ===== */
    await h.nav('/appearance'); await H.sleep(2600);
    R.appearance = JSON.parse(await h.eval(`(() => {
      const vis=${VISFN};
      return JSON.stringify({ hasIframe: !!document.querySelector('iframe'),
        inputs: [...document.querySelectorAll('input,select')].filter(vis).length,
        hasReadonlyBadge: (document.body.innerText||'').includes('只读') });
    })()`));

    await h.nav('/chat'); await H.sleep(1200);
    log('CHAT=' + JSON.stringify(R.chat));
    log('BILLS=' + JSON.stringify(R.bills));
    log('WEALTH=' + JSON.stringify(R.wealth));
    log('CARDS=' + JSON.stringify(R.cards));
    log('SUBS=' + JSON.stringify(R.subs));
    log('SAFE=' + JSON.stringify(R.safe));
    log('TRANSFER=' + JSON.stringify(R.transfer));
    log('APPEARANCE=' + JSON.stringify(R.appearance));
    fs.writeFileSync('_reports/_e2e_click_test.json', JSON.stringify(R, null, 1));
    await h.close();
    process.exit(0);
  } catch (e) {
    try { fs.appendFileSync(LOG, 'ERR ' + (e && e.stack || e) + '\n'); } catch (_) {}
    process.exit(1);
  }
})();
