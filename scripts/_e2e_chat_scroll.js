/* 真机验证：从其它页面回到对话页，消息区必须落在最底部（新消息处）
 *  ① 进对话页（有历史的会话）→ 读滚动位置
 *  ② 手动滚到顶部 → 切到 #/bills → 切回 #/chat
 *  ③ 断言 scrollTop 回到接近 scrollHeight（底部）
 * 净副作用 0：只导航，不发消息。
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const LOG = '_reports/_e2e_chat_scroll.log';
const log = (...a) => { try { fs.appendFileSync(LOG, a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {} };

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1440, height: 900, watchdogMs: 150000 });
  const R = {};
  const pos = () => h.eval(`(() => {
    const els=[...document.querySelectorAll('.overflow-y-auto.scroll-thin')].filter(e=>e.scrollHeight>e.clientHeight+50);
    const el=els[0];
    return el? {top:el.scrollTop, sh:el.scrollHeight, ch:el.clientHeight, msgs:el.children.length} : null;
  })()`);
  try {
    await h.goto(H.BASE + '/');
    await h.eval("localStorage.clear(); localStorage.setItem('xb_user','user1'); localStorage.setItem('xb:u:user1:thread','web')");
    await h.setToken(process.env.XB_TOKEN || '');
    await h.goto(H.BASE + '/chat', { reload: true });
    await H.sleep(3500);                       // 等历史加载
    R.p1 = await pos();
    log('INITIAL=' + JSON.stringify(R.p1));

    // 手动滚到顶部，确认真的在顶
    await h.eval(`(() => { const els=[...document.querySelectorAll('.overflow-y-auto.scroll-thin')].filter(e=>e.scrollHeight>e.clientHeight+50); if(els[0]) els[0].scrollTop=0; })()`);
    await H.sleep(300);
    R.p2 = await pos();
    log('AT_TOP=' + JSON.stringify(R.p2));

    // 切走再切回
    await h.eval("location.hash='#/bills'");
    await H.sleep(1200);
    R.hashAway = await h.eval('location.hash');
    await h.eval("location.hash='#/chat'");
    await H.sleep(1500);
    R.p3 = await pos();
    log('BACK=' + JSON.stringify(R.p3));
    await h.screenshot('_reports/_e2e_chat_scroll_back.png');

    const atBottom = (p) => p && (p.sh - p.top - p.ch) < 40;   // 距底 <40px 视为在底部
    R.verdict = {
      awayOK: String(R.hashAway).includes('/bills'),
      topBefore: atBottom(R.p2) === false,
      bottomAfterBack: atBottom(R.p3),
    };
    log('VERDICT=' + JSON.stringify(R.verdict));
  } catch (e) {
    log('ERR ' + (e && e.stack || e));
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(0);
  }
})();
