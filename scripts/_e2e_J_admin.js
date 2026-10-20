/* J：管理端 /admin.html —— 各 Tab 渲染 / 知识库重建索引 / 风控回显 / 越权 */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/J_admin.log');

async function fill(h, u, p) {
  return await h.eval(`(() => {
    const ins=[...document.querySelectorAll('input')];
    const U=ins.find(e=>e.type==='text'||e.type==='email'||/账号|用户/.test(e.placeholder||''));
    const P=ins.find(e=>e.type==='password');
    const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    if(U){s.call(U, ${JSON.stringify(u)}); U.dispatchEvent(new Event('input',{bubbles:true}));}
    if(P){s.call(P, ${JSON.stringify(p)}); P.dispatchEvent(new Event('input',{bubbles:true}));}
    return JSON.stringify({U:!!U,P:!!P});
  })()`);
}
async function clickLogin(h) {
  for (const t of ['进入运营后台','登录','立即登录','进入','登 录']) {
    const r = await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').replace(/\\s/g,'')===${JSON.stringify(t.replace(/\s/g,''))}); if(b){b.click(); return true;} return false; })()`);
    if (r) return t;
  }
  return null;
}
async function pageFacts(h) {
  return await h.eval(`(() => {
    const t=document.body.innerText||'';
    const tables=[...document.querySelectorAll('table')].map(tb=>({rows: tb.querySelectorAll('tr').length, cols: (tb.querySelector('tr')||{children:[]}).children.length}));
    const tabs=[...document.querySelectorAll('button,[role=tab],a')].map(e=>(e.innerText||'').replace(/\\s+/g,' ').trim()).filter(x=>x&&x.length<14);
    return JSON.stringify({ url:location.href, hasTabs:/用户管理|兑换码|公告|风控|知识库/.test(t), tabs:[...new Set(tabs)].slice(0,30), tables, head:t.replace(/\\s+/g,' ').slice(0,300) });
  })()`);
}

(async () => {
  const h = await launch({ watchdogMs: 240000 });
  // 1) 越权：普通用户 token 访问 /admin.html
  const userTok = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(userTok);
  await h.goto(BASE + '/admin.html', { reload: false }); await sleep(2500);
  log('normalUser_admin', await pageFacts(h));
  await h.screenshot(SHOTS + '/J01-usertoken-admin.png');

  // 2) admin 登录（先清普通用户 token，否则 /admin_login 会重定向到 /admin 403）
  await h.clearToken();
  await h.goto(BASE + '/admin_login.html', { reload: false }); await sleep(1500);
  log('admin_login_page', await h.eval(`(() => ({ url:location.href, inputs:[...document.querySelectorAll('input')].map(e=>({type:e.type,ph:e.placeholder})), btns:[...document.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(Boolean).slice(0,8) }))()`).then(JSON.stringify));
  await fill(h, 'admin', 'admin123');
  const t = await clickLogin(h);
  log('admin_login_click', t);
  await sleep(3000);
  log('admin_after_login', await pageFacts(h));
  await h.screenshot(SHOTS + '/J02-admin-home.png');

  // 遍历各 Tab
  const tabNames = await h.eval(`(() => {
    const t=[...document.querySelectorAll('button,a,[role=tab],li')].map(e=>(e.innerText||'').replace(/\\s+/g,' ').trim()).filter(x=>x && x.length<=8 && /用户|兑换|公告|风控|知识|索引|参数|审计|概览|总览|数据|理财|验证码|形象/.test(x) && !/返回|退出|端/.test(x));
    return JSON.stringify([...new Set(t)].slice(0,12));
  })()`);
  const tabs = JSON.parse(tabNames);
  log('tabs_found', tabs);
  for (const tb of tabs) {
    const clicked = await h.eval(`(() => { const e=[...document.querySelectorAll('button,a,[role=tab],li')].find(x=>(x.innerText||'').replace(/\\s+/g,' ').trim()===${JSON.stringify(tb)} && x.offsetParent!==null); if(e){e.click(); return true;} return false; })()`);
    if (!clicked) continue;
    await sleep(1800);
    const facts = await h.eval(`(() => { const t=document.body.innerText||'';
      const tables=[...document.querySelectorAll('table')].map(tb=>({rows:tb.querySelectorAll('tr').length}));
      return JSON.stringify({ tab:${JSON.stringify(tb)}, tables, hasRebuild:/重建索引/.test(t), hasSync:/同步|待同步/.test(t), body:t.replace(/\\s+/g,' ').slice(0,220) }); })()`);
    log('tab:' + tb, facts);
    await h.screenshot(SHOTS + '/J-tab-' + tb.replace(/[^\w\u4e00-\u9fa5]/g,'') + '.png');
    // 知识库：点重建索引
    if (JSON.parse(facts).hasRebuild) {
      const rb = await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x=>/重建索引/.test(x.innerText||'')); if(b){b.click(); return true;} return false; })()`);
      log('rebuild_clicked', rb);
      await sleep(4000);
      log('rebuild_result', await h.eval(`(() => { const t=document.body.innerText||''; return JSON.stringify({ m:(t.match(/[^\\n]{0,30}(索引|文档|片段|成功|失败|完成|篇|条)[^\\n]{0,30}/g)||[]).slice(0,6) }); })()`));
      await h.screenshot(SHOTS + '/J-rebuild.png');
    }
  }
  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
