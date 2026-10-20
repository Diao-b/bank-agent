/* 真机回归：**全新账号的账单页必须是空的**（用户报"新注册账号怎么会有账单记录"）
 *
 *  前置：由 scripts/test_register_clean_account.py 注册一个干净账号并把 token 传进来。
 *
 *  断言（全部要读真实渲染结果）：
 *    A. 页面进得去、没有白屏；本月笔数 = 0
 *    B. 分类占比区显示空态「本月暂无支出记录」
 *    C. 近 3 个月趋势显示空态「近 3 个月暂无支出记录」
 *    D. 异常提醒没有"待确认"条目（干净账号不该有疑似重复扣费）
 *    E. 本月总支出 = ¥0.00（先关隐私模式，否则金额被脱敏成 ¥···）
 *    F. 截图留档
 *
 *  用法：node scripts/_e2e_bills_empty.js <用户名>
 */
const H = require('./_e2e_lib.js');
const path = require('path');

const USER = process.argv[2] || 'clean_user';
let ok = 0, fail = 0;
const check = (name, cond, extra) => {
  if (cond) { ok++; console.log('PASS ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
  else { fail++; console.log('FAIL ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
};

(async () => {
  const h = await H.launch({ width: 1500, height: 1000, watchdogMs: 120000 });
  try {
    await h.goto(H.BASE + '/');
    await h.eval(`localStorage.clear(); localStorage.setItem('xb_user', ${JSON.stringify(USER)})`);
    await h.setToken(process.env.XB_TOKEN || '');
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.eval("location.hash = '#/bills'");
    /* ★ 必须等汇总行真的渲染出来再读 —— 直接 sleep 会在数据没返回时取到空文本，
       断言就变成假阴性（实测踩过：读数时 body 才 549 字，截图时页面才渲染完）。 */
    const ready = await h.waitForText('本月笔数', 20000);
    if (!ready) await h.waitForText('账单分析', 5000);
    await H.sleep(700);

    // 关掉隐私模式，否则金额是 ¥···（读不到数字）
    const priv = await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const b = [...document.querySelectorAll('button')].filter(vis).find(x => /隐私/.test(x.innerText || ''));
      return b ? (b.innerText || '').replace(/\s+/g, ' ').trim() : null;
    })()`);
    if (priv && /开/.test(priv)) {
      await h.eval(`(() => {
        const vis = e => !!(e.offsetParent || e.getClientRects().length);
        const b = [...document.querySelectorAll('button')].filter(vis).find(x => /隐私/.test(x.innerText || ''));
        if (b) b.click();
      })()`);
      await H.sleep(1500);
    }

    /* ★ 模板字符串陷阱：eval 目标里凡是 `\n` 都必须写成 `\\n`，
       否则会被转义成真换行 —— 正则变 "missing /"、注释被截断成代码，两种语法错都踩过 */
    const info = await h.eval(`(() => {
      const t = (document.body && document.body.innerText) || '';
      const m = t.match(/本月笔数\\s*(\\d+)/);
      const tot = t.match(/本月总支出\\s*¥?([\\d,]+\\.\\d{2})/);
      return {
        hasBillPage: /账单分析/.test(t),
        txCount: m ? parseInt(m[1], 10) : null,
        totalExpense: tot ? parseFloat(tot[1].replace(/,/g, '')) : null,
        pieEmpty: /本月暂无支出记录/.test(t),
        trendEmpty: /近 3 个月暂无支出记录/.test(t),
        anomalyPanel: /异常提醒/.test(t),
        anomalyItems: /项待确认/.test(t),
        wealthPanel: /理财买卖/.test(t),
        bodyLen: t.length,
        head: t.replace(/\\n/g, ' | ').slice(0, 220),
        summaryLine: (t.match(/本月总支出[^\\n]{0,60}/) || [''])[0],
      };
    })()`);
    console.log('INFO ' + JSON.stringify(info));

    check('A1 账单页正常渲染（没有白屏）', info.hasBillPage && info.bodyLen > 300, info.bodyLen);
    check('★A2 本月笔数 = 0', info.txCount === 0, info.txCount);
    check('★A3 本月总支出 = ¥0.00', info.totalExpense === 0, info.totalExpense);
    check('★B1 分类占比显示空态', info.pieEmpty === true);
    check('★C1 近 3 个月趋势显示空态', info.trendEmpty === true);
    check('★D1 异常提醒没有"待确认"条目', info.anomalyItems === false);
    check('E1 隐私模式已关（否则上面读不到数字）', info.totalExpense !== null, info.totalExpense);

    await h.screenshot('_reports/e2e_bills_empty.png');
    console.log('\n' + ok + ' passed, ' + fail + ' failed');
  } catch (e) {
    console.log('ERR ' + (e && e.message || e));
    fail++;
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(fail ? 1 : 0);
  }
})();
