/* 全站页面审计（真机 CDP）：隐形动作 / 过渡小字号 / 遮罩是否有解释 / 图表坐标文字 + 逐页安全点击
   用法：XB_TOKEN=<token> node scripts/_e2e_full_audit.js
   日志：_reports/_e2e_full_audit.log（跑完看这个文件）
   ★ 只点"无副作用"的控件：Tab / 展开收起 / 详情 / 问小微 / 打开弹窗后立刻关闭；不提交任何写操作。 */
const H = require('./_e2e_lib.js');
const fs = require('fs');

const TOKEN = process.env.XB_TOKEN || '';
const LOG = '_reports/_e2e_full_audit.log';
function log(...a) { const s = a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' '); try { fs.appendFileSync(LOG, s + '\n'); } catch (e) {} }

const PAGES = [
  { route: '/chat', name: '对话' },
  { route: '/bills', name: '账单分析' },
  { route: '/wealth', name: '理财' },
  { route: '/cards', name: '卡片' },
  { route: '/subs', name: '订阅' },
  { route: '/safe', name: '安全中心' },
  { route: '/transfer', name: '转账' },
  { route: '/appearance', name: '数字人形象' },
];

const METRICS_JS = `(() => {
  const vis = e => (e.offsetParent || e.getClientRects().length);
  const eff = e => { let el=e, op=1; while(el&&el!==document.body){ op*=parseFloat(getComputedStyle(el).opacity||'1'); el=el.parentElement; } return op; };
  const page = document.querySelector('.overflow-y-auto.space-y-5') || document.querySelector('main') || document.body;
  const faint = [...page.querySelectorAll('button,a,select,input,[role=button]')]
    .filter(vis).filter(e => !e.disabled && eff(e) < 0.99)
    .map(e => ({ t: (e.innerText||e.name||e.tagName||'').trim().slice(0,18), op: Math.round(eff(e)*100)/100 }));
  const tinyHtml = [];
  [...page.querySelectorAll('*')].filter(vis).forEach(e => {
    if (e.children.length) return;
    const s = (e.innerText||'').trim(); if (!s) return;
    const fs = parseFloat(getComputedStyle(e).fontSize||'0');
    if (fs > 0 && fs < 12) tinyHtml.push({ t: s.slice(0,16), fs: Math.round(fs*10)/10 });
  });
  const tinySvg = [];
  [...document.querySelectorAll('svg')].forEach(sv => {
    const m = sv.getScreenCTM(); const sc = m ? m.a : 1;
    [...sv.querySelectorAll('text')].forEach(t => {
      const s = (t.textContent||'').trim(); if (!s) return;
      const fs = parseFloat(getComputedStyle(t).fontSize||'0') * sc;
      if (fs > 0 && fs < 12) tinySvg.push({ t: s.slice(0,16), eff: Math.round(fs*10)/10 });
    });
  });
  const body = document.body.innerText || '';
  const masked = (body.match(/···/g)||[]).length;
  const explained = /隐私模式|已脱敏|已隐藏/.test(body);
  const svgWithText = [...document.querySelectorAll('svg')].filter(s => s.querySelector('text'));
  const chartTexts = svgWithText.map(s => [...s.querySelectorAll('text')].map(t => (t.textContent||'').trim()).join('|')).slice(0,4);
  return JSON.stringify({ faint, tinyHtml, tinySvg, masked, explained, chartCount: svgWithText.length, chartTexts });
})()`;

const CLICKABLE = /^(生效中|待确认|已结束|我的卡片|已注销|在售产品|我的持仓|已赎回|安全设置|操作记录|详情|收起|展开查看|查看风控规则|问小微|数字柜员 开|与小微对话 开|新会话)$/;
const SIG_JS = `JSON.stringify({ n: document.querySelectorAll('*').length, t: (document.body.innerText||'').length })`;

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1280, height: 800, watchdogMs: 900000 });
  const REPORT = {};
  try {
    log('innerWidth=' + await h.eval('innerWidth') + ' innerHeight=' + await h.eval('innerHeight'));
    await h.goto(H.BASE + '/');
    await h.clearToken();
    await h.eval("localStorage.setItem('xb_user','user1')");
    await h.eval("localStorage.setItem('xb:u:user1:privacy','off')");   // 隐私关：先看真实数值与真实可见性
    await h.setToken(TOKEN);
    await h.goto(H.BASE + '/chat', { reload: true });

    for (const p of PAGES) {
      await h.nav(p.route);
      await H.sleep(2600);
      const m = JSON.parse(await h.eval(METRICS_JS));
      const clicks = [];

      /* 安全点击扫描：最多点 6 个匹配控件，每次记录 DOM 是否变化 */
      const btns = JSON.parse(await h.eval(`(() => {
        const vis = e => (e.offsetParent || e.getClientRects().length);
        const NL = String.fromCharCode(10);
        const re = ${CLICKABLE};
        const seen = new Set(); const out = [];
        [...document.querySelectorAll('button')].filter(vis).forEach(b => {
          const t = (b.innerText||'').trim().split(NL)[0].trim();
          if (!re.test(t) || seen.has(t)) return;
          seen.add(t); out.push(t);
        });
        return JSON.stringify(out.slice(0, 6));
      })()`));
      for (const t of btns) {
        const before = await h.eval(SIG_JS);
        const r = await h.clickByText(t, { tag: 'button', visibleOnly: true, exact: true });
        await H.sleep(1000);
        const after = await h.eval(SIG_JS);
        let changed = before !== after;
        if (!changed) {    // 文字可能没变（如展开同一区），再看关键结构
          changed = (await h.eval(`document.querySelectorAll('[data-modal-scrim]').length`)) > 0;
        }
        clicks.push({ btn: t, clicked: /"found":[1-9]/.test(r), changed });
        /* 若点开了弹窗/抽屉，立刻关掉，保证净副作用 0 */
        await h.eval(`(() => { const els=[...document.querySelectorAll('[data-modal-scrim]')].filter(e=>(e.offsetParent||e.getClientRects().length)); const el=els.pop(); if(el) el.dispatchEvent(new MouseEvent('click',{bubbles:true})); })()`);
        await H.sleep(400);
      }

      REPORT[p.name] = { route: p.route, faint: m.faint, tinyHtml: m.tinyHtml, tinySvg: m.tinySvg,
                         masked: m.masked, explained: m.explained, chartCount: m.chartCount, chartTexts: m.chartTexts, clicks };
      await h.screenshot('_reports/_audit_' + p.route.replace(/\//g,'') + '.png');
      log(`--- ${p.name} ${p.route} ---`);
      log('  faint=' + JSON.stringify(m.faint));
      log('  tinyHtml=' + JSON.stringify(m.tinyHtml));
      log('  tinySvg=' + JSON.stringify(m.tinySvg));
      log('  masked=' + m.masked + ' explained=' + m.explained + ' charts=' + m.chartCount + ' texts=' + JSON.stringify(m.chartTexts));
      log('  clicks=' + JSON.stringify(clicks));
    }

    /* 返回对话页并确认页面未崩（无 React 错误层） */
    await h.nav('/chat');
    await H.sleep(1500);
    REPORT.finalOk = !(await h.bodyText()).includes('Something went wrong');
    log('FINAL_OK=' + REPORT.finalOk);
    fs.writeFileSync('_reports/_e2e_full_audit.json', JSON.stringify(REPORT, null, 1));
    await h.close();
    process.exit(0);
  } catch (e) {
    try { fs.appendFileSync(LOG, 'ERR ' + (e && e.stack || e) + '\n'); } catch (_) {}
    process.exit(1);
  }
})();
