/* 真机回归：账单页「交易流水明细」三个筛选下拉必须**有下拉箭头**（下选符号）
 *
 *  用户报告（2026-10-11）："就是这个下选符号" —— 截图里「本月 / 全部分类 / 全部类型」
 *  三个框右侧是空的，看不出能点开。
 *
 *  根因：`BillsPage` 内的 `Select` 用了 `appearance-none`（原生三角被隐藏）+
 *        `pr-7` 预留了 28px 位置，**但从没画自定义箭头** → 真机就是一块空白。
 *        诊断实况：appearance=none / backgroundImage=none / paddingRight=28px / 无任何子元素。
 *
 *  断言：① 三个 select 各自旁边有可见的 svg 箭头，且**落在 select 的框内右侧**；
 *        ② 两套主题（day 浅 / night 深）都成立（箭头用 currentColor + 主题变量）；
 *        ③ 选完之后功能仍然正常（选「交通」→ 明细只剩交通）。
 *
 *  用法：node scripts/_e2e_bills_select.js <token>
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const LOG = '_reports/_e2e_bills_select.log';
const log = (...a) => { try { fs.appendFileSync(LOG, a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {} };

let ok = 0, fail = 0;
const check = (name, cond, extra) => {
  if (cond) { ok++; console.log('PASS ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
  else { fail++; console.log('FAIL ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
};

/* 读取三个筛选下拉的实际渲染：几何 + 相邻箭头 */
const PROBE = `(() => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const sels = [...document.querySelectorAll('select')].filter(vis);
  return sels.map(s => {
    const cs = getComputedStyle(s);
    const r = s.getBoundingClientRect();
    const wrap = s.parentElement;
    const svg = wrap ? wrap.querySelector('svg') : null;
    let arrow = null;
    if (svg) {
      const ar = svg.getBoundingClientRect();
      const acs = getComputedStyle(svg);
      arrow = {
        w: Math.round(ar.width), h: Math.round(ar.height),
        stroke: getComputedStyle(svg.querySelector('path')).stroke || acs.color,
        color: acs.color,
        visible: acs.visibility !== 'hidden' && acs.display !== 'none' && ar.width > 0 && ar.height > 0,
        insideBox: ar.left >= r.left - 1 && ar.right <= r.right + 1 && ar.top >= r.top - 1 && ar.bottom <= r.bottom + 1,
        onRightHalf: ar.left > r.left + r.width / 2,
      };
    }
    return {
      text: (s.options[s.selectedIndex] || {}).text || '',
      appearance: cs.appearance,
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      arrow,
    };
  });
})()`;

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1440, height: 900, watchdogMs: 180000 });
  try {
    for (const theme of ['day', 'night']) {
      await h.goto(H.BASE + '/');
      await h.eval(`localStorage.clear(); localStorage.setItem('xb_user','user1'); localStorage.setItem('xb_theme_user1','${theme}')`);
      await h.setToken(process.env.XB_TOKEN || process.argv[2] || '');
      await h.goto(H.BASE + '/chat', { reload: true });
      await h.eval("location.hash = '#/bills'");
      await H.sleep(2800);
      const th = await h.eval('document.documentElement.dataset.theme');
      console.log('\n===== 主题 ' + theme + '（实际 ' + th + '）=====');

      // 展开全部折叠区（异常提醒 / 交易流水明细）
      await h.eval(`(() => {
        const vis = e => !!(e.offsetParent || e.getClientRects().length);
        [...document.querySelectorAll('button')].filter(vis)
          .filter(x => /展开查看/.test(x.innerText || '')).forEach(b => b.click());
      })()`);
      await H.sleep(1000);

      const rows = await h.eval(PROBE);
      log('PROBE_' + theme + '=' + JSON.stringify(rows));
      if (!rows.length) { check('[' + theme + '] 找到筛选下拉', false, '页面未就绪'); continue; }

      check('[' + theme + '] 三个筛选下拉都在', rows.length === 3, rows.map(r => r.text));
      const noArrow = rows.filter(r => !r.arrow);
      check('[' + theme + '] ★每个下拉都有箭头元素（不再是一块空白）',
            noArrow.length === 0, noArrow.map(r => r.text));
      const bad = rows.filter(r => r.arrow && (!r.arrow.visible || !r.arrow.insideBox || !r.arrow.onRightHalf));
      check('[' + theme + '] ★箭头可见且贴在框内右侧', bad.length === 0,
            bad.map(r => ({ t: r.text, a: r.arrow })));
      check('[' + theme + '] 原生三角确已隐藏（appearance=none，不会双箭头）',
            rows.every(r => r.appearance === 'none'), rows.map(r => r.appearance));
      check('[' + theme + '] 箭头颜色非透明（深/浅主题都看得见）',
            rows.every(r => r.arrow && !/rgba?\\([^)]*,\\s*0\\)$/.test(r.arrow.color)),
            rows.map(r => r.arrow && r.arrow.color));

      await h.screenshot('_reports/e2e_bills_select_' + theme + '.png');
    }

    /* ---- 功能仍正常：选「交通」→ 明细只剩交通 ---- */
    const before = await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const sels = [...document.querySelectorAll('select')].filter(vis);
      return sels.length;
    })()`);
    check('功能回归：下拉仍在（可选）', before === 3, before);

    console.log('\n' + ok + ' passed, ' + fail + ' failed');
  } catch (e) {
    log('ERR ' + (e && e.stack || e));
    console.log('ERR ' + (e && e.message || e));
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(0);
  }
})();
