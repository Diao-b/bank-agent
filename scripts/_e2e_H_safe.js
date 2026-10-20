/* H：安全中心（操作日志颜色=已知缺陷#1；隐私脱敏；支付密码入口；冻结入口） */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/H_safe.log');

(async () => {
  const h = await launch();
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/safe', { reload: false }); await sleep(3500);
  log('realMode', /已接入真实账户/.test(await h.bodyText()));

  // 操作日志：逐条取「动作文案 / 结果文案 / 圆点计算色 / 结果徽标色」
  const logs = await h.eval(`(() => {
    // 找含"操作日志"标题的面板
    const panel = [...document.querySelectorAll('div')].find(d => (d.innerText||'').startsWith('操作日志')) || document.body;
    const lis = [...(panel.querySelectorAll('ol > li'))];
    const rows = lis.slice(0, 12).map(li => {
      const circle = li.querySelector('span');
      const cs = circle ? getComputedStyle(circle) : null;
      const spans = [...li.querySelectorAll('span,div')].map(s=>(s.innerText||'').trim()).filter(Boolean);
      const resultEl = [...li.querySelectorAll('span')].find(s => /成功|失败|未实现|success|failed/.test(s.innerText||''));
      const rcs = resultEl ? getComputedStyle(resultEl) : null;
      return {
        text: (li.innerText||'').replace(/\\s+/g,' ').slice(0,90),
        circleBg: cs ? cs.backgroundColor : null,
        resultColor: rcs ? rcs.color : null,
        raw: spans.slice(0,4),
      };
    });
    return JSON.stringify({ count: lis.length, rows });
  })()`);
  log('op_logs', logs);
  await h.screenshot(SHOTS + '/H01-safe-oplogs.png');

  // 三条统计
  const stats = await h.eval(`(() => {
    const t = (document.body.innerText||'');
    const grab = (label) => { const m = t.match(new RegExp(label + '\\\\s*\\\\n?\\\\s*([^\\\\n]{0,24})')); return m?m[1].trim():null; };
    return JSON.stringify({ total: grab('累计操作数'), verify: grab('身份核验次数'), blocked: grab('拦截次数'), head: t.replace(/\\s+/g,' ').slice(0,200) });
  })()`);
  log('stats', stats);

  // 支付密码核验日志面板
  const pwdPanel = await h.eval(`(() => {
    const p = [...document.querySelectorAll('div')].find(d => (d.innerText||'').startsWith('支付密码核验日志'));
    return JSON.stringify({ present: !!p, text: p ? (p.innerText||'').replace(/\\s+/g,' ').slice(0,240) : null });
  })()`);
  log('pwdPanel', pwdPanel);

  // 隐私开关：读当前态 → 点一次 → 读财富页金额是否脱敏
  const priv = await h.eval(`(() => {
    const btns = [...document.querySelectorAll('button')].filter(b=>/隐私/.test(b.innerText||''));
    return JSON.stringify({ privacyBtns: btns.map(b=>(b.innerText||'').replace(/\\s+/g,' ').trim()) });
  })()`);
  log('privacy_btn', priv);
  await h.screenshot(SHOTS + '/H02-safe-full.png');

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
