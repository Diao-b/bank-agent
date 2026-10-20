/* C：#/bills 账单分析 —— 图表是否真渲染 / 数值 / 与聊天是否一致 / 布局溢出 */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/C_bills.log');

(async () => {
  const h = await launch({ watchdogMs: 180000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/bills', { reload: false }); await sleep(4000);
  const res = await h.eval(`(() => {
    const t = document.body.innerText || '';
    const grab = (re) => { const m = t.match(re); return m ? m[0] : null; };
    const svgs = [...document.querySelectorAll('svg')];
    const charts = svgs.map(s => ({ w: s.getBoundingClientRect().width|0, h: s.getBoundingClientRect().height|0, paths: s.querySelectorAll('path,polyline,rect,circle').length }));
    const canvases = document.querySelectorAll('canvas').length;
    return JSON.stringify({
      hash: location.hash,
      realMode: /已接入真实账户/.test(t),
      svgCount: svgs.length,
      canvasCount: canvases,
      chartCandidates: charts.filter(c=>c.w>60 && c.h>40),
      hasPieTitle: /分类占比/.test(t),
      hasTrendTitle: /趋势/.test(t),
      total: grab(/总支出[^\\n]{0,20}|本月支出[^\\n]{0,20}|¥[\\d,]+\\.\\d\\d/),
      anomaly: grab(/异常[^\\n]{0,60}/),
      categories: (t.match(/餐饮|购物|交通|娱乐|订阅|转账|其他|医疗|教育|居住/g)||[]).slice(0,12),
      horizontalScroll: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
      docScrollW: document.documentElement.scrollWidth, docClientW: document.documentElement.clientWidth,
      head: t.replace(/\\s+/g,' ').slice(0,400),
    });
  })()`);
  log('bills', res);
  await h.screenshot(SHOTS + '/C01-bills.png');
  await h.screenshot(SHOTS + '/C01-bills-full.png', { full: true });
  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
