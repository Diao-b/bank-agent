/* 账单页问题复验 · 真机 CDP 点击测试
   覆盖：①「操作」列按钮无需 hover 即可见 ②趋势图 Y 轴有数值 ③展开/收起 ④筛选 ⑤行内「问小微」跳转预填
   用法：XB_TOKEN=<token> node scripts/_e2e_bills_clicks.js */
const H = require('./_e2e_lib.js');
const fs = require('fs');

const TOKEN = process.env.XB_TOKEN || '';
const LOG = '_reports/_e2e_bills_clicks.log';
function log(...a) { const s = a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' '); try { fs.appendFileSync(LOG, s + '\n'); } catch (e) {} }
const VISFN = 'e=>(e.offsetParent||e.getClientRects().length)';

/* 页面侧工具：React 受控 <select> 必须用原生 setter + change 事件 */
const SET_SEL = `const setSel=(el,val)=>{const s=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set;s.call(el,val);el.dispatchEvent(new Event('change',{bubbles:true}));};`;

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1280, height: 800 });
  const R = {};
  try {
    log('innerWidth=' + await h.eval('innerWidth') + ' innerHeight=' + await h.eval('innerHeight'));
    await h.goto(H.BASE + '/');
    await h.clearToken();
    await h.eval("localStorage.setItem('xb_user','user1')");
    await h.eval("localStorage.setItem('xb:u:user1:privacy','off')");   // 隐私关：核对坐标轴真实数值
    await h.setToken(TOKEN);
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.nav('/bills');
    await h.waitForText('本月总支出', 12000);

    /* ① 展开两个折叠区（点「展开查看」，先异常后流水） */
    log('expand1=' + await h.clickByText('展开查看', { tag: 'button', visibleOnly: true, nth: 0 }));
    await H.sleep(700);
    log('expand2=' + await h.clickByText('展开查看', { tag: 'button', visibleOnly: true, nth: 0 }));
    await H.sleep(1200);
    await h.screenshot('_reports/_e2e_bills_fix_1_expanded.png');

    /* ② 图表：Y 轴数值 + 环形图中心金额 */
    R.charts = JSON.parse(await h.eval(`(() => {
      const svgs=[...document.querySelectorAll('svg')];
      const trend=svgs.find(s=>s.querySelector('polyline'));
      const donut=svgs.find(s=>s.querySelectorAll('circle[stroke-dasharray]').length>0);
      const yLabels=trend?[...trend.querySelectorAll('text')].filter(t=>t.getAttribute('text-anchor')==='end').map(t=>t.textContent):[];
      const xLabels=trend?[...trend.querySelectorAll('text')].filter(t=>t.getAttribute('text-anchor')==='middle').map(t=>t.textContent):[];
      const donutTexts=donut?[...donut.querySelectorAll('text')].map(t=>t.textContent):[];
      return JSON.stringify({ yLabels, xLabels, donutTexts });
    })()`));

    /* ③ 「操作」列按钮：不悬停也应可见（有效 opacity 必须为 1） */
    R.actionBtn = JSON.parse(await h.eval(`(() => {
      const row=document.querySelector('table tbody tr');
      if(!row) return JSON.stringify({err:'no row'});
      const btn=[...row.querySelectorAll('button')].find(b=>/问小微/.test(b.innerText||''));
      if(!btn) return JSON.stringify({err:'no ask btn'});
      let el=btn, op=1; while(el&&el!==document.body){op*=parseFloat(getComputedStyle(el).opacity||'1'); el=el.parentElement;}
      const r=btn.getBoundingClientRect();
      return JSON.stringify({ text:(btn.innerText||'').trim(), rects:btn.getClientRects().length,
        effOpacity:Math.round(op*100)/100, w:Math.round(r.width), h:Math.round(r.height) });
    })()`));

    /* ④ 筛选：改「分类」为第一个非空项 → 表格行数变化 */
    R.filter = JSON.parse(await h.eval(`(() => {
      ${SET_SEL}
      const sels=[...document.querySelectorAll('select')].filter(${VISFN});
      if(sels.length<3) return JSON.stringify({err:'selects<3', n:sels.length});
      const before=document.querySelectorAll('table tbody tr').length;
      const opts=[...sels[1].options].map(o=>o.value).filter(Boolean);
      if(!opts.length) return JSON.stringify({before, changed:false, why:'no category options'});
      setSel(sels[1], opts[0]);
      return JSON.stringify({before, picked:opts[0], total:sels.length});
    })()`));
    await H.sleep(1400);
    R.filterAfter = await h.eval(`document.querySelectorAll('table tbody tr').length`);
    R.filterNote = (await h.bodyText()).includes('该筛选条件下暂无流水') ? 'empty-state' : 'rows-or-loading';

    /* ⑤ 行内「问小微」→ 跳到对话页且输入框已预填 */
    const clickAsk = await h.clickByText('问小微', { tag: 'button', visibleOnly: true, nth: 0 });
    log('clickAsk=' + clickAsk);
    await H.sleep(1600);
    R.afterAsk = JSON.parse(await h.eval(`(() => {
      const ta=document.querySelector('textarea');
      return JSON.stringify({ hash:location.hash, input: ta ? (ta.value||'').slice(0,40) : null,
                              hasTextarea: !!ta });
    })()`));
    await h.screenshot('_reports/_e2e_bills_fix_2_ask.png');

    /* ⑥ 回账单页：先展开再收起，双向都点一遍 */
    await h.nav('/bills');
    await h.waitForText('本月分类占比', 10000);
    R.collapsedDefault = await h.eval(`JSON.stringify({ hasTable: !!document.querySelector('table'),
      expands: [...document.querySelectorAll('button')].filter(${VISFN}).filter(b=>/展开查看/.test(b.innerText||'')).length })`);
    await h.clickByText('展开查看', { tag: 'button', visibleOnly: true, nth: 0 });
    await H.sleep(400);
    await h.clickByText('展开查看', { tag: 'button', visibleOnly: true, nth: 0 });
    await H.sleep(900);
    R.expanded = JSON.parse(await h.eval(`JSON.stringify({ hasTable: !!document.querySelector('table'),
      collapses: [...document.querySelectorAll('button')].filter(${VISFN}).filter(b=>/^收起$/.test((b.innerText||'').trim())).length,
      selects: [...document.querySelectorAll('select')].filter(${VISFN}).length })`));
    await h.clickByText('收起', { tag: 'button', visibleOnly: true, nth: 0 });
    await H.sleep(400);
    await h.clickByText('收起', { tag: 'button', visibleOnly: true, nth: 0 });
    await H.sleep(900);
    R.recollapsed = JSON.parse(await h.eval(`JSON.stringify({ hasTable: !!document.querySelector('table'),
      expands: [...document.querySelectorAll('button')].filter(${VISFN}).filter(b=>/展开查看/.test(b.innerText||'')).length,
      selects: [...document.querySelectorAll('select')].filter(${VISFN}).length })`));

    /* ⑦ 隐私开：应出现「数值已隐藏」提示、Y 轴变 ···（用 hash 路由，别直连 /bills——那是 404） */
    await h.eval("localStorage.setItem('xb:u:user1:privacy','on')");
    await h.goto(H.BASE + '/chat', { reload: true });
    await h.waitForText('新会话', 12000);
    await h.nav('/bills');
    await h.waitForText('本月分类占比', 12000);
    await h.clickByText('展开查看', { tag: 'button', visibleOnly: true, nth: 0 });
    await H.sleep(600);
    R.privacyOn = JSON.parse(await h.eval(`(() => {
      const t=document.body.innerText||'';
      const svgs=[...document.querySelectorAll('svg')];
      const trend=svgs.find(s=>s.querySelector('polyline'));
      const yLabels=trend?[...trend.querySelectorAll('text')].filter(x=>x.getAttribute('text-anchor')==='end').map(x=>x.textContent):[];
      const donut=svgs.find(s=>s.querySelectorAll('circle[stroke-dasharray]').length>0);
      const donutTexts=donut?[...donut.querySelectorAll('text')].map(x=>x.textContent):[];
      return JSON.stringify({ hint: t.includes('隐私模式：数值已隐藏'), yLabels, donutTexts });
    })()`));
    await h.screenshot('_reports/_e2e_bills_fix_3_privacy_on.png');

    log('CHARTS=' + JSON.stringify(R.charts));
    log('ACTION_BTN=' + JSON.stringify(R.actionBtn));
    log('FILTER=' + JSON.stringify(R.filter) + ' after=' + R.filterAfter + ' note=' + R.filterNote);
    log('AFTER_ASK=' + JSON.stringify(R.afterAsk));
    log('COLLAPSE default=' + R.collapsedDefault + ' expanded=' + JSON.stringify(R.expanded) + ' recollapsed=' + JSON.stringify(R.recollapsed));
    log('PRIVACY_ON=' + JSON.stringify(R.privacyOn));
    await h.close();
    process.exit(0);
  } catch (e) {
    try { fs.appendFileSync(LOG, 'ERR ' + (e && e.stack || e) + '\n'); } catch (_) {}
    process.exit(1);
  }
})();
