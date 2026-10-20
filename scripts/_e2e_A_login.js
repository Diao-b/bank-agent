/* A 任务：登录 / 会话。真实浏览器点击，每条留截图+原文。结果落文件绕过管道缓冲。 */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const RES = 'D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/A.log';
const log = makeLogger(RES);
const USER = process.argv[2] || 'user1';
const PASS = process.argv[3] || '123456';

async function fillLogin(h, user, pass) {
  return await h.eval(`(() => {
    const ins = [...document.querySelectorAll('input')];
    const u = ins.find(e => e.type==='text'||e.type==='email'||(e.placeholder||'').match(/账号|用户|手机|登录/));
    const p = ins.find(e => e.type==='password');
    const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    if(u){ s.call(u, ${JSON.stringify(user)}); u.dispatchEvent(new Event('input',{bubbles:true})); }
    if(p){ s.call(p, ${JSON.stringify(pass)}); p.dispatchEvent(new Event('input',{bubbles:true})); }
    return JSON.stringify({ u: !!u, p: !!p });
  })()`);
}
async function clickLoginBtn(h) {
  const r = await h.clickByText('立即登录', { tag: 'button' });
  if (JSON.parse(r).found > 0) return r;
  return await h.clickByText('登 录', { tag: 'button' });
}
function found(r) { try { return JSON.parse(r).found > 0; } catch (_) { return false; } }

(async () => {
  const h = await launch();
  h._logf = RES;

  // 1) 清 token → 未登录访问 /chat
  await h.goto(BASE + '/chat', { reload: false });
  await h.clearToken();
  await h.goto(BASE + '/chat', { reload: true });
  await sleep(2000);
  const gate = await h.eval(`(() => {
    const inputs = [...document.querySelectorAll('input,textarea')].map(e => ({type:e.type, ph:e.placeholder}));
    const btns = [...document.querySelectorAll('button')].map(b => (b.innerText||'').trim()).filter(Boolean);
    return JSON.stringify({ url: location.href, inputs, btns: btns.slice(0,12) });
  })()`);
  log('1_gate', gate);
  await h.screenshot(SHOTS + '/A01-login-gate.png');

  const gi = JSON.parse(gate);
  const hasForm = gi.inputs.some(i => i.type==='text'||i.type==='email'||i.type==='password');
  log('1_hasForm', hasForm);

  // 2) 错误密码
  await fillLogin(h, USER, 'WRONGPASS');
  await clickLoginBtn(h);
  await sleep(2000);
  const err = await h.eval(`(() => {
    const t = document.body.innerText || '';
    const m = t.match(/.{0,24}(密码|错误|失败|不正确|无效|不对|尝试).{0,20}/g);
    return JSON.stringify({ errMatches: m ? m.slice(0,3): null, stillGate: !/已接入真实账户/.test(t) });
  })()`);
  log('2_wrongPass', err);
  await h.screenshot(SHOTS + '/A02-wrong-password.png');

  // 3) 正确登录
  await fillLogin(h, USER, PASS);
  await clickLoginBtn(h);
  await h.waitForText('已接入真实账户', 15000);
  const after = await h.eval(`(() => { const t=document.body.innerText||''; return JSON.stringify({ mode: /已接入真实账户/.test(t)?'real':(/演示|离线演示/.test(t)?'demo':'unknown'), hash: location.hash, greet: (t.match(/尊敬的.{0,6}(先生|女士)|你好|下午好|上午好|晚上好|早安/)||[''])[0] }); })()`);
  log('3_loginResult', after);
  await h.screenshot(SHOTS + '/A03-logged-in-chat.png');

  // 4) 刷新保活
  await h.goto(BASE + '/chat', { reload: true });
  await sleep(2500);
  const afterRefresh = await h.eval(`(() => { const t=document.body.innerText||''; return JSON.stringify({ stillReal: /已接入真实账户/.test(t), hash: location.hash }); })()`);
  log('4_afterRefresh', afterRefresh);
  await h.screenshot(SHOTS + '/A04-after-refresh.png');

  // 5) 深链直达 #/cards
  await h.goto(BASE + '/chat#/cards', { reload: true });
  await sleep(2500);
  const deep = await h.eval(`(() => { const t=document.body.innerText||''; return JSON.stringify({ hash: location.hash, showsCards: /卡片|卡号|储蓄|信用/.test(t), stillReal: /已接入真实账户/.test(t) }); })()`);
  log('5_deepLinkCards', deep);
  await h.screenshot(SHOTS + '/A05-deeplink-cards.png');

  // 6) 退出登录（顶栏用户菜单：触发按钮含「已接入真实账户」）
  const trig = await h.clickByText('已接入真实账户', { tag: 'button', visibleOnly: true });
  log('6_openMenuTrigger', trig);
  await sleep(900);
  const menuVisible = (await h.bodyText()).includes('个人信息') && (await h.bodyText()).includes('退出登录');
  log('6_menuVisible', menuVisible);
  await h.screenshot(SHOTS + '/A06a-user-menu-open.png');
  const out = await h.clickByText('退出登录', { tag: 'button', exact: true, visibleOnly: true });
  log('6_clickLogout', out);
  await sleep(2200);
  const afterLogout = await h.eval(`(() => { const t=document.body.innerText||''; return JSON.stringify({ url: location.href, backToGate: /请输入用户名|请输入密码/.test(t), stillReal: /已接入真实账户/.test(t), token: !!localStorage.getItem('xb_token') }); })()`);
  log('6_afterLogout', afterLogout);
  await h.screenshot(SHOTS + '/A06-after-logout.png');

  // 7) 未登录访问 /admin.html
  await h.goto(BASE + '/admin.html', { reload: true });
  await sleep(2200);
  const adminNoAuth = await h.eval(`(() => { const t=document.body.innerText||''; return JSON.stringify({ url: location.href, hasAdminContent: /用户管理|风控|兑换码|知识库/.test(t), showsGate: /登录|账号|密码/.test(t), body: t.replace(/\\s+/g,' ').slice(0,180) }); })()`);
  log('7_adminNoAuth', adminNoAuth);
  await h.screenshot(SHOTS + '/A07-admin-noauth.png');

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
