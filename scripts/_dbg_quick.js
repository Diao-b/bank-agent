/* 调试：快捷入口按钮分布 + 输入框存在性 */
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const BASE = 'http://127.0.0.1:8178';
(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  try {
    await h.goto(BASE + '/login');
    await h.eval(`localStorage.clear()`);
    await h.setViewport(1440, 920);
    await h.goto(BASE + '/login');
    await h.type('input[placeholder="请输入用户名"]', 'user1');
    await h.type('input[placeholder="请输入密码"]', '123456');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3600);
    await h.eval(`location.hash = '#/chat'`);
    await sleep(2600);
    const info = await h.eval(`(()=>{
      const btns=[...document.querySelectorAll('button')].filter(b=>/^(转账|账单分析|理财|冻结卡|卡密充值)$/.test((b.innerText||'').trim()));
      return JSON.stringify({
        textarea: document.querySelectorAll('textarea').length,
        textareaValue: (document.querySelector('textarea')||{}).value,
        hash: location.hash,
        btns: btns.map(b=>({t:b.innerText.trim(), inNav: !!b.closest('nav'), cls:(b.className||'').slice(0,40)}))
      });
    })()`);
    console.log('BEFORE CLICK:', info);
    await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='账单分析' && x.closest('nav'));if(b)b.click();return 1})()`);
    await sleep(2000);
    const after = await h.eval(`(()=>{const t=document.querySelector('textarea');return JSON.stringify({hash:location.hash, ta: document.querySelectorAll('textarea').length, val:(t||{}).value, bodyHasChat:/小微在线/.test(document.body.innerText)})})()`);
    console.log('AFTER CLICK:', after);
    await h.screenshot('_reports/ux_audit/_dbg_quick.png');
  } catch (e) { console.log('ERR', e.message); }
  finally { await h.close().catch(() => {}); }
  process.exit(0);
})();
