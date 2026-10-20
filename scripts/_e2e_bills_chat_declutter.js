/* 页面降噪 · 第 5 批验收（真机 CDP）：#/bills + #/chat 去重
   用法：XB_TOKEN=<token> node scripts/_e2e_bills_chat_declutter.js */
const H = require('./_e2e_lib.js');
const fs = require('fs');

const TOKEN = process.env.XB_TOKEN || '';
const LOG = '_reports/_e2e_bills_chat_declutter.log';
function log(...a) { const s = a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' '); try { fs.appendFileSync(LOG, s + '\n'); } catch (e) {} }
const VISFN = 'e=>(e.offsetParent||e.getClientRects().length)';

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1280, height: 800 });
  const R = {};
  try {
    log('innerWidth=' + await h.eval('innerWidth') + ' innerHeight=' + await h.eval('innerHeight'));
    await h.goto(H.BASE + '/');
    await h.clearToken();
    await h.eval("localStorage.setItem('xb_user','user1')");
    await h.setToken(TOKEN);
    await h.goto(H.BASE + '/chat', { reload: true });

    /* ---------- A. 账单页 ---------- */
    await h.nav('/bills');
    await h.waitForText('本月总支出', 12000);
    R.bills = JSON.parse(await h.eval(`(() => {
      const NL=String.fromCharCode(10);
      const shell = document.querySelector('.overflow-y-auto.space-y-5');
      const kids=[...shell.children].filter(${VISFN}).map(e=>{const b=e.getBoundingClientRect();
        return {top:Math.round(b.top),h:Math.round(b.height),head:(e.innerText||'').trim().slice(0,26).split(NL).join('|')};});
      return JSON.stringify({ innerHeight:innerHeight, kids,
        firstScreen: kids.filter(k=>k.top<innerHeight).length,
        gold:[...document.querySelectorAll('button')].filter(${VISFN}).filter(e=>/bg-brand-600/.test(e.className)).length,
        inputs:[...document.querySelectorAll('input,select')].filter(${VISFN}).map(e=>e.name||e.tagName),
        hasMonthTitle: (document.body.innerText||'').includes('本月分类占比'),
        hasTrendTitle: (document.body.innerText||'').includes('近 3 个月支出趋势'),
        anomBodyHidden: !(document.body.innerText||'').includes('本月未发现异常支出'),
      });
    })()`));
    await h.screenshot('_reports/_e2e_bills_1_collapsed.png');

    /* 展开异常提醒 */
    await h.clickByText('展开查看', { tag: 'button', visibleOnly: true, nth: 0 });
    await H.sleep(700);
    const t1 = await h.bodyText();
    R.anomExpanded = t1.includes('本月未发现异常支出') || t1.includes('待确认');
    await h.screenshot('_reports/_e2e_bills_2_anomaly.png');

    /* 展开交易流水（第二个「展开查看」） */
    await h.clickByText('展开查看', { tag: 'button', visibleOnly: true, nth: 0 });
    await H.sleep(900);
    R.txExpanded = JSON.parse(await h.eval(`JSON.stringify((() => ({
      hasTable: !!document.querySelector('table'),
      selects: [...document.querySelectorAll('select')].filter(${VISFN}).length,
      hasRuleNote: (document.body.innerText||'').includes('不影响上方图表'),
    }))())`));
    await h.screenshot('_reports/_e2e_bills_3_tx.png');
    log('BILLS=' + JSON.stringify(R.bills));
    log('ANOM_EXPANDED=' + R.anomExpanded + ' TX=' + JSON.stringify(R.txExpanded));

    /* ---------- B. 对话页去重 ---------- */
    await h.nav('/chat');
    await h.waitForText('新会话', 12000);
    await H.sleep(800);
    R.chat = JSON.parse(await h.eval(`(() => {
      const vis = ${VISFN};
      const btns=[...document.querySelectorAll('button')].filter(vis);
      const txt = b => (b.innerText||'').trim();
      return JSON.stringify({
        newChatBtns: btns.filter(b=>/新会话|新建/.test(txt(b))).map(txt),
        chatCards: [...document.querySelectorAll('.qa-card')].filter(vis).map(e=>txt(e).split(String.fromCharCode(10))[0]),
        sidebarChips: [...document.querySelectorAll('.qa-chip')].filter(vis).map(e=>txt(e)),
        transferCount: btns.filter(b=>/^转账$/.test(txt(b))).length,
        billsNav: [...document.querySelectorAll('button')].filter(vis).filter(b=>/^账单分析$/.test(txt(b))).length,
        billsCard: [...document.querySelectorAll('.qa-card')].filter(vis).filter(e=>txt(e).includes('账单分析')).length,
      });
    })()`));
    await h.screenshot('_reports/_e2e_bills_chat_4_chat.png');
    log('CHAT=' + JSON.stringify(R.chat));
    await h.close();
    process.exit(0);
  } catch (e) {
    try { fs.appendFileSync(LOG, 'ERR ' + (e && e.stack || e) + '\n'); } catch (_) {}
    process.exit(1);
  }
})();
