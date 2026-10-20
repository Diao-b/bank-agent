/* 批 2 续（第 4 组）· 真机验收：G-02 弹窗 ESC/aria · G-03 h1/aria-label · T-03 手续费前置 · T-04 付款账户三态
 * 用法：node scripts/_ux_fix_verify4.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_fix_verify4.log'));
let pass = 0, fail = 0;
const ok = (n, c, e) => { if (c) { pass++; log('PASS', n + (e !== undefined ? '  ' + JSON.stringify(e) : '')); } else { fail++; log('FAIL', n + '  ' + JSON.stringify(e)); } };

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  try {
    await h.goto(BASE + '/login');
    await h.eval(`localStorage.clear()`);
    await h.setViewport(1440, 920);
    await h.goto(BASE + '/login');
    await h.type('input[placeholder="请输入用户名"]', 'user1');
    await h.type('input[placeholder="请输入密码"]', '123456');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3600);

    /* ============ G-03 每页 h1 + 输入框 aria-label ============ */
    const h1count = async () => Number(await h.eval(`document.querySelectorAll('h1').length`));
    await h.eval(`location.hash = '#/chat'`);
    await sleep(2200);
    ok('G-03 ★对话页有 h1', (await h1count()) >= 1);
    ok('G-03 ★对话主输入框有 aria-label',
      await h.eval(`(()=>{const t=document.querySelector('textarea');return !!(t&&t.getAttribute('aria-label'))})()`));
    for (const [name, hash] of [['bills', '#/bills'], ['wealth', '#/wealth'], ['cards', '#/cards'], ['subs', '#/subs'], ['safe', '#/safe'], ['transfer', '#/transfer']]) {
      await h.eval(`location.hash = ${JSON.stringify(hash)}`);
      await sleep(1600);
      const n = await h1count();
      ok(`G-03 页面有 h1（${name}）`, n >= 1, { h1: n });
    }

    /* ============ G-02 弹窗 ESC + role=dialog ============ */
    await h.eval(`location.hash = '#/subs'`);
    await sleep(2600);
    await h.clickByText('取消', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(1500);
    const dlg = await h.eval(`document.querySelectorAll('[role="dialog"][aria-modal="true"]').length`);
    ok('G-02 ★弹窗带 role=dialog + aria-modal', dlg >= 1, { dialogs: dlg });
    const before = (await h.bodyText()) || '';
    const opened = /直接取消订阅|提交后走与 AI 对话/.test(before);
    ok('G-02 取消弹窗已打开', opened);
    // 真按 ESC
    await h.eval(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',keyCode:27,bubbles:true}));`);
    await sleep(1500);
    const afterEsc = (await h.bodyText()) || '';
    ok('G-02 ★按 ESC 能关掉弹窗', !/直接取消订阅[\s\S]{0,80}提交后将走/.test(afterEsc) && !/确认执行/.test(afterEsc),
      { sample: (afterEsc.match(/.{0,40}直接取消订阅/) || ['(无)'])[0] });
    await h.screenshot(path.join(OUT, 'fix4_esc_closed.png'));

    /* ============ T-03 手续费在金额步就可见 ============ */
    await h.eval(`location.hash = '#/transfer'`);
    await sleep(2600);
    // 选第一个收款人（收款人卡是 div，不是 button）→ 点「下一步」进入金额步
    // 优先挑「跨行」收款人（站内转账本来就免手续费，看不到费率口径）
    const pick = await h.eval(`(()=>{
      const cands=[...document.querySelectorAll('div,button')].filter(e=>{
        const t=(e.innerText||'').trim();
        return /^(妈妈|李雷|房东王姐|张伟)/.test(t) && t.length < 60 && (e.offsetParent||e.getClientRects().length);
      });
      const cross = cands.filter(e=>/跨行/.test(e.innerText));
      const chosen = (cross[0] || cands[cands.length-1]);
      if (!chosen) return 'no-payee';
      chosen.click();
      return 'pick:'+chosen.innerText.trim().replace(/\\s+/g,' ').slice(0,24);
    })()`);
    await sleep(1500);
    await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/下一步/.test(x.innerText||'')&&!x.disabled);b&&b.click();return 1})()`);
    await sleep(1800);
    log('INFO', '收款人选择结果 ' + pick);
    const step2 = (await h.bodyText()) || '';
    await h.type('input[inputmode="decimal"]', '3000');
    await sleep(2200);
    const step2b = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix4_transfer_step2_fee.png'));
    // 跨行 → 「本次手续费 ¥X（跨行，费率 …，最低 …、最高 …）」；站内 → 「本行站内转账免手续费」
    ok('T-03 ★金额步（第 2 步）就能看到手续费/免手续费与扣款总额',
      (/本次手续费/.test(step2b) && /费率/.test(step2b) && /最低/.test(step2b))
      || /本行站内转账免手续费/.test(step2b),
      (step2b.match(/本次手续费[\s\S]{0,70}/) || step2b.match(/本行站内转账免手续费[\s\S]{0,40}/) || [''])[0]);
    ok('T-03 金额步同时给出「实际扣款」总额', /实际扣款/.test(step2b));
    log('INFO', '第 2 步可见文本片段: ' + ((step2b.match(/转账金额[\s\S]{0,220}/) || [''])[0]).replace(/\n/g, ' | ').slice(0, 220));
    const warn = /需完成安全验证/.test(step2b);
    ok('T-01b 转 3000 给常用收款人时出现"需安全验证"预告（阈值 2000 生效）', warn, { warn });

    /* ============ T-04 付款账户三态 + 余额不可得挡下一步 ============ */
    // 余额在应用启动时就被父级拉过一次，仅拦请求不够 —— 拦掉后整页重载，让它从"取不到"开始
    await h.b.send('Network.setBlockedURLs', { urls: ['*/api/balance*'] }, h.sid);
    await h.eval(`location.hash = '#/transfer'`);
    await h.goto(null, { reload: true });
    await sleep(5200);
    const pick2 = await h.eval(`(()=>{
      const cands=[...document.querySelectorAll('div,button')].filter(e=>{
        const t=(e.innerText||'').trim();
        return /^(妈妈|李雷|房东王姐|张伟)/.test(t) && t.length < 60 && (e.offsetParent||e.getClientRects().length);
      });
      if (!cands.length) return 'no-payee';
      cands[cands.length-1].click(); return 'ok';
    })()`);
    await sleep(1200);
    await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/下一步/.test(x.innerText||'')&&!x.disabled);b&&b.click();return 1})()`);
    await sleep(2000);
    const t4 = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix4_transfer_no_balance.png'));
    const nextDisabled = await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/下一步/.test(x.innerText||''));return b?b.disabled:null})()`);
    ok('T-04 ★余额接口失败时付款账户区渲染错误态（不再是空白）',
      /Mock|暂不可用|重试/.test(t4), { pick2, sample: (t4.match(/[^\n]{0,30}(Mock|暂不可用|重试)[^\n]{0,20}/) || [''])[0] });
    ok('T-04 ★余额不可得时"下一步"不可点', nextDisabled === true, { nextDisabled });
    await h.b.send('Network.setBlockedURLs', { urls: [] }, h.sid);

    log('RESULT', `PASS=${pass} FAIL=${fail}`);
  } catch (e) {
    fail++; log('EXCEPTION', String((e && e.message) || e));
  } finally { await h.close().catch(() => {}); }
  process.exit(fail === 0 ? 0 : 1);
})();
