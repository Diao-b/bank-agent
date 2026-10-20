/* L-01 验收：后端不可达时登录必须失败且不签发任何本地 token
 * 做法：CDP 拦截 /api/auth/* → 提交登录 → 断言无 xb_token、有「服务…不可用」提示、仍在登录页
 * 用法：node scripts/_ux_login_offline_verify.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_login_offline.log'));
let pass = 0, fail = 0;
const ok = (n, c, e) => { if (c) { pass++; log('PASS', n + (e !== undefined ? '  ' + JSON.stringify(e) : '')); } else { fail++; log('FAIL', n + '  ' + JSON.stringify(e)); } };

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  try {
    await h.goto(BASE + '/login');
    await h.eval(`localStorage.clear()`);
    await h.setViewport(1440, 920);
    await h.goto(BASE + '/login');
    await sleep(1500);
    // 拦掉认证接口
    await h.b.send('Network.setBlockedURLs', { urls: ['*/api/auth/*'] }, h.sid);
    await h.type('input[placeholder="请输入用户名"]', 'user1');
    await h.type('input[placeholder="请输入密码"]', '123456');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(4500);
    const token = await h.eval(`localStorage.getItem('xb_token')`);
    const user = await h.eval(`localStorage.getItem('xb_user')`);
    const href = await h.eval(`location.href`);
    const txt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix_login_offline.png'));
    ok('L-01 ★后端不可达时不签发 xb_token', token === null, { token });
    ok('L-01 ★后端不可达时不写入 xb_user', user === null, { user });
    ok('L-01 ★仍停留在登录页（未进入 /chat）', /login/.test(href), { href });
    ok('L-01 提示为「服务…不可用」而非「密码错误」', /不可用|稍后重试/.test(txt) && !/密码错误/.test(txt), { hit: (txt.match(/[^\n]{0,30}(不可用|密码错误)[^\n]{0,20}/) || [])[0] });

    // 恢复网络 → 正常登录仍可用
    await h.b.send('Network.setBlockedURLs', { urls: [] }, h.sid);
    await h.goto(BASE + '/login');
    await h.type('input[placeholder="请输入用户名"]', 'user1');
    await h.type('input[placeholder="请输入密码"]', '123456');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3800);
    const token2 = await h.eval(`localStorage.getItem('xb_token')`);
    ok('L-01 恢复后正常登录仍可用', !!token2, { hasToken: !!token2 });
    log('RESULT', `PASS=${pass} FAIL=${fail}`);
  } catch (e) {
    fail++; log('EXCEPTION', String((e && e.message) || e));
  } finally { await h.close().catch(() => {}); }
  process.exit(fail === 0 ? 0 : 1);
})();
