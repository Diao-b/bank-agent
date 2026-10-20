/* 界面复核：重放截图那一问，看回复是否还以称呼开头（真按键 Enter 发送）
 * 净副作用：只发一条查询类问题（无写操作）。
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const LOG = '_reports/_e2e_addr_ui.log';
const log = (...a) => { try { fs.appendFileSync(LOG, a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {} };

const VISFN = "e=>!!(e.offsetParent||e.getClientRects().length)";

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1440, height: 900, watchdogMs: 200000 });
  try {
    await h.goto(H.BASE + '/');
    await h.eval("localStorage.clear(); localStorage.setItem('xb_user','user1')");
    await h.setToken(process.env.XB_TOKEN || '');
    await h.goto(H.BASE + '/chat', { reload: true });
    await H.sleep(1500);
    log('GREETING=' + (await h.eval(`(document.body.innerText||'').split('\\n').find(l=>/我是小微/.test(l))||'none'`)));

    /* 填问题（React 受控 textarea 用原生 setter） */
    const filled = await h.eval(`(() => {
      const t = [...document.querySelectorAll('textarea')].filter(${VISFN})[0];
      if (!t) return 'no-textarea';
      t.focus();
      const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(t, '全球成长精选这个产品怎么样？风险高吗');
      t.dispatchEvent(new Event('input', { bubbles: true }));
      return t.value;
    })()`);
    log('FILLED=' + filled);
    await H.sleep(400);

    /* 真按键 Enter（CDP，非页内合成事件） */
    for (const type of ['keyDown', 'char', 'keyUp']) {
      await h.b.send('Input.dispatchKeyEvent', {
        type, key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13,
        text: type === 'char' ? '\r' : undefined,
      }, h.sid);
    }

    /* 等回复落地 */
    let txt = '';
    for (let i = 0; i < 30; i++) {
      await H.sleep(2000);
      const s = JSON.parse(await h.eval(`JSON.stringify({
        typing: /小微正在输入/.test(document.body.innerText||''),
        bubbles: [...document.querySelectorAll('div')].filter(d => d.className && /whitespace-pre-wrap/.test(String(d.className))).map(d => (d.innerText||'').trim())
      })`));
      if (!s.typing && s.bubbles.length >= 2) { txt = s.bubbles[s.bubbles.length - 1]; break; }
    }
    const first = (txt || '').split('\n')[0];
    log('REPLY_FIRST=' + first);
    log('STARTS_WITH_ADDR=' + /^张先生|^尊敬的张先生/.test((txt || '').trim()));
    await h.screenshot('_reports/_e2e_addr_ui.png');
    log('SHOT=_reports/_e2e_addr_ui.png');
  } catch (e) {
    log('ERR ' + (e && e.stack || e));
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(0);
  }
})();
