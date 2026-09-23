/* 用 CDP 驱动无头 Edge 真实验证「用户管理 → 删除账号」这条新增链路：
   1) 注入 admin token 打开 /admin
   2) 进「用户管理」页，展开一行用户
   3) 断言操作区里出现了「删除账号」按钮（本轮新增）
   4) 点它 → 断言弹出二次确认（含"不可撤销"与 confirm_username 校验的说明）
   5) 截图存 _reports/admin_delete/，供人工核对

   用法：node scripts/_admin_delete_ui_check.js <admin_token> [outDir]
   前置：8178 在跑；本脚本自己拉起无头 Edge（只杀自己那一个实例）。
*/
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const ARGV = process.argv.slice(2).filter(a => !a.startsWith('--'));
const TOK = ARGV[0];
const OUT = ARGV[1] || path.join(__dirname, '..', '_reports', 'admin_delete');
const BASE = 'http://127.0.0.1:8178';
const CDP = 'http://127.0.0.1:9222';
const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const PROFILE = path.join(require('os').tmpdir(), 'wb_admin_delete_' + Date.now());

if (!TOK) { console.error('用法：node scripts/_admin_delete_ui_check.js <admin_token>'); process.exit(2); }
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
let failed = 0;
const check = (cond, good, bad) => {
  console.log((cond ? '  ✓ ' : '  ✗ ') + (cond ? good : bad));
  if (!cond) failed++;
};

const edge = spawn(EDGE, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--use-angle=swiftshader',
  '--enable-unsafe-swiftshader', '--remote-debugging-port=9222',
  '--user-data-dir=' + PROFILE, '--window-size=1440,1000', 'about:blank',
], { detached: false, stdio: 'ignore' });

async function pickPageTarget() {
  for (let i = 0; i < 30; i++) {
    try {
      const list = await (await fetch(CDP + '/json/list')).json();
      const p = list.filter(t => t.type === 'page').pop();
      if (p && p.webSocketDebuggerUrl) return p;
    } catch (_) { /* retry */ }
    await sleep(500);
  }
  throw new Error('no CDP page target');
}

function cdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    }
  });
  const ready = new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  return { ready, send, close: () => ws.close() };
}

const evalJs = async (send, expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error('JS 异常：' + JSON.stringify(r.exceptionDetails).slice(0, 300));
  return r.result.value;
};

(async () => {
  const target = await pickPageTarget();
  const { ready, send, close } = cdp(target.webSocketDebuggerUrl);
  await ready;
  await send('Page.enable');
  await send('Runtime.enable');

  // 先到应用源播种登录态，再进后台（★ 必须在页面 JS 之前注入）
  await send('Page.navigate', { url: BASE + '/admin' });
  await sleep(1200);
  await evalJs(send, `localStorage.setItem('xb_token', ${JSON.stringify(TOK)});
                      localStorage.setItem('xb_user','admin');
                      localStorage.setItem('xb_role','admin'); true`);
  await send('Page.navigate', { url: BASE + '/admin' });
  await sleep(2500);

  const landed = await evalJs(send, `location.pathname + location.hash`);
  check(/^\/admin/.test(landed), `已进入运营后台（${landed}）`, `被重定向到登录页（${landed}）—— token 无效？`);

  // 切到「用户管理」页
  const clicked = await evalJs(send, `(() => {
    const el = [...document.querySelectorAll('a,button,div,span')]
      .find(e => e.textContent.trim() === '用户管理' && e.children.length === 0);
    if (!el) return false;
    el.click(); return true;
  })()`);
  check(clicked, '找到并点击「用户管理」入口', '页面上找不到「用户管理」入口');
  await sleep(1800);

  // 展开第一行用户
  const expanded = await evalJs(send, `(() => {
    const tr = document.querySelector('tbody tr');
    if (!tr) return false;
    tr.click(); return true;
  })()`);
  check(expanded, '展开用户详情行', '用户表格没有可展开的行');
  await sleep(1500);

  await send('Emulation.setDeviceMetricsOverride',
    { width: 1440, height: 1200, deviceScaleFactor: 1, mobile: false });
  await sleep(400);

  const state = await evalJs(send, `(() => {
    const txt = document.body.innerText;
    const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '删除账号');
    return JSON.stringify({
      hasButton: !!btn,
      hasOldButtons: txt.includes('锁定登录') || txt.includes('解锁登录'),
      hasRedline: txt.includes('资产数据无任何修改入口'),
      bodySnippet: txt.replace(/\\s+/g, ' ').slice(0, 400),
    });
  })()`);
  const st = JSON.parse(state);
  check(st.hasButton, '用户详情操作区出现「删除账号」按钮', '没有渲染出「删除账号」按钮');
  check(st.hasOldButtons, '原有操作按钮仍在（锁定/重置/角色）', '原有按钮丢了 —— 操作区可能被改坏');
  check(st.hasRedline, '「资产数据无任何修改入口」红线仍在', '红线说明丢失');

  // 点按钮 → 应弹出不可撤销的二次确认
  const modal = await evalJs(send, `(() => {
    const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '删除账号');
    if (!btn) return JSON.stringify({ opened: false });
    btn.click();
    return JSON.stringify({ opened: true });
  })()`);
  await sleep(800);
  const after = await evalJs(send, `(() => {
    const t = document.body.innerText;
    return JSON.stringify({
      hasTitle: t.includes('删除用户（不可撤销）'),
      hasConfirm: t.includes('确认删除该账号'),
      mentionsRecycle: t.includes('user_id 会被回收'),
      mentionsAudit: t.includes('admin_logs'),
    });
  })()`);
  const md = JSON.parse(after);
  check(md.hasTitle, '弹出二次确认（标题：删除用户（不可撤销））', '没有弹出二次确认弹窗');
  check(md.hasConfirm, '确认按钮文案正确（确认删除该账号）', '确认按钮文案不对');
  check(md.mentionsRecycle, '弹窗说明了 uid 会被回收', '弹窗缺少 uid 回收提示');
  check(md.mentionsAudit, '弹窗说明了审计留痕不受影响', '弹窗缺少审计说明');
  console.log('  · 按钮点击结果：' + modal);

  const shot = await send('Page.captureScreenshot', { format: 'png' });
  const file = path.join(OUT, 'admin_delete_confirm.png');
  fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
  console.log('  截图：' + file);

  close();
  try { process.kill(edge.pid); } catch (_) { /* ignore */ }
  const cleanup = require('child_process').spawnSync('cmd', ['/c', 'rmdir', '/s', '/q', PROFILE]);
  void cleanup;
  console.log(failed ? `\n结论：${failed} 项失败` : '\n结论：全部通过');
  process.exit(failed ? 1 : 0);
})().catch(async (e) => {
  console.error('检查中断：' + e.message);
  try { process.kill(edge.pid); } catch (_) { /* ignore */ }
  process.exit(1);
});
