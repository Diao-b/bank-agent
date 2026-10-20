/* A2：深链直达（fresh navigate 到各 hash 路由） */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/A2.log');

const ROUTES = [
  { hash: '/cards',      name: 'cards',      expect: /卡号|储蓄卡|信用卡|持有卡片/ },
  { hash: '/bills',      name: 'bills',      expect: /账单|支出|消费|分类/ },
  { hash: '/wealth',     name: 'wealth',     expect: /理财|产品|风险|起购|持仓/ },
  { hash: '/subs',       name: 'subs',       expect: /订阅|周期|扣款/ },
  { hash: '/safe',       name: 'safe',       expect: /安全|隐私|支付密码|冻结/ },
  { hash: '/transfer',   name: 'transfer',   expect: /转账|收款人|金额/ },
  { hash: '/profile',    name: 'profile',    expect: /个人信息|资料|称呼|实名/ },
  { hash: '/realname',   name: 'realname',   expect: /实名|身份证|认证/ },
  { hash: '/appearance', name: 'appearance', expect: /形象|数字人|外观|预览/ },
];

(async () => {
  const h = await launch();
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false });
  await h.setToken(TOK);
  await h.goto(BASE + '/chat', { reload: true });
  await sleep(1500);
  log('login_state', await h.eval(`/已接入真实账户/.test(document.body.innerText||'')`));

  for (const r of ROUTES) {
    // 关键：先离开文档（about:blank），再加载目标 URL —— 才是"粘链接/书签"的真·全新加载，
    // 否则同文档的 hash 导航会被路由逻辑改写，测不到首屏行为。
    await h.goto('about:blank', { reload: false });
    await h.goto(BASE + '/chat#' + r.hash, { reload: false });
    await sleep(2000);
    const res = await h.eval(`(() => {
      const t = document.body.innerText || '';
      const overlay = [...document.querySelectorAll('div')].filter(d => typeof d.className==='string' && d.className.includes('fixed inset-0'));
      const modalText = overlay.length ? (overlay[overlay.length-1].innerText||'').replace(/\\s+/g,' ').slice(0,160) : '';
      return JSON.stringify({ hash: location.hash, expectHit: ${r.expect}.test(t), modalOpen: overlay.length>0, modalText });
    })()`);
    log('route:' + r.hash, res);
    await h.screenshot(SHOTS + '/A2-' + r.name + '.png');
  }

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
