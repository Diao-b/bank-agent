/* 探索：打开 SPA，读懂登录界面 + 模式指示 */
const { launch, BASE, sleep } = require('./_e2e_lib');
const TOK = process.argv[2] || '';

(async () => {
  const h = await launch();
  // 1) 直接打开 SPA（未登录）
  await h.goto(BASE + '/chat', { reload: false });
  await sleep(1500);
  const loginInfo = await h.eval(`(() => {
    const inputs = [...document.querySelectorAll('input,textarea')].map(e => ({tag:e.tagName, type:e.type, ph:e.placeholder, name:e.name, id:e.id}));
    const btns = [...document.querySelectorAll('button')].map(b => (b.innerText||'').trim()).filter(Boolean);
    const h1 = document.querySelector('h1,h2') ? (document.querySelector('h1,h2').innerText||'') : '';
    return JSON.stringify({ url: location.href, title: document.title, inputs, btns: btns.slice(0,20), h1, bodyHead: (document.body.innerText||'').replace(/\\s+/g,' ').slice(0,300) });
  })()`);
  console.log('LOGIN_PAGE:', loginInfo);
  console.log('MODE_TEXT(raw):', JSON.stringify(await h.bodyText()));

  // 2) 注入 token 后真刷新
  if (TOK) {
    await h.setToken(TOK);
    await h.goto(BASE + '/chat', { reload: true });
    await sleep(2500);
    const mode = await h.eval(`(() => {
      const t = document.body.innerText || '';
      const real = /已接入真实账户/.test(t);
      const demo = /演示模式|离线演示/.test(t);
      return JSON.stringify({ real, demo, hasModeIndicator: real||demo,
        modeSnippet: t.replace(/\\s+/g,' ').match(/.{0,30}(已接入真实账户|演示模式|离线演示).{0,40}/)?.[0] || null });
    })()`);
    console.log('AFTER_TOKEN_MODE:', mode);
    await h.screenshot('D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e/00-chat-after-login.png');
    console.log('CHAT_BODY_HEAD:', JSON.stringify((await h.bodyText()).replace(/\\s+/g,' ').slice(0,500)));
  }
  await h.close();
})().catch(e => { console.error('FATAL', e); process.exit(1); });
