/* E2E：弹窗按钮必须有「按钮框」（2026-10-11）
 * 用法：node scripts/_e2e_modal_buttons.js <user_token>
 *
 * 覆盖：
 *   A. ★真机：赎回 → gate 阶段（取消 / 确认，开始办理）—— 只断言样式，**不点确认**，不动资金；
 *   B. ★真机：直接购买填「超余额」金额 → 断言**前端原地拦截**（提示余额不足、不提交、不动资金）
 *      —— 注：加了「付款账户」预检后，超余额不再走到后端结果弹窗，故断言改为拦截图示行为；
 *   C. 源码级：结果弹窗的「完成」按钮（原 bug 那个）必须由自带背景/内边距的 `btnGold` 渲染
 *      —— 在不移动资金、不改安全状态的前提下无法再从 UI 走到那个弹窗（要走到它必须真扣款
 *         或先把支付密码置为锁定态），所以这一条用源码断言兜底，防止有人把它改回裸文字。
 * 断言方式（A/B）是读 computedStyle：修复前这些按钮是裸文字（无背景 / 无内边距 / 无边框）。
 */
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const log = H.makeLogger(path.resolve('_reports/e2e_modal_buttons.log'));
const TOK = process.argv[2];
if (!TOK) { console.error('缺少 token 参数'); process.exit(2); }

let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; log('PASS', name + (extra !== undefined ? '  ' + JSON.stringify(extra) : '')); }
  else { fail++; log('FAIL', name + '  ' + JSON.stringify(extra)); }
};

/* 取按钮的 computed style 关键项（同名可能多个 → 取最后一个可见的） */
const btnStyle = (h, text) => h.eval(`(() => {
  const bs = [...document.querySelectorAll('button')]
    .filter(b => (b.innerText || '').trim() === ${JSON.stringify(text)} && b.offsetParent !== null);
  if (!bs.length) return null;
  const cs = getComputedStyle(bs[bs.length - 1]);
  return { bgImage: cs.backgroundImage, bgColor: cs.backgroundColor,
           borderW: parseFloat(cs.borderTopWidth), borderStyle: cs.borderTopStyle,
           padT: parseFloat(cs.paddingTop), padL: parseFloat(cs.paddingLeft),
           radius: parseFloat(cs.borderTopLeftRadius) };
})()`);

const clickDeepest = (h, startsWith) => h.eval(`(() => {
  const cand = [...document.querySelectorAll('*')].filter(e => (e.innerText || '').trim().startsWith(${JSON.stringify(startsWith)}));
  const leaf = cand.filter(e => ![...e.children].some(c => (c.innerText || '').trim().startsWith(${JSON.stringify(startsWith)})));
  (leaf[0] || cand[cand.length - 1] || { click() {} }).click();
  return cand.length;
})()`);

const isFramed = (s) => !!(s && ((s.borderW > 0 && s.borderStyle !== 'none') || /gradient/.test(s.bgImage || '')
                             || (s.bgColor && s.bgColor !== 'rgba(0, 0, 0, 0)')));
const isGradient = (s) => !!(s && /gradient/.test(s.bgImage || ''));

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  await h.setViewport(1440, 920);

  await h.goto(H.BASE + '/login');
  await h.eval(`localStorage.setItem('xb_token', ${JSON.stringify(TOK)}); localStorage.setItem('xb_user','刁伟民');`);
  await h.goto(H.BASE + '/chat#/wealth');
  await sleep(4200);

  /* ---------- A. 赎回 → gate 阶段（只断言样式，不确认） ---------- */
  await clickDeepest(h, '我的持仓');
  await sleep(900);
  ok('停在「我的持仓」并可赎回', await h.waitForText('赎回', 8000), null);
  await h.clickByText('赎回', { tag: 'button', exact: true });
  ok('打开 gate 弹窗（含确认提示）', await h.waitForText('确认赎回', 8000), null);
  await sleep(500);

  const cancelS = await btnStyle(h, '取消');
  ok('A1 gate「取消」有按钮框（边框/背景）', isFramed(cancelS), cancelS);
  ok('A2 gate「取消」有内边距', !!cancelS && cancelS.padT > 0 && cancelS.padL > 0, cancelS && (cancelS.padT + '/' + cancelS.padL));
  const goS = await btnStyle(h, '确认，开始办理');
  ok('A3 gate「确认，开始办理」有渐变背景', isGradient(goS), goS && goS.bgImage);
  ok('A4 gate「确认，开始办理」有内边距', !!goS && goS.padT > 0, goS && goS.padT);

  /* 关闭弹窗（不执行） */
  await h.clickByText('关闭', { tag: 'button' });
  await sleep(700);

  /* ---------- B. 直接购买（超余额）→ 结果弹窗 ---------- */
  await clickDeepest(h, '在售产品');
  await sleep(900);
  await h.clickByText('立即购买', { tag: 'button' });
  ok('B0 打开「直接购买」弹窗', await h.waitForText('直接购买', 8000), null);

  const amtSel = '[data-modal-scrim] input[type="number"]';
  await h.type(amtSel, '999999');            // 远超余额 → 前端预检必须原地拦下
  await sleep(300);
  const cb = '[data-modal-scrim] input[type="checkbox"]';
  if (await h.exists(cb)) { await h.clickBySelector(cb); await sleep(200); }
  await h.clickByText('下一步（安全核验）', { tag: 'button' });
  await sleep(1600);

  const guard = await h.eval(`(() => {
    const vis = e => !!(e.offsetParent || e.getClientRects().length);
    const scrims = [...document.querySelectorAll('[data-modal-scrim]')].filter(vis);
    const dlg = scrims[scrims.length - 1];
    if (!dlg) return null;
    const txt = dlg.innerText || '';
    return { open: true, inlineErr: /不足支付/.test(txt), resultModal: /未办理完成|办理完成/.test(txt),
             stillForm: /购买金额/.test(txt) };
  })()`);
  log('GUARD', JSON.stringify(guard));
  ok('★B1 超余额 → 前端原地拦截并提示余额不足', !!guard && guard.inlineErr, guard);
  ok('★B2 仍停在表单（没有空跑一趟后端结果弹窗）',
     !!guard && guard.stillForm && !guard.resultModal, guard);
  ok('B3 拦截时不提交（资金未发生变动 —— 由后端余额断言另行覆盖）', !!guard && guard.open, guard);

  /* C. 源码级兜底：结果弹窗「完成」按钮必须用自带背景/内边距的 btnGold */
  const src = require('fs').readFileSync(path.resolve('web/index.html'), 'utf8');
  const goldDef = (src.match(/const btnGold = '([\s\S]{0,400}?)';/) || [])[1] || '';
  ok('★C1 结果弹窗「完成」按钮引用 btnGold（非裸文字）',
     /className=\{btnGold\}>完成<\/button>/.test(src), null);
  ok('★C2 btnGold 自带渐变背景', /linear-gradient/.test(goldDef), goldDef.slice(0, 90));
  ok('★C3 btnGold 自带内边距（修复前 padding 为 0）', /px-5|px-4|py-2\.5|py-2/.test(goldDef),
     goldDef.match(/px-\d+(\.\d+)?|py-\d+(\.\d+)?/g));

  await h.screenshot(path.resolve('_reports/e2e_modal_buttons.png'), { full: false });
  log('SHOT', '_reports/e2e_modal_buttons.png');
  console.log(`\n${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('ERR', e); process.exit(1); });
