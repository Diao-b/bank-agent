/* E2E：理财「直接购买」弹窗必须有付款账户选择 + 确认卡显示扣款账户（2026-10-11）
 * 用法：node scripts/_e2e_wealth_pay_account.js <user_token>
 */
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const log = H.makeLogger(path.resolve('_reports/e2e_wealth_pay_account.log'));
const TOK = process.argv[2];
if (!TOK) { console.error('缺少 token'); process.exit(2); }

let pass = 0, fail = 0;
const ok = (n, c, e) => { if (c) { pass++; log('PASS', n + (e !== undefined ? '  ' + JSON.stringify(e) : '')); } else { fail++; log('FAIL', n + '  ' + JSON.stringify(e)); } };
const clickDeepest = (h, s) => h.eval(`(() => {
  const cand = [...document.querySelectorAll('*')].filter(e => (e.innerText || '').trim().startsWith(${JSON.stringify(s)}));
  const leaf = cand.filter(e => ![...e.children].some(c => (c.innerText || '').trim().startsWith(${JSON.stringify(s)})));
  (leaf[0] || cand[cand.length - 1] || { click() {} }).click(); return cand.length;
})()`);

(async () => {
  const h = await H.launch({ width: 1440, height: 980 });
  await h.setViewport(1440, 980);
  await h.goto(H.BASE + '/login');
  await h.eval(`localStorage.setItem('xb_token', ${JSON.stringify(TOK)}); localStorage.setItem('xb_user','刁伟民');`);
  await h.goto(H.BASE + '/chat#/wealth');
  await sleep(4200);

  await clickDeepest(h, '在售产品');
  await sleep(900);
  await h.clickByText('立即购买', { tag: 'button' });
  ok('打开「直接购买」弹窗', await h.waitForText('直接购买', 8000), null);

  const sel = await h.eval(`(() => {
    const s = document.querySelector('[data-modal-scrim] select');
    if (!s) return null;
    return { value: s.value, opts: [...s.options].map(o => o.textContent.trim()),
             ids: [...s.options].map(o => Number(o.value)).filter(n => Number.isFinite(n)) };
  })()`);
  ok('弹窗里有「付款账户」下拉', !!sel, sel && sel.opts.length);
  ok('下拉里有真实账户与余额', !!sel && sel.opts.some(t => /余额/.test(t)), sel && sel.opts.slice(0, 4));
  ok('默认选中 id 最小的账户（与后端默认扣款口径一致）',
     !!sel && sel.ids.length > 0 && sel.value === String(Math.min(...sel.ids)), sel && sel.value);
  const tip = await h.bodyText();
  ok('提示文案写明从哪个账户扣款', /将从「.+」扣款/.test(tip), (tip.match(/将从「.+?」扣款/) || [''])[0]);

  await h.screenshot(path.resolve('_reports/e2e_wealth_pay_account.png'), { full: false });
  log('SHOT', '_reports/e2e_wealth_pay_account.png');
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('ERR', e); process.exit(1); });
