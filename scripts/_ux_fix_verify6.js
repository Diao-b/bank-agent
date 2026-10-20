/* 批 3 · 真机验收：S-07 订阅对账三件套 · G-06 全站重试 · G-07 主按钮配色 · F-04 口径文案 · B-05 图注 · G-09 空会话
 * 用法：node scripts/_ux_fix_verify6.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_fix_verify6.log'));
let pass = 0, fail = 0;
const ok = (n, c, e) => { if (c) { pass++; log('PASS', n + (e !== undefined ? '  ' + JSON.stringify(e) : '')); } else { fail++; log('FAIL', n + '  ' + JSON.stringify(e)); } };

async function login(h) {
  await h.goto(BASE + '/login');
  await h.eval(`localStorage.clear()`);
  await h.setViewport(1440, 920);
  await h.goto(BASE + '/login');
  await h.type('input[placeholder="请输入用户名"]', 'user1');
  await h.type('input[placeholder="请输入密码"]', '123456');
  await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
  await sleep(3600);
}

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  try {
    await login(h);

    /* ============ S-07 订阅对账三件套 ============ */
    await h.eval(`location.hash = '#/subs'`);
    await sleep(3000);
    const subsTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix6_subs_fields.png'));
    ok('S-07 ★列表出现「扣款商户」', /扣款商户/.test(subsTxt), (subsTxt.match(/扣款商户[^\n]{0,20}/) || [''])[0]);
    ok('S-07 ★列表出现「上次实扣」金额', /上次实扣/.test(subsTxt), (subsTxt.match(/上次实扣[\s\S]{0,20}/) || [''])[0].replace(/\n/g, ' '));
    ok('S-07 ★扣款账户口径写明（不编造具体卡）', /从余额足够的账户自动扣款/.test(subsTxt));
    ok('S-07 时间字段改名「上次扣款日」，避免与金额混淆',
      /上次扣款日/.test(subsTxt) && !/· 上次扣款 20/.test(subsTxt));
    const api = await h.eval(`(async()=>{const r=await fetch('/api/subscriptions',{headers:authHeaders()});const j=await r.json();
      const a=(j.subscriptions||[]).filter(x=>x.status==='active');
      return JSON.stringify({hasKey: a.every(x=>'last_amount' in x), sample: a.slice(0,3).map(x=>x.last_amount)});})()`);
    ok('S-07 接口返回 last_amount 字段', /"hasKey":true/.test(api), JSON.parse(api));

    /* ============ G-09 会话栏空条目 ============ */
    await h.eval(`location.hash = '#/chat'`);
    await sleep(2600);
    const chatTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix6_convbar.png'));
    const emptyRows = await h.eval(`(()=>{const t=document.body.innerText;return (t.match(/未开始/g)||[]).length})()`);
    ok('G-09 ★会话栏不再堆「未开始」空条目（≤1 条：仅当前会话）', Number(emptyRows) <= 1, { emptyRows });

    /* ============ F-04 / B-05 文案口径 ============ */
    await h.eval(`location.hash = '#/safe'`);
    await sleep(2600);
    const safeTxt = (await h.bodyText()) || '';
    ok('F-04 ★安全中心统计口径如实', /最近 50 条/.test(safeTxt) && /需要复核的操作/.test(safeTxt),
      { 旧词: /累计操作数|超额 \/ 越权/.test(safeTxt) });
    await h.eval(`location.hash = '#/bills'`);
    await sleep(2800);
    const billsTxt = (await h.bodyText()) || '';
    ok('B-05 ★图表/提醒标注了各自口径',
      /按支出金额占比/.test(billsTxt) && /按扣费笔数判断/.test(billsTxt),
      { 金额口径: /按支出金额占比/.test(billsTxt), 笔数口径: /按扣费笔数判断/.test(billsTxt) });

    /* ============ G-07 主按钮配色统一（金色） ============ */
    const PICK = (re) => `(()=>{
      const b=[...document.querySelectorAll('button')].find(x=>${re}.test((x.innerText||'').trim())&&!x.disabled);
      if(!b) return null; const cs=getComputedStyle(b);
      return JSON.stringify({t:b.innerText.trim().slice(0,10), bg:cs.backgroundColor, img:(cs.backgroundImage||'').slice(0,60)});
    })()`;
    await h.eval(`location.hash = '#/subs'`);
    await sleep(2800);
    const contractBtn = JSON.parse((await h.eval(PICK('/^签约代扣$/'))) || 'null');
    await h.eval(`location.hash = '#/cards'`);
    await sleep(2800);
    const applyBtn = JSON.parse((await h.eval(PICK('/^立即申请$/'))) || 'null');
    const gp = { contract: contractBtn, apply: applyBtn };
    const isGold = (o) => !!o && (/235, 185, 59|212, 160, 23/.test(o.bg) || /235, 185, 59|212, 160, 23/.test(o.img));
    ok('G-07 ★签约代扣与申请新卡用同一套主行动色（金色）', isGold(gp.contract) && isGold(gp.apply), gp);
    const cyan = await h.eval(`(()=>[...document.querySelectorAll('button')].filter(b=>/74, 236, 212/.test(getComputedStyle(b).backgroundColor)).length)()`);
    ok('G-07 全站不再有青色主行动按钮', Number(cyan) === 0, { cyanCount: cyan });
    const dangerWired = await h.eval(`/DANGER_ACTIONS = new Set\\(\\['delete_card', 'cancel_subscription'\\]\\)/.test(document.documentElement.innerHTML)`);
    ok('G-07 不可逆动作已接入红色确认（静态校验，实际生效于最终确认步）', dangerWired === true);

    /* ============ G-06 全站错误态可重试（真断接口） ============ */
    await h.b.send('Network.setBlockedURLs', { urls: ['*/api/holdings*'] }, h.sid);
    await h.eval(`location.hash = '#/chat'`); await sleep(700);
    await h.eval(`location.hash = '#/wealth'`);
    await sleep(3200);
    const wTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix6_retry.png'));
    const hasRetryBtn = await h.eval(`[...document.querySelectorAll('button')].some(b=>(b.innerText||'').trim()==='重试')`);
    ok('G-06 ★理财页接口失败时出现「重试」按钮', hasRetryBtn === true, { sample: (wTxt.match(/[^\n]{0,30}(暂不可用|重试)[^\n]{0,20}/) || [''])[0] });
    await h.b.send('Network.setBlockedURLs', { urls: [] }, h.sid);
    await h.clickByText('重试', { tag: 'button', nth: 0 });
    await sleep(2600);
    const recovered = await h.eval(`!document.body.innerText.includes('银行服务暂不可用')`);
    ok('G-06 ★点「重试」能原地恢复（无需刷新整页）', recovered === true);

    log('RESULT', `PASS=${pass} FAIL=${fail}`);
  } catch (e) {
    fail++; log('EXCEPTION', String((e && e.message) || e));
  } finally { await h.close().catch(() => {}); }
  process.exit(fail === 0 ? 0 : 1);
})();
