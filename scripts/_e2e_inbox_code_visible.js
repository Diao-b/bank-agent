/* 真机复现/回归：绑定手机号时打开收件箱，验证码必须**完整可见**
 *
 *  用户报："这个绑定这个有验证码打开这个邮箱看不清啊"
 *  两个原因叠加：
 *    ① 收件箱抽屉 z-50 在弹窗（z-85/86/88/90）**下面** → 整个抽屉被弹窗遮罩压住，
 *       糊了一层暗色（实测 GEO：drawerZ=50 / modalZ=85 / overlap=360px 全遮）；
 *    ② 验证码只在**展开**后才用 32px 金色显示，折叠态是一行 12px 灰字里夹着数字。
 *  修法：抽屉 z → z-[95]（高于所有弹窗）；验证码只要未过期就一直大字显示（去掉 isOpen 条件）。
 *
 *  ★ 副作用说明：本脚本会**真的点一次「发送验证码」**（会在收件箱留一条验证码通知 +
 *    一条 verification_codes 记录，并写入 users.phone_pending）。跑完后请清理，例如：
 *      DELETE FROM notifications WHERE user_id=<uid> AND type='verify_code';
 *      DELETE FROM verification_codes WHERE user_id=<uid>;
 *      UPDATE users SET phone_pending=NULL WHERE id=<uid> AND phone IS NULL;
 *
 *  用法：node scripts/_e2e_inbox_code_visible.js <用户名> [截图前缀]
 */
const H = require('./_e2e_lib.js');

const USER = process.argv[2] || 'user1';
const PREFIX = process.argv[3] || '_reports/e2e_inbox_code';

let ok = 0, fail = 0;
const check = (name, cond, extra) => {
  if (cond) { ok++; console.log('PASS ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
  else { fail++; console.log('FAIL ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
};

(async () => {
  const h = await H.launch({ width: 1080, height: 700, watchdogMs: 150000 });
  try {
    await h.goto(H.BASE + '/');
    await h.eval(`localStorage.clear(); localStorage.setItem('xb_user', ${JSON.stringify(USER)})`);
    await h.setToken(process.env.XB_TOKEN || '');
    await h.goto(H.BASE + '/chat', { reload: true });

    // 1) 打开个人信息 → 绑定手机号
    await h.eval("location.hash = '#/profile'");
    await h.waitForText('个人信息', 15000);
    await H.sleep(1000);
    await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const dlg = [...document.querySelectorAll('[data-modal-scrim]')].filter(vis).pop();
      const b = [...dlg.querySelectorAll('button')].filter(vis).find(x => /绑定手机号|修改手机号/.test(x.innerText || ''));
      if (b) b.click();
    })()`);
    await H.sleep(1200);

    // 2) 填手机号 + 发验证码（真发码 → 收件箱里会多一条）
    await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const dlg = [...document.querySelectorAll('[data-modal-scrim]')].filter(vis).pop();
      const inp = dlg.querySelector('input[inputmode="numeric"]');
      if (inp) {
        const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        s.call(inp, '13800008888');
        inp.dispatchEvent(new Event('input', { bubbles: true }));
      }
      const b = [...dlg.querySelectorAll('button')].filter(vis).find(x => /发送验证码/.test(x.innerText || ''));
      if (b) b.click();
    })()`);
    await H.sleep(2500);

    // 3) 点「打开收件箱」
    await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const b = [...document.querySelectorAll('button')].filter(vis).find(x => /打开收件箱/.test(x.innerText || ''));
      if (b) b.click();
    })()`);
    await H.sleep(1800);

    await h.screenshot(PREFIX + '_before_expand.png');

    // ---- 断言 1：抽屉必须**在弹窗之上**（不被压住）----
    const geo = await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const drawer = [...document.querySelectorAll('aside')].filter(vis).pop();
      const scrims = [...document.querySelectorAll('[data-modal-scrim]')].filter(vis);
      const modal = scrims[scrims.length - 1];
      if (!drawer) return null;
      const dr = drawer.getBoundingClientRect();
      const mr = modal ? modal.getBoundingClientRect() : null;
      const z = el => { let n = el, best = 0; while (n && n !== document.body) { const v = parseInt(getComputedStyle(n).zIndex, 10); if (!isNaN(v)) best = Math.max(best, v); n = n.parentElement; } return best; };
      // 抽屉左边缘是否被弹窗覆盖
      const coveredLeft = mr ? Math.max(0, Math.min(mr.right, dr.right) - Math.max(mr.left, dr.left)) : 0;
      return { drawerX: Math.round(dr.x), drawerW: Math.round(dr.width), modalRight: mr ? Math.round(mr.right) : null,
               overlap: Math.round(coveredLeft), drawerZ: z(drawer), modalZ: modal ? z(modal) : null };
    })()`);
    console.log('GEO ' + JSON.stringify(geo));
    check('A1 收件箱抽屉已打开', !!geo, geo);
    check('★A2 抽屉层级在弹窗之上（zIndex 更大）',
          !!geo && geo.drawerZ > (geo.modalZ || 0), geo);
    check('★A3 抽屉整体可见（左边缘没被弹窗压在下面）',
          !!geo && geo.drawerX >= 0 && (geo.drawerZ > (geo.modalZ || 0) || geo.overlap === 0), geo);

    // ---- 断言 2：验证码必须**不展开也能看到**，且够大够亮 ----
    const code = await h.eval(`(() => {
      const vis = e => !!(e.offsetParent || e.getClientRects().length);
      const els = [...document.querySelectorAll('div,span')].filter(vis)
        .filter(e => /^\\d{6}$/.test((e.textContent || '').trim()));
      if (!els.length) return null;
      const el = els[els.length - 1];
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return { code: el.textContent.trim(), fontSize: parseFloat(cs.fontSize), color: cs.color,
               visible: r.width > 0 && r.height > 0, x: Math.round(r.x) };
    })()`);
    console.log('CODE ' + JSON.stringify(code));
    check('★B1 收件箱里能看到 6 位验证码（无需额外展开）', !!code && /^\d{6}$/.test(code.code), code);
    check('★B2 验证码字号足够大（≥ 20px）', !!code && code.fontSize >= 20, code && code.fontSize);
    check('★B3 验证码在抽屉内可见区域（没被压到弹窗底下）',
          !!code && code.visible && (!geo || geo.drawerZ > (geo.modalZ || 0) || code.x >= (geo.modalRight || 0)), code);

    await h.screenshot(PREFIX + '_after.png');
    console.log('\n' + ok + ' passed, ' + fail + ' failed');
  } catch (e) {
    console.log('ERR ' + (e && e.message || e));
    fail++;
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(fail ? 1 : 0);
  }
})();
