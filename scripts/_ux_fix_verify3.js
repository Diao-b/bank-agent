/* 批 2 续 · 真机验收：理财页签（W-02）/ 理财关键字号（W-03）/ 窄屏顶栏（G-01/G-04）
 * 用法：node scripts/_ux_fix_verify3.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_fix_verify3.log'));
let pass = 0, fail = 0;
const ok = (n, c, e) => { if (c) { pass++; log('PASS', n + (e !== undefined ? '  ' + JSON.stringify(e) : '')); } else { fail++; log('FAIL', n + '  ' + JSON.stringify(e)); } };

/* 用 Range 的 client rects 数「真实行数」——比 clientHeight/lineHeight 比值可靠
   （按钮内边距、40px 头像都会被后者误判成"换行了"） */
const LINE_COUNT = `(() => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const out = [];
  document.querySelectorAll('header *').forEach(e => {
    if (!vis(e) || e.children.length) return;
    const t = (e.innerText || '').trim();
    if (!t || t.length < 2) return;
    const r = document.createRange(); r.selectNodeContents(e);
    const rects = [...r.getClientRects()].filter(x => x.width > 1 && x.height > 1);
    // 注意：React 会把 "隐私 {开}" 渲染成两个文本节点 → 单行也有 2 个 rect。
    // 真正的换行判据是「不同的 top 值 > 1」。
    const tops = new Set(rects.map(x => Math.round(x.top)));
    if (tops.size > 1) out.push(t.slice(0, 24) + ' ×' + tops.size);
  });
  return JSON.stringify(out.slice(0, 8));
})()`;

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

    /* ============ W-02 理财页签 ============ */
    await h.eval(`location.hash = '#/wealth'`);
    await sleep(3000);
    const t1 = (await h.bodyText()) || '';
    const h1 = await h.eval(`(()=>{const c=[...document.querySelectorAll('*')].filter(e=>/(auto|scroll)/.test(getComputedStyle(e).overflowY)&&e.scrollHeight-e.clientHeight>40).sort((a,b)=>(b.scrollHeight-b.clientHeight)-(a.scrollHeight-a.clientHeight))[0];return c?c.scrollHeight:0})()`);
    await h.screenshot(path.join(OUT, 'fix3_wealth_holding.png'));
    ok('W-02 ★默认只显示「我的持仓」（不渲染在售产品面板）',
      /我的持仓/.test(t1) && /买入日/.test(t1) && !/在售理财产品/.test(t1), { scrollHeight: h1 });

    await h.clickByText('在售产品', { tag: 'button', nth: 0 });
    await sleep(1500);
    const t2 = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix3_wealth_product.png'));
    // 注意：产品「期限 活期/7天灵活赎回」里也含"赎回"二字，持仓面板要用「买入日」作标记
    ok('W-02 ★切到「在售产品」后持仓面板消失',
      /在售理财产品/.test(t2) && /立即购买/.test(t2) && !/买入日/.test(t2));

    /* ============ W-03 关键信息字号（在产品页签下量） ============ */
    const fsObj = JSON.parse(await h.eval(`(()=>{
      // 风险徽标内部有"圆点"子元素，所以不能要求 children.length===0
      const pick = (re) => { const es=[...document.querySelectorAll('span,div')].filter(x=>{const t=(x.innerText||'').trim();return t && re.test(t) && x.querySelectorAll('*').length <= 2;}); return es.length?es.map(e=>getComputedStyle(e).fontSize):null; };
      return JSON.stringify({ risk: pick(/^R[123]/), annual: pick(/^年化收益率$/), min: pick(/^起购金额$/) });
    })()`) || '{}');
    const smallest = (arr) => (arr && arr.length) ? Math.min(...arr.map(parseFloat)) : null;
    ok('W-03 ★风险徽标 / 年化标签 / 起购标签 字号 ≥12px',
      ['risk', 'annual', 'min'].every(k => smallest(fsObj[k]) != null && smallest(fsObj[k]) >= 12), fsObj);

    /* ============ G-01 / G-04 窄屏 ============ */
    await h.setViewport(746, 900);
    await h.eval(`location.hash = '#/chat'`);
    await sleep(2400);
    await h.screenshot(path.join(OUT, 'fix3_narrow_chat.png'));
    const wrapped = JSON.parse(await h.eval(LINE_COUNT));
    ok('G-01 ★746px 窄屏顶栏无多行/逐字断行', wrapped.length === 0, { wrapped });
    const overflow = await h.eval(`document.documentElement.scrollWidth > window.innerWidth + 2`);
    ok('G-01 窄屏无横向溢出', overflow === false, { overflow });
    const convHidden = await h.eval(`(()=>{const t=document.body.innerText;return !/暂无历史会话|条消息/.test(t)})()`);
    ok('G-04 ★窄屏不再常驻会话列表（216px 让给正文）', convHidden === true, { convHidden });

    // 回到宽屏：会话列表与顶栏恢复
    await h.setViewport(1440, 920);
    await sleep(1600);
    const wideWrapped = JSON.parse(await h.eval(LINE_COUNT));
    ok('G-01 宽屏顶栏同样无断行（未误伤大屏）', wideWrapped.length === 0, { wideWrapped });
    const convBack = await h.eval(`(()=>{const t=document.body.innerText;return /暂无历史会话|条消息|新会话/.test(t)})()`);
    ok('G-04 宽屏会话列表恢复显示', convBack === true, { convBack });
    await h.screenshot(path.join(OUT, 'fix3_wide_chat.png'));

    log('RESULT', `PASS=${pass} FAIL=${fail}`);
  } catch (e) {
    fail++; log('EXCEPTION', String((e && e.message) || e));
  } finally { await h.close().catch(() => {}); }
  process.exit(fail === 0 ? 0 : 1);
})();
