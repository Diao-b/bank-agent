/* ★ 真机浏览器实测：用户状态隔离（退出重进开新会话 / 换号零串号）
 * 连 CDP 真浏览器（9222）→ user1 登录 → 开数字柜员 + 语音对话 → 发消息建会话
 *  → 点退出 → 看分桶是否被清 → user2 登录看是否零串号 → 再回 user1 看是否新会话。
 * 用法：node scripts/_e2e_I_isolation.js
 */
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const log = H.makeLogger(path.resolve('_reports/e2e_I_isolation.log'));

const BASE = 'http://127.0.0.1:8178';
let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; log('PASS', name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
  else { fail++; log('FAIL', name + '   ' + JSON.stringify(extra)); }
};

// 分桶键 = xb:u:<xb_user>:<key>（身份键 xb_token/xb_user 保持全局）
const LS_SNAP = `(() => {
  const o = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k === 'xb_token' || k === 'xb_user') { o[k] = localStorage.getItem(k); continue; }
    if (/^xb:u:/.test(k)) o[k] = localStorage.getItem(k);
  }
  return JSON.stringify(o);
})()`;

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  await h.setViewport(1440, 920);

  /* ---------- helper ---------- */
  const snap = async () => JSON.parse(await h.eval(LS_SNAP));

  const doLogin = async (user, pwd) => {
    await h.goto(BASE + '/login');
    await h.eval(`localStorage.removeItem('xb_token')`);
    await h.goto(BASE + '/login');
    await h.type('input[placeholder="请输入用户名"]', user);
    await h.type('input[placeholder="请输入密码"]', pwd);
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3400);
  };

  // 点顶栏用户区那个 button（innerText 含用户名）→ 弹出用户菜单（已开则幂等返回 already-open）
  const openUserMenu = async (user) => {
    const r = await h.eval(`(()=>{
      const bs = () => [...document.querySelectorAll('header button')];
      const hasMenu = () => bs().some(x => /数字柜员/.test(x.innerText||''));
      if (hasMenu()) return 'already-open';
      const b = bs().find(x => (x.innerText||'').includes(${JSON.stringify(user)}));
      if (!b) return 'no-btn';
      b.scrollIntoView({block:'center'}); b.click(); return 'ok';
    })()`);
    await sleep(700);
    return r;
  };
  const clickMenuItem = async (txt) => h.eval(`(()=>{
      const b = [...document.querySelectorAll('header button')].find(x => (x.innerText||'').includes(${JSON.stringify(txt)}));
      if (!b) return 'no-btn'; b.click(); return 'ok';
    })()`);
  // 菜单里的开关文案（不点开也能读：打开菜单后立刻读，读完再关）
  const menuLabels = () => h.eval(`(()=>{
      const bs = [...document.querySelectorAll('header button')].map(b => (b.innerText||'').trim()).filter(Boolean);
      return JSON.stringify(bs);
    })()`);
  // 关菜单：UserMenu 用 document mousedown 外部关闭
  const closeMenu = () => h.eval(`(()=>{document.body.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));return 1})()`);
  const dhLabel = () => h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>/数字柜员/.test(b.innerText||''));return b?b.innerText.trim():null})()`);
  const clearComposer = () => h.eval(`(()=>{const t=document.querySelector('textarea');if(t){t.value='';t.dispatchEvent(new Event('input',{bubbles:true}));}return 1})()`);

  try {
    /* ============ A user1：开功能 + 建会话 ============ */
    await h.goto(BASE + '/login');
    // 每轮从干净起点：清掉全部 localStorage（否则历史残留会让 C2/D2 假红）
    await h.eval(`localStorage.clear()`);
    await h.goto(BASE + '/login');
    await doLogin('user1', '123456');
    const body1 = await h.bodyText();
    ok('A1 user1 登录进入主页', /张小明|小微|账户/.test(body1 || ''), await h.eval('location.href'));

    const m1 = await openUserMenu('user1');
    ok('A2 顶栏用户区能点开菜单', m1 === 'ok', { m1 });
    const pre1 = await menuLabels();
    ok('A2b 菜单里能看到数字柜员开关（默认「开」=未开）',
      /数字柜员\s*开/.test(pre1 || ''), { labels: pre1 });
    const k1 = await clickMenuItem('数字柜员');
    await sleep(700);
    const m1b = await openUserMenu('user1');
    const lbl1 = await menuLabels();
    ok('A3 ★点开数字柜员 → 文案切到「关」(即当前已开)', /数字柜员\s*关/.test(lbl1 || ''), { k1, m1b, labels: lbl1 });

    await openUserMenu('user1');
    const k2 = await clickMenuItem('与小微对话');
    await sleep(900);
    const m1c = await openUserMenu('user1');
    const lbl2 = await menuLabels();
    ok('A4 开语音对话后数字柜员自动开', /数字柜员\s*关/.test(lbl2 || ''), { k2, m1c, labels: lbl2 });
    await closeMenu();

    // 发一条消息建会话
    await h.eval(`location.hash = '#/chat'`);
    await sleep(2400);
    await h.type('textarea', '查一下我的余额');
    await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(b=>/发送|提交|发送/.test(b.innerText||''));if(b){b.click();return 'ok';}return 'nosend'})()`);
    await sleep(4200);
    await clearComposer();

    const s1 = await snap();
    const th1 = s1['xb:u:user1:thread'] || null;
    ok('A5 功能态写进 user1 的分桶（不是全局键）', !!s1['xb:u:user1:digital_human'], s1);
    ok('A6 user1 的 thread 已建立', !!th1, { thread: th1 });
    const glob = Object.keys(s1).filter((k) => !/^xb:u:|^xb_token$|^xb_user$/.test(k));
    ok('A7 localStorage 无全局功能态键残留', glob.length === 0, { leaked: glob });
    await h.screenshot('_reports/shot_iso_u1.png');

    /* ============ B 退出登录 → 分桶清空 ============ */
    await openUserMenu('user1');
    const o1 = await clickMenuItem('退出登录');
    await sleep(2600);
    const urlB = await h.eval('location.href');
    ok('B1 ★点退出登录跳回登录页', /login/.test(urlB), { o1, url: urlB });
    const sB = await snap();
    const leftB = Object.keys(sB).filter((k) => /^xb:u:/.test(k));
    ok('B2 ★退出后本人分桶被清空', leftB.length === 0, { o: sB });
    ok('B3 退出后 token 被清', (await h.eval(`localStorage.getItem('xb_token')`)) === null);
    await h.screenshot('_reports/shot_iso_logout.png');

    /* ============ C 换 user2 → 零串号 ============ */
    await doLogin('user2', '123456');
    const body2 = await h.bodyText();
    ok('C1 user2 登录成功', /账户|小微|账单/.test(body2 || ''), await h.eval('location.href'));
    const s2 = await snap();
    const dh2 = s2['xb:u:user2:digital_human'] || null;
    const th2 = s2['xb:u:user2:thread'] || null;
    ok('C2 ★换号后看不到 user1 的数字柜员开关', dh2 === null, { dh2 });
    ok('C3 ★换号后拿到全新会话（不是 user1 那条）', !!th2 && th2 !== th1, { th1, th2 });
    const m2 = await openUserMenu('user2');
    const lbl2b = await menuLabels();
    ok('C4 user2 菜单里数字柜员是默认关态（文案「开」）', /数字柜员\s*开/.test(lbl2b || ''), { m2, labels: lbl2b });
    ok('C5 user2 菜单里没有沿用 user1 的语音对话态', !/与小微对话\s*关/.test(lbl2b || ''), { labels: lbl2b });
    await closeMenu();
    await h.screenshot('_reports/shot_iso_u2.png');

    /* ============ D 退出 → 重进 user1 → 新会话 ============ */
    await openUserMenu('user2');
    await clickMenuItem('退出登录');
    await sleep(2400);
    await doLogin('user1', '123456');
    await h.eval(`location.hash = '#/chat'`);
    await sleep(2800);
    const s3 = await snap();
    const th3 = s3['xb:u:user1:thread'] || null;
    ok('D1 ★退出重进：user1 拿到全新会话（不是上一轮那条）', !!th3 && th3 !== th1, { old: th1, now: th3 });
    ok('D2 ★退出重进：数字柜员回到默认（关）', (s3['xb:u:user1:digital_human'] || null) === null, s3);
    // 只看「当前会话的消息区」（主聊天区），不看左侧历史会话列表 —— 历史会话本来就该留着
    const hits = JSON.parse(await h.eval(`(()=>{
      const out = [];
      // 只看「当前会话的消息流」：带「小微在线」头部的那个面板（排除左侧会话列表面板）
      const panels = [...document.querySelectorAll('div.flex-1.min-w-0, div.flex-1.overflow-y-auto')]
        .filter(d => /小微在线/.test(d.innerText || ''));
      panels.forEach(d => {
        const t = (d.innerText || '').trim();
        if (t.includes('查一下我的余额')) out.push((d.className||'').slice(0,50) + ' | ' + t.slice(0,60));
      });
      return JSON.stringify(out.slice(0, 4));
    })()`));
    ok('D3 ★当前会话是全新会话（旧消息没跟进来）', hits.length === 0, { hits });
    const listed = /查一下我的余额/.test(await h.bodyText());
    log('INFO', 'D4 上一轮会话仍留在历史列表（历史不该被误删）: ' + listed);
    await h.screenshot('_reports/shot_iso_u1_reenter.png');
  } catch (e) {
    fail++;
    log('EXCEPTION', String((e && e.message) || e));
  } finally {
    await h.close().catch(() => {});
  }
  log('RESULT', `PASS=${pass} FAIL=${fail}`);
  process.exit(fail === 0 ? 0 : 1);
})();
