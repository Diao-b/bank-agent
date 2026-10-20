/* 真机验证：前端「零延迟导航」不再劫持陈述/咨询句
 *  ① 说「我的数据安全。」→ hash 必须留在 #/chat
 *  ② 说「打开我的账单」  → hash 应变 #/bills（真指令仍要零延迟跳）
 * 净副作用：只发两条消息（一条查询类 + 一条导航指令），不触发任何写操作。
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const LOG = '_reports/_e2e_navfix.log';
const log = (...a) => { try { fs.appendFileSync(LOG, a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {} };

const VISFN = "e=>!!(e.offsetParent||e.getClientRects().length)";

async function typeAndSend(h, text) {
  const filled = await h.eval(`(() => {
    const t = [...document.querySelectorAll('textarea')].filter(${VISFN})[0];
    if (!t) return 'no-textarea';
    t.focus();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(t, ${JSON.stringify(text)});
    t.dispatchEvent(new Event('input', { bubbles: true }));
    return t.value;
  })()`);
  await H.sleep(300);
  for (const type of ['keyDown', 'char', 'keyUp']) {
    await h.b.send('Input.dispatchKeyEvent', {
      type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
      text: type === 'char' ? '\r' : undefined,
    }, h.sid);
  }
  return filled;
}

async function hash(h) {
  return await h.eval("location.hash || '(empty)'");
}

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1440, height: 900, watchdogMs: 200000 });
  const R = {};
  try {
    await h.goto(H.BASE + '/');
    await h.eval("localStorage.clear(); localStorage.setItem('xb_user','user1')");
    await h.setToken(process.env.XB_TOKEN || '');
    await h.goto(H.BASE + '/chat', { reload: true });
    await H.sleep(1500);
    R.hash0 = await hash(h);
    log('HASH_START=' + R.hash0);

    /* ① 陈述句：不应跳转 */
    log('FILL1=' + await typeAndSend(h, '我的数据安全。'));
    await H.sleep(6000);                       // 等前端跳转（若有）+ 回复落地
    R.hashAfterConsult = await hash(h);
    R.reply1 = await h.eval(`(() => {
      const b = [...document.querySelectorAll('div')].filter(${VISFN})
        .map(d => (d.innerText || '').trim()).filter(t => /数据安全/.test(t) && t.length > 30);
      return b.length ? b[b.length - 1].slice(0, 60) : '(未匹配到回复)';
    })()`);
    log('HASH_AFTER_CONSULT=' + R.hashAfterConsult);
    log('REPLY1=' + String(R.reply1).replace(/\n/g, ' '));
    await h.screenshot('_reports/_e2e_navfix_1_consult.png');

    /* ② 真指令：应零延迟跳转 */
    log('FILL2=' + await typeAndSend(h, '打开我的账单'));
    await H.sleep(1500);
    R.hashAfterCommand = await hash(h);
    log('HASH_AFTER_COMMAND=' + R.hashAfterCommand);
    await h.screenshot('_reports/_e2e_navfix_2_command.png');

    /* ③ 后端闸门：问「安全中心是干嘛的」→ 不得出现「查看卡片管理」这类错入口 */
    await h.goto(H.BASE + '/chat', { reload: true });
    await H.sleep(1200);
    log('FILL3=' + await typeAndSend(h, '安全中心是干嘛的'));
    await H.sleep(9000);
    R.hashAfterSafe = await hash(h);
    // 收集本条回复下的可点导航入口文案（NavHint 按钮）
    R.navHints = await h.eval(`(() => {
      const vis = ${VISFN};
      return [...document.querySelectorAll('button')].filter(vis)
        .map(b => (b.innerText || '').trim())
        .filter(t => /^查看|前往|打开/.test(t));
    })()`);
    log('HASH_AFTER_SAFE=' + R.hashAfterSafe);
    log('NAV_HINTS=' + JSON.stringify(R.navHints));
    await h.screenshot('_reports/_e2e_navfix_3_safegate.png');

    R.verdict = {
      consultStayed: R.hashAfterConsult.includes('/chat'),
      commandJumped: R.hashAfterCommand.includes('/bills'),
      safeStayed: R.hashAfterSafe.includes('/chat'),
      noWrongCardHint: !(R.navHints || []).some(t => /卡片/.test(t)),
    };
    log('VERDICT=' + JSON.stringify(R.verdict));
  } catch (e) {
    log('ERR ' + (e && e.stack || e));
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(0);
  }
})();
