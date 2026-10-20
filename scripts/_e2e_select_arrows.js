/* 真机回归：**全站**下拉箭头统一性（2026-10-12）
 *
 * 背景：账单页三个筛选下拉补上自定义箭头后（见 `_e2e_bills_select.js`），
 *       站内仍是"1 处自定义 + 12 处原生"两套观感。按用户决策统一为同一套自定义箭头。
 *
 * 断言（逐「面」跑）：每个面上的**可见 <select>** 都必须
 *   ① appearance:none（关掉原生三角，避免双箭头）
 *   ② 紧邻一个 chevron 箭头元素（SelectWrap 内）
 *   ③ 箭头可见、贴在框内右半边、颜色非透明（深浅主题都看得见）。
 *
 * 覆盖面：账单筛选 / 理财购买弹窗 / 申请新卡弹窗 / 签约新订阅弹窗 / 后台操作筛选。
 *
 * 用法：node scripts/_e2e_select_arrows.js <token>
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const LOG = '_reports/_e2e_select_arrows.log';
const log = (...a) => { try { fs.appendFileSync(LOG, a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {} };

let ok = 0, fail = 0;
const check = (name, cond, extra) => {
  if (cond) { ok++; console.log('PASS ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
  else { fail++; console.log('FAIL ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
};

const PROBE = `(() => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const sels = [...document.querySelectorAll('select')].filter(vis);
  return sels.map(s => {
    const cs = getComputedStyle(s);
    const r = s.getBoundingClientRect();
    const wrap = s.parentElement;
    const svg = wrap ? wrap.querySelector(':scope > svg') : null;
    let arrow = null;
    if (svg) {
      const ar = svg.getBoundingClientRect();
      const acs = getComputedStyle(svg);
      arrow = {
        w: Math.round(ar.width), h: Math.round(ar.height),
        color: acs.color,
        visible: acs.visibility !== 'hidden' && acs.display !== 'none' && ar.width > 0 && ar.height > 0,
        insideBox: ar.left >= r.left - 1 && ar.right <= r.right + 1 && ar.top >= r.top - 1 && ar.bottom <= r.bottom + 1,
        onRightHalf: ar.left > r.left + r.width / 2,
      };
    }
    return { text: (s.options[s.selectedIndex] || {}).text || '', appearance: cs.appearance, arrow,
             box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] };
  });
})()`;

/* 点最深的那个元素（理财「在售产品」这类需要先展开/下钻的入口） */
const clickDeepest = (h, s) => h.eval(`(() => {
  const cand = [...document.querySelectorAll('*')].filter(e => (e.innerText || '').trim().startsWith(${JSON.stringify(s)}));
  const leaf = cand.filter(e => ![...e.children].some(c => (c.innerText || '').trim().startsWith(${JSON.stringify(s)})));
  (leaf[0] || cand[cand.length - 1] || { click() {} }).click(); return cand.length;
})()`);

async function audit(h, label, minCount) {
  const rows = await h.eval(PROBE);
  log(label + '=' + JSON.stringify(rows));
  check('[' + label + '] 找到下拉 (>= ' + minCount + ')', rows.length >= minCount, rows.map(r => r.text));
  if (!rows.length) return rows;
  const noArrow = rows.filter(r => !r.arrow);
  check('[' + label + '] ★每个下拉都有箭头元素', noArrow.length === 0, noArrow.map(r => r.text));
  const bad = rows.filter(r => r.arrow && (!r.arrow.visible || !r.arrow.insideBox || !r.arrow.onRightHalf));
  check('[' + label + '] ★箭头可见且贴在框内右侧', bad.length === 0, bad.map(r => ({ t: r.text, a: r.arrow })));
  check('[' + label + '] 原生三角已隐藏（appearance=none）',
        rows.every(r => r.appearance === 'none'), rows.map(r => r.appearance));
  check('[' + label + '] 箭头颜色非透明',
        rows.every(r => r.arrow && !/rgba?\([^)]*,\s*0\)$/.test(r.arrow.color)), rows.map(r => r.arrow && r.arrow.color));
  return rows;
}

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const TOK = process.env.XB_TOKEN || process.argv[2] || '';
  const h = await H.launch({ width: 1440, height: 900, watchdogMs: 240000 });
  try {
    // 首页登录态
    await h.goto(H.BASE + '/');
    await h.eval("localStorage.clear(); localStorage.setItem('xb_user','user1'); localStorage.setItem('xb_theme_user1','day')");
    await h.setToken(TOK);
    await h.goto(H.BASE + '/chat', { reload: true });

    // ---------- ① 账单筛选 ----------
    console.log('\n===== ① 账单页筛选 =====');
    await h.eval("location.hash = '#/bills'");
    await H.sleep(2800);
    await h.eval(`(() => { const vis=e=>!!(e.offsetParent||e.getClientRects().length);
      [...document.querySelectorAll('button')].filter(vis).filter(x=>/展开查看/.test(x.innerText||'')).forEach(b=>b.click()); })()`);
    await H.sleep(900);
    await audit(h, '账单筛选', 3);
    await h.screenshot('_reports/e2e_select_arrows_bills.png');

    // ---------- ② 理财购买弹窗 ----------
    console.log('\n===== ② 理财购买弹窗 =====');
    await h.eval("location.hash = '#/wealth'"); await H.sleep(4200);
    try {
      await clickDeepest(h, '在售产品'); await H.sleep(900);   // 先展开「在售产品」区
      await h.clickByText('立即购买', { tag: 'button' });
      await h.waitForText('直接购买', 8000);
      await H.sleep(600);
      await audit(h, '理财购买弹窗', 1);
      await h.screenshot('_reports/e2e_select_arrows_wealth.png');
    } catch (e) { check('[理财购买弹窗] 能打开（点「立即购买」）', false, String(e && e.message || e).slice(0, 120)); }

    // ---------- ③ 申请新卡弹窗 ----------
    console.log('\n===== ③ 申请新卡弹窗 =====');
    await h.eval("location.hash = '#/cards'"); await H.sleep(2600);
    try {
      await h.clickByText('申请新卡', { tag: 'button' }); await H.sleep(900);
      await audit(h, '申请新卡弹窗', 1);
      await h.screenshot('_reports/e2e_select_arrows_cards.png');
    } catch (e) { check('[申请新卡弹窗] 能打开', false, String(e && e.message || e).slice(0, 120)); }

    // ---------- ④ 签约新订阅弹窗 ----------
    console.log('\n===== ④ 签约新订阅弹窗 =====');
    await h.eval("location.hash = '#/subs'"); await H.sleep(2600);
    try {
      await h.clickByText('签约新订阅', { tag: 'button' }); await H.sleep(900);
      await audit(h, '签约新订阅弹窗', 1);
      await h.screenshot('_reports/e2e_select_arrows_subs.png');
    } catch (e) { check('[签约新订阅弹窗] 能打开', false, String(e && e.message || e).slice(0, 120)); }

    // ---------- ⑤ 后台操作筛选 ----------
    console.log('\n===== ⑤ 后台操作筛选 =====');
    const ADMIN = process.env.XB_ADMIN_TOKEN || process.argv[3] || '';
    if (!ADMIN) {
      console.log('SKIP [后台筛选] 未提供管理员 token（设 XB_ADMIN_TOKEN 或传第 3 个参数）');
    } else {
      try {
        await h.eval("localStorage.setItem('xb_token'," + JSON.stringify(ADMIN) + ")");
        await h.goto(H.BASE + '/admin', { reload: true }); await H.sleep(3600);
        const denied = await h.eval("/无管理员权限|403/.test(document.body.innerText)");
        if (denied) console.log('SKIP [后台筛选] 该账号无管理员权限，跳过');
        else {
          await h.clickByText('操作审计', { tag: 'button' }); await H.sleep(1400);  // 筛选下拉在「操作审计」Tab
          await audit(h, '后台筛选', 1);
          await h.screenshot('_reports/e2e_select_arrows_admin.png');
        }
      } catch (e) { console.log('SKIP [后台筛选] ' + String(e && e.message || e).slice(0, 100)); }
    }

    console.log('\n' + ok + ' passed, ' + fail + ' failed');
  } catch (e) {
    log('ERR ' + (e && e.stack || e));
    console.log('ERR ' + (e && e.message || e));
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(0);
  }
})();
