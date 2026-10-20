/* 真机验证：转账页「跨行手续费」展示（修复 <¥5 漏收后的前端表现）
 * 目标：小额 ¥4.99 现在必须显示 ¥1.00 手续费（修复前显示 ¥0.00 /「站内免手续费」）。
 * 纪律：净副作用 0 —— 只走到「核对」步读文案，绝不提交（不输入支付密码）。
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const LOG = '_reports/_e2e_fee_fix.log';
const log = (...a) => {
  try { fs.appendFileSync(LOG, a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {}
};

const VISFN = "e=>!!(e.offsetParent||e.getClientRects().length)";

/* React 受控 input 必须用原生 setter + input 事件，否则 React 收不到 */
async function setAmount(h, val) {
  return await h.eval(`(() => {
    const inp = [...document.querySelectorAll('input')].filter(${VISFN}).find(i => i.placeholder === '0.00');
    if (!inp) return 'no-input';
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(inp, ${JSON.stringify(String(val))});
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    return 'ok';
  })()`);
}

function readFeeExpr() {
  return `(() => {
    const t = document.body.innerText || '';
    const m = t.match(/本次手续费[\\s\\S]{0,90}/);
    const conf = t.match(/手续费[\\s\\S]{0,60}/g) || [];
    const inputs = [...document.querySelectorAll('input')].filter(${VISFN})
      .map(i => ({ ph: i.placeholder, v: i.value }));
    return JSON.stringify({ amountLine: m ? m[0].replace(/\\n/g, ' ').slice(0, 120) : null,
                            feeRows: conf.slice(0, 3).map(s => s.replace(/\\n/g, ' ').slice(0, 80)),
                            inputs });
  })()`;
}

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1280, height: 800 });
  const R = {};
  try {
    await h.goto(H.BASE + '/');
    await h.clearToken();
    await h.eval("localStorage.clear(); localStorage.setItem('xb_user','user1')");
    await h.setToken(process.env.XB_TOKEN || '');
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.nav('/transfer');
    await h.waitForText('选择收款人', 12000);

    /* 1) 选一个跨行收款人（卡片右侧有「跨行」徽标） */
    R.pick = await h.eval(`(() => {
      const b = [...document.querySelectorAll('button')].filter(${VISFN})
        .find(x => /跨行/.test(x.innerText || '') && /尾号/.test(x.innerText || ''));
      if (!b) return 'not-found';
      b.click();
      return (b.innerText || '').replace(/\\n/g, ' ').slice(0, 60);
    })()`);
    await H.sleep(600);
    log('PICK=' + R.pick);

    /* 2) 下一步 → 金额步 */
    R.next = await h.clickByText('下一步', { tag: 'button', visibleOnly: true });
    await H.sleep(800);
    await h.screenshot('_reports/_e2e_fee_1_amount.png');

    /* 3) 小额 ¥4.99（修复前 = 0 手续费） */
    await setAmount(h, '4.99');
    await H.sleep(1600);
    R.small = JSON.parse(await h.eval(readFeeExpr()));
    await h.screenshot('_reports/_e2e_fee_2_small.png');
    log('SMALL_4.99=' + JSON.stringify(R.small));

    /* 4) ¥5000 → 应 0.1% = ¥5.00 */
    await setAmount(h, '5000');
    await H.sleep(1600);
    R.mid = JSON.parse(await h.eval(readFeeExpr()));
    log('MID_5000=' + JSON.stringify(R.mid));

    /* 5) 走到「核对」步，确认费用明细行 */
    R.next2 = await h.clickByText('下一步', { tag: 'button', visibleOnly: true });
    await H.sleep(1200);
    R.confirm = JSON.parse(await h.eval(`(() => {
      const t = (document.body.innerText || '');
      const i = t.indexOf('手续费');
      return JSON.stringify({ hasFeeRow: i >= 0,
        around: i >= 0 ? t.slice(Math.max(0, i - 70), i + 90).replace(/\\n/g, ' ') : null });
    })()`));
    await h.screenshot('_reports/_e2e_fee_3_confirm.png');
    log('CONFIRM=' + JSON.stringify(R.confirm));

    /* 净副作用 0：不提交，直接离开 */
    await h.nav('/chat');
    log('DONE');
  } catch (e) {
    log('ERR ' + (e && e.stack || e));
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(0);
  }
})();
