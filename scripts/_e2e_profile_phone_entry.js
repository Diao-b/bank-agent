/* 真机回归：个人信息弹窗的「手机号」入口必须随绑定状态变文案
 *
 *  用户报："手机号绑定没有地方可以绑定的按钮啊"
 *  根因：手机那一行只有一行灰字「未绑定」；唯一入口是页脚的文字链，
 *        而它在**未绑定**时也叫「修改手机号」→ 用户以为没有可绑的入口。
 *  修法：两处都随状态走 —— 未绑定 → 页脚「绑定手机号」+ 行内「去绑定」；
 *        已绑定 → 页脚「修改手机号」+ 行内「更换」。
 *
 *  用法：node scripts/_e2e_profile_phone_entry.js <用户名> <bound|unbound> [截图名]
 */
const H = require('./_e2e_lib.js');

const USER = process.argv[2] || 'user1';
const EXPECT = (process.argv[3] || 'bound') === 'bound' ? 'bound' : 'unbound';
const SHOT = process.argv[4] || ('_reports/e2e_profile_phone_' + EXPECT + '.png');

let ok = 0, fail = 0;
const check = (name, cond, extra) => {
  if (cond) { ok++; console.log('PASS ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
  else { fail++; console.log('FAIL ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
};

(async () => {
  const h = await H.launch({ width: 1500, height: 1000, watchdogMs: 120000 });
  try {
    await h.goto(H.BASE + '/');
    await h.eval(`localStorage.clear(); localStorage.setItem('xb_user', ${JSON.stringify(USER)})`);
    await h.setToken(process.env.XB_TOKEN || '');
    await h.goto(H.BASE + '/chat', { reload: true });

    // /profile 路由会重定向到对话页并弹出「个人信息」弹窗（与「去绑定手机」引导同一路径）
    await h.eval("location.hash = '#/profile'");
    const opened = await h.waitForText('个人信息', 15000);
    await H.sleep(1200);

    // ★ 模板字符串里不写任何反斜杠（`\s` 会被吃成 `s`、`\n` 会变真换行），
    //   原始文本拿回 Node 再做匹配
    const raw = await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const dlg = [...document.querySelectorAll('[data-modal-scrim]')].filter(vis).pop();
      return dlg ? dlg.innerText : '';
    })()`);
    const flat = String(raw).replace(/\s+/g, ' ');
    console.log('MODAL_TEXT ' + JSON.stringify(flat.slice(0, 260)));

    check('A1 个人信息弹窗已打开', opened === true && /个人信息/.test(flat), opened);
    check('A2 手机行存在', /手机/.test(flat) && (EXPECT === 'bound' ? /1\d{2}\*{4}\d{4}/.test(flat) : /未绑定/.test(flat)),
          flat.slice(0, 90));

    if (EXPECT === 'unbound') {
      check('★B1 页脚显示「绑定手机号」（不是"修改手机号"）', /绑定手机号/.test(flat));
      check('★B2 手机行有「去绑定」入口', /去绑定/.test(flat));
      check('B3 未绑定时不出现"修改手机号"', !/修改手机号/.test(flat));
    } else {
      check('★B1 页脚显示「修改手机号」', /修改手机号/.test(flat));
      check('★B2 手机行有「更换」入口', /更换/.test(flat));
      check('B3 已绑定时不出现"绑定手机号"', !/绑定手机号/.test(flat));
    }

    // 点页脚那条链 → 应切到绑定/更换表单（能看到手机号输入 + 发送验证码）
    await h.screenshot(SHOT.replace('.png', '_main.png'));   // ★ 点击前的「资料主视图」
    await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const dlg = [...document.querySelectorAll('[data-modal-scrim]')].filter(vis).pop();
      if (!dlg) return;
      const b = [...dlg.querySelectorAll('button')].filter(vis)
        .find(x => ${EXPECT === 'unbound' ? '/绑定手机号/' : '/修改手机号/'}.test(x.innerText || ''));
      if (b) b.click();
    })()`);
    await H.sleep(1400);
    const form = await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const dlg = [...document.querySelectorAll('[data-modal-scrim]')].filter(vis).pop();
      return dlg ? dlg.innerText : '';
    })()`);
    const fflat = String(form).replace(/\s+/g, ' ');
    check('★C1 点入口能进到绑定/更换表单', /发送验证码/.test(fflat),
          fflat.slice(0, 120));

    await h.screenshot(SHOT);   // 点击后的「绑定 / 更换表单」
    console.log('\n' + ok + ' passed, ' + fail + ' failed');
  } catch (e) {
    console.log('ERR ' + (e && e.message || e));
    fail++;
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(fail ? 1 : 0);
  }
})();
