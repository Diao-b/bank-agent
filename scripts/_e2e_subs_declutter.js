/* 页面降噪 · 第 1 批验收（真机 CDP）
   验证 #/subs 新结构：首屏区块数、金色主按钮数、主视图 input 数、Tab、弹窗。
   用法：XB_TOKEN=<token> node scripts/_e2e_subs_declutter.js
   日志：_reports/_e2e_subs_declutter.log（绕开管道块缓冲） */
const H = require('./_e2e_lib.js');
const fs = require('fs');

const TOKEN = process.env.XB_TOKEN || '';
const LOG = '_reports/_e2e_subs_declutter.log';
function log(...a) { const s = a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' '); try { fs.appendFileSync(LOG, s + '\n'); } catch (e) {} }

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1280, height: 800 });
  const R = {};
  try {
    log('innerWidth=' + await h.eval('innerWidth') + ' innerHeight=' + await h.eval('innerHeight'));

    await h.goto(H.BASE + '/');
    await h.clearToken();
    await h.eval("localStorage.setItem('xb_user','user1')");
    await h.setToken(TOKEN);
    await h.eval("localStorage.removeItem('xb:u:user1:subs_tab')");   // 清 Tab 记忆 → 验默认落在「生效中」
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.nav('/subs');
    await h.waitForText('生效中', 12000);

    /* 1) 首屏区块：PageShell 直接子元素（排除敏感提示行） */
    R.blocks = JSON.parse(await h.eval(`(() => {
      const shell = document.querySelector('.overflow-y-auto.space-y-5');
      if (!shell) return JSON.stringify({err:'no shell'});
      const kids = [...shell.children].filter(e => e.offsetParent !== null).map(e => {
        const r = e.getBoundingClientRect();
        return { top: Math.round(r.top), h: Math.round(r.height),
                 head: (e.innerText||'').trim().slice(0,22).replace(/\\n/g,'|') };
      });
      return JSON.stringify({ innerHeight: innerHeight, kids });
    })()`));

    /* 2) 金色实心主按钮（可见 bg-brand-600） */
    R.goldBtns = JSON.parse(await h.eval(`(() => {
      const els=[...document.querySelectorAll('button')].filter(e=>e.offsetParent!==null && /bg-brand-600/.test(e.className));
      return JSON.stringify(els.map(e=>(e.innerText||'').trim().slice(0,20)));
    })()`));

    /* 3) 主视图可见 input/select */
    R.inputs = JSON.parse(await h.eval(`(() => {
      const els=[...document.querySelectorAll('input,select')].filter(e=>e.offsetParent!==null);
      return JSON.stringify(els.map(e=>e.name||e.type||e.tagName));
    })()`));

    /* 4) Tab 标题（带数量） */
    R.tabs = JSON.parse(await h.eval(`(() => {
      const b=[...document.querySelectorAll('button')].filter(e=>e.offsetParent!==null && /生效中|待确认|已结束/.test(e.innerText||''));
      return JSON.stringify(b.map(e=>e.innerText.trim()));
    })()`));

    await h.screenshot('_reports/_e2e_subs_1_live.png');
    log('LIVE=' + JSON.stringify(R));

    /* 5) 打开签约弹窗 → 字段清单
       ★ fixed 定位元素的 offsetParent 恒为 null，可见性必须用 getClientRects().length 兜底 */
    log('clickContract=' + await h.clickByText('+ 签约新订阅', { tag: 'button', visibleOnly: true }));
    await H.sleep(900);
    R.modal = JSON.parse(await h.eval(`(() => {
      const vis = e => (e.offsetParent || e.getClientRects().length);
      const d=[...document.querySelectorAll('[data-modal-scrim]')].filter(vis);
      const el=d[d.length-1]; if(!el) return JSON.stringify({err:'no modal', scrims:document.querySelectorAll('[data-modal-scrim]').length});
      return JSON.stringify({ title:(el.innerText||'').trim().slice(0,90),
        inputs:[...el.querySelectorAll('input,select')].map(e=>e.name||e.type) });
    })()`));
    await h.screenshot('_reports/_e2e_subs_2_contract.png');
    /* 关弹窗 */
    await h.eval(`(()=>{const vis=e=>(e.offsetParent||e.getClientRects().length);const d=[...document.querySelectorAll('[data-modal-scrim]')].filter(vis);const el=d[d.length-1];if(el)el.dispatchEvent(new MouseEvent('click',{bubbles:true}));})()`);
    await H.sleep(800);
    R.modalClosed = await h.eval(`(()=>{const vis=e=>(e.offsetParent||e.getClientRects().length);return [...document.querySelectorAll('[data-modal-scrim]')].filter(vis).length;})()`);

    /* 5b) 回「生效中」→ 行内「详情」展开 → 附属信息应出现；「取消订阅」→ 危险确认弹窗 */
    await h.clickByText('生效中', { tag: 'button', visibleOnly: true });
    await H.sleep(800);
    await h.clickByText('详情', { tag: 'button', visibleOnly: true });
    await H.sleep(600);
    R.detailOpen = (await h.bodyText()).includes('上次实扣');
    await h.screenshot('_reports/_e2e_subs_5_detail.png');
    await h.clickByText('取消订阅', { tag: 'button', visibleOnly: true });
    await H.sleep(900);
    R.cancelModal = JSON.parse(await h.eval(`(()=>{const vis=e=>(e.offsetParent||e.getClientRects().length);const d=[...document.querySelectorAll('[data-modal-scrim]')].filter(vis);const el=d[d.length-1];return JSON.stringify({n:d.length,title:el?(el.innerText||'').trim().slice(0,60):null});})()`));
    await h.screenshot('_reports/_e2e_subs_6_cancel.png');
    /* 净副作用 0：点「取消」关掉危险确认，不落库 */
    await h.eval(`(()=>{const vis=e=>(e.offsetParent||e.getClientRects().length);const d=[...document.querySelectorAll('[data-modal-scrim]')].filter(vis);const el=d[d.length-1];if(el){const b=[...el.querySelectorAll('button')].find(x=>/取消/.test(x.innerText||''));if(b)b.click();}})()`);
    await H.sleep(500);

    /* 6) 待确认 Tab */
    await h.clickByText('待确认', { tag: 'button', visibleOnly: true });
    await H.sleep(1000);
    R.pendingText = (await h.bodyText()).replace(/\n+/g, ' | ').slice(0, 500);
    await h.screenshot('_reports/_e2e_subs_3_pending.png');

    /* 7) 已结束 Tab */
    await h.clickByText('已结束', { tag: 'button', visibleOnly: true });
    await H.sleep(1000);
    R.endedText = (await h.bodyText()).replace(/\n+/g, ' | ').slice(0, 400);
    await h.screenshot('_reports/_e2e_subs_4_ended.png');

    log('MODAL=' + JSON.stringify(R.modal));
    log('DETAIL_OPEN=' + R.detailOpen + ' CANCEL_MODAL=' + JSON.stringify(R.cancelModal));
    log('PENDING=' + R.pendingText);
    log('ENDED=' + R.endedText);
    log('FINAL=' + JSON.stringify({ gold: R.goldBtns, inputs: R.inputs, tab: R.tabs,
                                    blocks: R.blocks.kids, modalClosed: R.modalClosed }, null, 1));
    await h.close();
    process.exit(0);
  } catch (e) {
    try { fs.appendFileSync(LOG, 'ERR ' + (e && e.stack || e) + '\n'); } catch (_) {}
    process.exit(1);
  }
})();
