/* H2：操作日志颜色（缺陷#1）+ 隐私开关行为 */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/H2_safe.log');

const readLogs = `(() => {
  const ols = [...document.querySelectorAll('ol')].map(o=>o.querySelectorAll(':scope > li').length);
  const mi = ols.indexOf(Math.max(...ols, 0));
  const ol = [...document.querySelectorAll('ol')][mi];
  if(!ol) return JSON.stringify({err:'no ol'});
  const rows = [...ol.querySelectorAll(':scope > li')].slice(0,12).map(li => {
    const spans=[...li.querySelectorAll('span')];
    const circle=spans[0];
    const resultEl=spans.find(s=>/^(成功|失败|未实现|success|failed)$/.test((s.innerText||'').trim()));
    return {
      text:(li.innerText||'').replace(/\\s+/g,' ').slice(0,70),
      circleBg: circle?getComputedStyle(circle).backgroundColor:null,
      resultColor: resultEl?getComputedStyle(resultEl).color:null,
    };
  });
  return JSON.stringify({liCount:[...ol.querySelectorAll(':scope > li')].length, rows});
})()`;

(async () => {
  const h = await launch({ watchdogMs: 180000 });
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/safe', { reload: false }); await sleep(4000);
  log('op_logs', await h.eval(readLogs));
  await h.screenshot(SHOTS + '/H2-01-oplogs.png');

  // 隐私开关：在账单页读金额明文/脱敏
  const amounts = `(() => { const t=document.body.innerText||'';
    return JSON.stringify({ masked: (t.match(/¥ ··· ··|¥ ••••|隐私模式/g)||[]).length, plain: (t.match(/¥[\\d,]+\\.\\d\\d/g)||[]).slice(0,8) }); })()`;
  await h.goto(BASE + '/chat#/bills', { reload: false }); await sleep(3000);
  log('bills_privacy_ON', await h.eval(amounts));
  // 点顶栏隐私开关
  const tog = await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>/隐私/.test(x.innerText||'')); if(b){b.click(); return (b.innerText||'').trim();} return null; })()`);
  log('privacy_toggle_clicked', tog);
  await sleep(1200);
  log('bills_privacy_afterToggle', await h.eval(amounts));
  await h.screenshot(SHOTS + '/H2-02-privacy-toggled.png');
  await sleep(11000);
  log('bills_privacy_after11s', await h.eval(amounts));
  await h.screenshot(SHOTS + '/H2-03-privacy-11s.png');
  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
