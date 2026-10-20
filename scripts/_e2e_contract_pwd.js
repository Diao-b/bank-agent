/* 真机回归：订阅页「签约新订阅」新增的支付密码步骤（P0.2 票据配套 UI）
 *  流程：填表提交 → 确认卡「确认」→ ★必须出现 PasswordCard（支付密码步骤）
 *  净副作用 0：到密码步即取消 —— 不输密码、不发签约请求（has_passed 未消费）。
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const LOG = '_reports/_e2e_contract_pwd.log';
const log = (...a) => { try { fs.appendFileSync(LOG, a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {} };
const VISFN = "e=>!!(e.offsetParent||e.getClientRects().length)";

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1440, height: 900, watchdogMs: 180000 });
  const R = {};
  try {
    await h.goto(H.BASE + '/');
    await h.eval("localStorage.clear(); localStorage.setItem('xb_user','user1')");
    await h.setToken(process.env.XB_TOKEN || '');
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.eval("location.hash = '#/subs'");
    await H.sleep(2000);
    R.hash = await h.eval('location.hash');
    log('HASH=' + R.hash);

    // 1) 打开「签约新订阅」弹窗
    await h.eval(`(() => {
      const vis=${VISFN};
      const btn=[...document.querySelectorAll('button')].filter(vis).find(b=>/签约新订阅|签约代扣/.test(b.innerText||''));
      if (btn) btn.click();
      return btn ? 'opened' : 'no-btn';
    })()`);
    await H.sleep(800);

    // 2) 填表并提交（表单在 C7Modal 里）
    R.filled = await h.eval(`(() => {
      const vis=${VISFN};
      const dlg=[...document.querySelectorAll('[role=dialog]')].filter(vis).pop();
      if (!dlg) return 'no-dialog';
      const q=(n)=>dlg.querySelector('[name='+n+']');
      if (!q('service_name')) return 'no-form';
      const set=(el,v)=>{const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set; s.call(el,v); el.dispatchEvent(new Event('input',{bubbles:true}));};
      set(q('service_name'),'真机回归·密码步');
      set(q('fee'),'3');
      const sub=[...dlg.querySelectorAll('button')].filter(vis).find(b=>/签约代扣/.test(b.innerText||'')&&b.type==='submit');
      if (!sub) return 'no-submit';
      sub.click();
      return 'submitted';
    })()`);
    log('FILLED=' + R.filled);
    await H.sleep(800);

    // 3) 确认卡 →「确认」
    R.confirm = await h.eval(`(() => {
      const vis=${VISFN};
      const scrims=[...document.querySelectorAll('[data-modal-scrim]')].filter(vis);
      const dlg=scrims[scrims.length-1];
      if (!dlg) return 'no-scrim';
      const btn=[...dlg.querySelectorAll('button')].filter(vis).find(b=>/确认/.test(b.innerText||'')&&!/取消|不可撤销/.test(b.innerText||''));
      if (!btn) return 'no-confirm-btn';
      btn.click();
      return 'confirmed';
    })()`);
    log('CONFIRM=' + R.confirm);
    await H.sleep(1200);

    // 4) 断言：出现支付密码输入（PasswordCard —— 6 位格子输入，非 type=password）
    R.pwdStep = await h.eval(`(() => {
      const vis=${VISFN};
      const texts=[...document.querySelectorAll('div,span')].filter(vis)
        .map(d=>(d.innerText||''));
      const title = texts.find(t=>/输入支付密码/.test(t)&&t.length<60);
      const boxes = [...document.querySelectorAll('input')].filter(vis)
        .filter(i=>i.getAttribute('inputmode')==='numeric'||i.maxLength===1).length;
      return { shown: !!title, boxes, hint: title ? title.slice(0,40) : '' };
    })()`);
    log('PWD_STEP=' + JSON.stringify(R.pwdStep));
    await h.screenshot('_reports/_e2e_contract_pwd.png');

    // 5) 取消（不输密码 → 不发签约请求，净副作用 0）
    await h.eval(`(() => {
      const vis=${VISFN};
      const btns=[...document.querySelectorAll('button')].filter(vis);
      const c=btns.reverse().find(b=>/取消|返回/.test(b.innerText||''));
      if (c) c.click();
    })()`);
    await H.sleep(500);
    R.verdict = {
      onSubsPage: String(R.hash).includes('/subs'),
      formSubmitted: R.filled === 'submitted',
      confirmPassed: R.confirm === 'confirmed',
      pwdStepShown: !!(R.pwdStep && R.pwdStep.shown),
    };
    log('VERDICT=' + JSON.stringify(R.verdict));
  } catch (e) {
    log('ERR ' + (e && e.stack || e));
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(0);
  }
})();
