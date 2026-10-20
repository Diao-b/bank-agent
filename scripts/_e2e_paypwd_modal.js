/* 真机回归：安全中心「支付密码 设置 / 修改」必须在本页弹窗内完成，**不能跳对话页**
 *  —— 用户报告：点「设置 / 修改」被跳到对话页（而 agent 明确不支持该诉求 → 白跑一趟）。
 *
 *  前置（由 scripts/test_paypwd_ui_flow.py 准备）：uid 的支付密码哈希被临时清空
 *  → 页面走「首次设置」路径（无需原密码），可完整走完三步而不必知道旧密码。
 *
 *  断言链：
 *    A. 点按钮后 **hash 仍是 #/safe**（没跳对话）★这就是本次要修的 bug
 *    B. 出现 data-modal-scrim 弹窗，含 6 个数字格 + 步骤条
 *    C. 输 6 位 → 进入「第 3/3 步 · 确认新密码」
 *    D. 再输同样 6 位 → 出现「完成」且**有渐变背景/内边距**（弹窗按钮铁律）
 *    E. 点「完成」→ 弹窗关闭，卡片状态变「已设置」
 *
 *  用法：node scripts/_e2e_paypwd_modal.js <token>
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const LOG = '_reports/_e2e_paypwd_modal.log';
const log = (...a) => { try { fs.appendFileSync(LOG, a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {} };
const VISFN = "e=>!!(e.offsetParent||e.getClientRects().length)";
const PWD = '135790';

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1440, height: 900, watchdogMs: 180000 });
  const R = {};
  let ok = 0, fail = 0;
  const check = (name, cond, extra) => {
    if (cond) { ok++; console.log('PASS ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
    else { fail++; console.log('FAIL ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
  };

  /* 往最上层弹窗的 PasswordCard 第 i 个格子输入一位（逐位、带间隔 —— React 状态需重渲染） */
  async function typeDigit(i, ch) {
    return await h.eval(`(() => {
      const vis=${VISFN};
      const scrims=[...document.querySelectorAll('[data-modal-scrim]')].filter(vis);
      const dlg=scrims[scrims.length-1];
      if(!dlg) return 'no-scrim';
      const ins=[...dlg.querySelectorAll('input')].filter(vis)
        .filter(x=>x.maxLength===1||x.getAttribute('inputmode')==='numeric');
      const el=ins[${i}];
      if(!el) return 'no-input-'+${i};
      const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
      s.call(el, ${JSON.stringify(ch)});
      el.dispatchEvent(new Event('input',{bubbles:true}));
      return 'ok';
    })()`);
  }
  async function fillPwd() {
    for (let i = 0; i < PWD.length; i++) { await typeDigit(i, PWD[i]); await H.sleep(130); }
    await H.sleep(700);
  }
  async function modalInfo() {
    return await h.eval(`(() => {
      const vis=${VISFN};
      const scrims=[...document.querySelectorAll('[data-modal-scrim]')].filter(vis);
      const dlg=scrims[scrims.length-1];
      if(!dlg) return null;
      const t=dlg.innerText||'';
      const boxes=[...dlg.querySelectorAll('input')].filter(vis)
        .filter(x=>x.maxLength===1||x.getAttribute('inputmode')==='numeric').length;
      const btns=[...dlg.querySelectorAll('button')].filter(vis).map(b=>({txt:(b.innerText||'').trim(), bg:getComputedStyle(b).backgroundImage, padL:getComputedStyle(b).paddingLeft, bgc:getComputedStyle(b).backgroundColor}));
      return { text: t, boxes, btns };
    })()`);
  }

  try {
    await h.goto(H.BASE + '/');
    await h.eval("localStorage.clear(); localStorage.setItem('xb_user','user1')");
    await h.setToken(process.env.XB_TOKEN || '');
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.eval("location.hash = '#/safe'");
    await H.sleep(2200);
    check('A0 已进入安全中心', String(await h.eval('location.hash')).includes('/safe'));

    /* ---- A. 点「设置 / 修改」 ---- */
    R.clicked = await h.eval(`(() => {
      const vis=${VISFN};
      const btns=[...document.querySelectorAll('button')].filter(vis);
      const b=btns.find(x=>/^(设置|修改|设置 \\/ 修改)$/.test((x.innerText||'').trim()));
      if(!b) return 'no-btn';
      b.click();
      return 'clicked:' + (b.innerText||'').trim();
    })()`);
    log('CLICKED=' + R.clicked);
    await H.sleep(1200);
    const hashAfter = String(await h.eval('location.hash'));
    check('★A1 点按钮后仍在安全中心（没被跳去对话页）',
          hashAfter.includes('/safe'), { hash: hashAfter, clicked: R.clicked });

    /* ---- 锁定态分支（XB_EXPECT_LOCKED=1）：用户被锁时点进来看的是什么 ---- */
    if (process.env.XB_EXPECT_LOCKED === '1') {
      const ml = await modalInfo();
      log('MODAL_LOCKED=' + JSON.stringify(ml));
      check('★L1 锁定态：弹窗明确提示「支付密码已锁定」',
            !!ml && /支付密码已锁定/.test(ml.text),
            ml ? ml.text.replace(/\n/g, ' ').slice(0, 80) : null);
      check('L2 锁定态：说明解锁时间（今日 23:59:59）', !!ml && /23:59:59/.test(ml.text));
      check('L3 锁定态：给得出「关闭」（不是死胡同）',
            !!ml && (ml.btns || []).some(b => /^关闭$/.test(b.txt)),
            ml ? ml.btns.map(b => b.txt) : null);
      await h.screenshot('_reports/e2e_paypwd_locked.png');
      await h.eval(`(() => {
        const vis=${VISFN};
        const scrims=[...document.querySelectorAll('[data-modal-scrim]')].filter(vis);
        const dlg=scrims[scrims.length-1];
        const b=[...dlg.querySelectorAll('button')].filter(vis).find(x=>/^关闭$/.test((x.innerText||'').trim()));
        if(b) b.click();
      })()`);
      await H.sleep(900);
      check('L4 关闭后弹窗消失', !(await modalInfo()));
      console.log('\n' + ok + ' passed, ' + fail + ' failed');
      return;
    }

    /* ---- B. 弹窗与密码卡 ---- */
    const m1 = await modalInfo();
    log('MODAL1=' + JSON.stringify(m1));
    check('★B1 本页弹出了密码弹窗（scrim + 6 格）',
          !!m1 && m1.boxes === 6, m1 ? { boxes: m1.boxes } : null);
    check('B2 显示「设置支付密码」步骤条', !!m1 && /设置支付密码/.test(m1.text),
          m1 ? m1.text.replace(/\n/g, ' ').slice(0, 70) : null);
    check('B3 提示 6 位数字', !!m1 && /6 位数字/.test(m1.text));

    /* ---- C. 输新密码 → 二次确认 ---- */
    await fillPwd();
    const m2 = await modalInfo();
    log('MODAL2=' + JSON.stringify(m2));
    check('★C1 进入第 3/3 步「确认新密码」', !!m2 && /确认新密码/.test(m2.text),
          m2 ? m2.text.replace(/\n/g, ' ').slice(0, 70) : null);
    check('C2 确认步仍是 6 格输入', !!m2 && m2.boxes === 6, m2 ? m2.boxes : null);
    /* ★ 二次确认步必须是**空**的：否则等于把刚输的密码预填出来（泄隐私 + 一致性校验失效） */
    R.confirmVals = await h.eval(`(() => {
      const vis=${VISFN};
      const scrims=[...document.querySelectorAll('[data-modal-scrim]')].filter(vis);
      const dlg=scrims[scrims.length-1];
      if(!dlg) return null;
      return [...dlg.querySelectorAll('input')].filter(vis)
        .filter(x=>x.maxLength===1||x.getAttribute('inputmode')==='numeric')
        .map(x=>x.value);
    })()`);
    log('CONFIRM_VALS=' + JSON.stringify(R.confirmVals));
    check('★C3 确认步的格子是空的（没有把上一步密码带过来）',
          Array.isArray(R.confirmVals) && R.confirmVals.every(v => v === ''),
          R.confirmVals);

    /* ---- D. 二次一致 → 完成卡（按钮样式铁律） ---- */
    await fillPwd();
    const m3 = await modalInfo();
    log('MODAL3=' + JSON.stringify(m3));
    check('★D1 提交成功，出现完成卡', !!m3 && /已设置|已更新/.test(m3.text),
          m3 ? m3.text.replace(/\n/g, ' ').slice(0, 80) : null);
    const doneBtn = m3 && (m3.btns || []).find(b => /^完成$/.test(b.txt));
    check('★D2 「完成」有渐变背景', !!doneBtn && /gradient/.test(doneBtn.bg),
          doneBtn ? doneBtn.bg : null);
    check('★D3 「完成」有内边距', !!doneBtn && parseFloat(doneBtn.padL) > 0,
          doneBtn ? doneBtn.padL : null);
    await h.screenshot('_reports/e2e_paypwd_modal.png');

    /* ---- E. 关闭后状态刷新 ---- */
    await h.eval(`(() => {
      const vis=${VISFN};
      const scrims=[...document.querySelectorAll('[data-modal-scrim]')].filter(vis);
      const dlg=scrims[scrims.length-1];
      const b=[...dlg.querySelectorAll('button')].filter(vis).find(x=>/^完成$/.test((x.innerText||'').trim()));
      if(b) b.click();
    })()`);
    await H.sleep(1500);
    const m4 = await modalInfo();
    check('E1 点「完成」后弹窗关闭', !m4, m4 ? 'still open' : null);
    const cardTxt = await h.eval(`(() => {
      const vis=${VISFN};
      const d=[...document.querySelectorAll('div')].filter(vis)
        .find(x=>(x.innerText||'').trim().startsWith('支付密码') && (x.innerText||'').length < 120);
      return d ? d.innerText.replace(/\\n/g,' | ') : '';
    })()`);
    check('E2 安全中心状态刷成「已设置」', /已设置/.test(cardTxt), cardTxt);

    console.log('\n' + ok + ' passed, ' + fail + ' failed');
  } catch (e) {
    log('ERR ' + (e && e.stack || e));
    console.log('ERR ' + (e && e.message || e));
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(0);
  }
})();
