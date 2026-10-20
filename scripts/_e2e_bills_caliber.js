/* E2E：账单分析页「理财买卖」新块 + 口径文案（2026-10-11）
 * 用法：node scripts/_e2e_bills_caliber.js <user_token>
 * 说明：用 token 直接注入 localStorage 免登录，打开 /chat#/bills 断言页面文字与截图。
 */
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const log = H.makeLogger(path.resolve('_reports/e2e_bills_caliber.log'));
const TOK = process.argv[2];
if (!TOK) { console.error('缺少 token 参数'); process.exit(2); }

let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; log('PASS', name + (extra !== undefined ? '  ' + JSON.stringify(extra) : '')); }
  else { fail++; log('FAIL', name + '  ' + JSON.stringify(extra)); }
};

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  await h.setViewport(1440, 920);

  await h.goto(H.BASE + '/login');
  await h.eval(`localStorage.setItem('xb_token', ${JSON.stringify(TOK)}); localStorage.setItem('xb_user','刁伟民');`);
  await h.goto(H.BASE + '/chat#/bills');
  await sleep(4200);

  /* ★ 隐私模式默认「开」→ 金额全渲染成 ¥···，正则取不到数字（老版本这里恒 FAIL，是假阴性）。
     像用户那样先点开顶栏「隐私」把它关掉，再读数字。 */
  const privBefore = await h.eval(`(() => {
    const vis = e => !!(e.offsetParent || e.getClientRects().length);
    const b = [...document.querySelectorAll('button')].filter(vis).find(x => /隐私/.test(x.innerText || ''));
    return b ? (b.innerText || '').replace(/\\s+/g, ' ').trim() : null;
  })()`);
  log('PRIVACY_BEFORE', JSON.stringify(privBefore));
  if (privBefore && /开/.test(privBefore)) {
    await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const b = [...document.querySelectorAll('button')].filter(vis).find(x => /隐私/.test(x.innerText || ''));
      if (b) b.click();
    })()`);
    await sleep(1600);
  }

  const txt = await h.bodyText();
  ok('页面已进入账单分析', /账单分析/.test(txt), null);
  ok('隐私已关（否则金额被脱敏、下面读不到数字）', /¥[\d,]+\.\d{2}/.test(txt),
     (await h.eval(`(() => {
       const vis = e => !!(e.offsetParent || e.getClientRects().length);
       const b = [...document.querySelectorAll('button')].filter(vis).find(x => /隐私/.test(x.innerText || ''));
       return b ? (b.innerText || '').replace(/\\s+/g, ' ').trim() : null;
     })()`)));
  ok('出现「理财买卖」区块', /理财买卖/.test(txt), null);
  ok('出现「内部划转 · 不计入支出/收入」口径说明', /内部划转/.test(txt) && /不计入支出/.test(txt), null);
  ok('出现 申购/赎回/净买入 三列', /申购/.test(txt) && /赎回/.test(txt) && /净买入/.test(txt), null);
  ok('笔数旁标注「不含理财划转」', /不含理财划转/.test(txt), null);
  const m = txt.match(/本月总支出\s*¥?([\d,]+\.\d{2})/);
  ok('本月总支出已按新口径（应为 40 左右、不含理财）', m && parseFloat(m[1].replace(/,/g, '')) < 1000,
     m ? m[1] : '未匹配');

  await h.screenshot(path.resolve('_reports/e2e_bills_caliber.png'), { full: true });
  log('SHOT', '_reports/e2e_bills_caliber.png');
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('ERR', e); process.exit(1); });
