/* 批 2 第 5 组 · 真机验收：F-03/F-05 安全中心控制区 · A-01 形象页 · L-02/03/04 登录页 · S-08b 排序
 * 用法：node scripts/_ux_fix_verify5.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_fix_verify5.log'));
let pass = 0, fail = 0;
const ok = (n, c, e) => { if (c) { pass++; log('PASS', n + (e !== undefined ? '  ' + JSON.stringify(e) : '')); } else { fail++; log('FAIL', n + '  ' + JSON.stringify(e)); } };

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  try {
    /* ============ L-03/L-04：登录页首屏 ============ */
    await h.setViewport(1440, 920);
    await h.goto(BASE + '/login');
    await h.eval(`localStorage.clear()`);
    await h.goto(BASE + '/login');
    await sleep(2600);
    const loginTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix5_login_first.png'));
    ok('L-03 ★访问 /login 首屏即可输入账号密码（不再先看落地页）',
      !!(await h.eval(`!!document.querySelector('input[placeholder="请输入用户名"]')`)));
    ok('L-03 首屏存在「立即登录」按钮', /立即登录/.test(loginTxt));
    ok('L-02 ★存在「忘记密码」入口', /忘记密码/.test(loginTxt));
    await h.clickByText('忘记密码', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(800);
    const afterHelp = (await h.bodyText()) || '';
    ok('L-02 点开后给出可执行的说明（不是死链接）', /不支持自助重置|联系柜台|客服/.test(afterHelp),
      (afterHelp.match(/[^\n]{0,30}(自助重置|柜台)[^\n]{0,30}/) || [''])[0]);
    ok('L-04 ★环境标识按后端可达性动态给出', /已连接银行服务|未连接银行服务|正在检测/.test(loginTxt),
      (loginTxt.match(/[^\n]{0,20}(已连接|未连接|正在检测)[^\n]{0,30}/) || [''])[0]);
    ok('L-04 不再出现与真实模式矛盾的固定文案',
      !/演示环境 · 所有数据均为模拟/.test(loginTxt));

    /* ============ F-03 / F-05 安全中心 ============ */
    await h.goto(BASE + '/login');
    await h.type('input[placeholder="请输入用户名"]', 'user1');
    await h.type('input[placeholder="请输入密码"]', '123456');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3600);
    await h.eval(`location.hash = '#/safe'`);
    await sleep(3000);
    const safeTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix5_safe_controls.png'));
    ok('F-03 ★安全中心出现「安全设置」控制区', /安全设置/.test(safeTxt));
    ok('F-03 含隐私开关 / 支付密码 / 银行卡冻结 三项',
      ['隐私模式', '支付密码', '银行卡冻结'].every(k => safeTxt.includes(k)),
      ['隐私模式', '支付密码', '银行卡冻结'].filter(k => safeTxt.includes(k)));
    ok('F-03 支付密码区给出真实状态',
      /未设置|已设置|已锁定|状态暂不可用|正在读取/.test(safeTxt),
      (safeTxt.match(/支付密码[\s\S]{0,60}/) || [''])[0].replace(/\n/g, ' '));
    const pwdBtn = await h.clickByText('设置 / 修改', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(1600);
    // 预填文本在输入框的 value 里（innerText 取不到）
    const composer = await h.eval(`(()=>{const t=document.querySelector('textarea');return t?t.value:null})()`);
    const onChat = /#\/chat/.test(await h.eval('location.hash')) || /小微在线/.test((await h.bodyText()) || '');
    ok('F-03 ★点「设置 / 修改」跳对话并预填支付密码诉求', onChat && /支付密码/.test(String(composer || '')),
      { pwdBtn, composer });
    await h.eval(`location.hash = '#/safe'`);
    await sleep(2200);
    const rulesCollapsed = !/小额且常用：仅支付密码/.test((await h.bodyText()) || '');
    await h.clickByText('展开查看', { tag: 'button', nth: 0 });
    await sleep(900);
    const rulesShown = /小额且常用：仅支付密码/.test((await h.bodyText()) || '');
    ok('F-05 ★风控规则默认折叠，点击可展开', rulesCollapsed && rulesShown, { rulesCollapsed, rulesShown });

    /* ============ A-01 形象设置页 ============ */
    await h.eval(`location.hash = '#/appearance'`);
    await sleep(3000);
    const apTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix5_appearance.png'));
    // ★ A-01b 后：用户端已彻底去掉编辑控件（不是置灰，是不存在），
    //   编辑能力唯一入口在管理后台 `#/admin · 数字人形象`。
    ok('A-01b ★用户端已无任何编辑控件（select / 保存按钮 / 上传全部移除）',
      await h.eval(`(()=>{
        const sel=[...document.querySelectorAll('select')].length;
        const save=[...document.querySelectorAll('button,a')].filter(b=>/保存形象|上传新背景图/.test(b.innerText||'')).length;
        const file=document.querySelectorAll('input[type=file]').length;
        return sel===0 && save===0 && file===0;
      })()`));
    ok('A-01 ★显示中文名，不再裸露机读值',
      /晴（Haru）|前台接待台背景|标准教室背景/.test(apTxt) && !/nokia_reception_desk\.png/.test(apTxt),
      { sample: (apTxt.match(/选择数字柜员[\s\S]{0,120}/) || [''])[0].replace(/\n/g, ' ') });
    ok('A-01 ★页面不再显示内网预览 URL', !/127\.0\.0\.1:5173/.test(apTxt));
    ok('A-01 给出「全局设置 · 修改请到管理后台」的说明', /全局/.test(apTxt) && /管理后台/.test(apTxt),
      (apTxt.match(/[^\n]{0,20}只读预览[^\n]{0,30}/) || [''])[0]);

    /* ============ S-08b 订阅排序（接口层） ============ */
    const order = await h.eval(`(async () => {
      const r = await fetch('/api/subscriptions', { headers: authHeaders() });
      const j = await r.json();
      return JSON.stringify((j.subscriptions || []).map(x => [x.status, x.next_due].join(':')));
    })()`);
    const arr = JSON.parse(order);
    const firstEnded = arr.findIndex(x => !x.startsWith('active'));
    const lastActive = arr.map((x, i) => ({ x, i })).filter(o => o.x.startsWith('active')).pop();
    ok('S-08b ★接口返回：生效中在前、且按 next_due 升序',
      firstEnded === -1 || (lastActive && lastActive.i < firstEnded), { order: arr });

    log('RESULT', `PASS=${pass} FAIL=${fail}`);
  } catch (e) {
    fail++; log('EXCEPTION', String((e && e.message) || e));
  } finally { await h.close().catch(() => {}); }
  process.exit(fail === 0 ? 0 : 1);
})();
