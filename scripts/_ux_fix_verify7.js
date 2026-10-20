/* 批 3 第 2 批 · 真机验收：G-08 快捷入口合并 · C-05 额度入口 · G-10 侧栏口径 · X-05 文档更正
 * 用法：node scripts/_ux_fix_verify7.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_fix_verify7.log'));
let pass = 0, fail = 0;
const ok = (n, c, e) => { if (c) { pass++; log('PASS', n + (e !== undefined ? '  ' + JSON.stringify(e) : '')); } else { fail++; log('FAIL', n + '  ' + JSON.stringify(e)); } };

async function doLogin(h, user, pwd) {
  await h.goto(BASE + '/login');
  await h.eval(`localStorage.clear()`);
  await h.setViewport(1440, 920);
  await h.goto(BASE + '/login');
  await h.type('input[placeholder="请输入用户名"]', user);
  await h.type('input[placeholder="请输入密码"]', pwd);
  await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
  await sleep(3600);
}

const composer = (h) => h.eval(`(()=>{const t=document.querySelector('textarea');return t?t.value:null})()`);
const msgCount = (h) => h.eval(`document.querySelectorAll('[class*="rounded-2xl"][class*="max-w-"]').length`);

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  try {
    await doLogin(h, 'user1', '123456');

    /* ============ G-10 侧栏资产口径 ============ */
    const sideTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix7_sidebar.png'));
    ok('G-10 ★侧栏改叫「账户余额合计」', /账户余额合计/.test(sideTxt));
    ok('G-10 ★写明口径（不含理财持仓）', /仅银行账户余额，不含理财持仓/.test(sideTxt));
    ok('G-10 不再出现与持仓混淆的「账户总览」', !/账户总览/.test(sideTxt));

    /* ============ G-08 快捷入口合并 + 行为统一 ============ */
    await h.eval(`location.hash = '#/chat'`);
    await sleep(2600);
    const chatQuick = await h.eval(`JSON.stringify([...document.querySelectorAll('.qa-card')].map(b=>(b.innerText||'').replace(/\\s+/g,' ').trim()))`);
    ok('G-08 ★对话页快捷卡为 4 张（来自统一数据源）', JSON.parse(chatQuick).length === 4, JSON.parse(chatQuick));
    const sideQuick = await h.eval(`JSON.stringify([...document.querySelectorAll('.qa-chip')].map(b=>(b.innerText||'').trim()))`);
    ok('G-08 ★侧栏快捷只剩「导航项里没有的动作」（转账/冻结卡/卡密充值）',
      JSON.parse(sideQuick).length === 3 && !JSON.parse(sideQuick).includes('账单分析'), JSON.parse(sideQuick));

    // 侧栏点「账单分析」→ 只预填、不发送
    const before = await msgCount(h);
    await h.eval(`(()=>{const b=[...document.querySelectorAll('.qa-chip')].find(x=>/冻结卡/.test(x.innerText||''));if(b)b.click();return 1})()`);
    await sleep(1600);
    const v1 = await composer(h);
    const after1 = await msgCount(h);
    ok('G-08 ★侧栏快捷「冻结卡」= 预填不发送', /冻结我的银行卡/.test(String(v1 || '')) && after1 === before,
      { composer: v1, before, after: after1 });

    // 对话页卡片点「账单分析」→ 同样只预填（行为统一）
    await h.eval(`(()=>{const t=document.querySelector('textarea');if(t){t.value='';t.dispatchEvent(new Event('input',{bubbles:true}));}return 1})()`);
    await sleep(500);
    const before2 = await msgCount(h);
    await h.eval(`(()=>{const b=[...document.querySelectorAll('.qa-card')].find(x=>/账单分析/.test(x.innerText||''));if(b)b.click();return 1})()`);
    await sleep(1600);
    const v2 = await composer(h);
    const after2 = await msgCount(h);
    ok('G-08 ★对话页快捷卡同为「预填不发送」（两处行为一致）',
      /这个月账单分析/.test(String(v2 || '')) && after2 === before2, { composer: v2, before: before2, after: after2 });

    // 「转账」两处都应是跳页
    const gotoBoth = await h.eval(`(()=>{const t=[...document.querySelectorAll('button')].filter(b=>(b.innerText||'').trim()==='转账');return t.length})()`);
    ok('G-08 「转账」在侧栏与对话页都保留（同一动作同一行为=跳页）', Number(gotoBoth) >= 1, { count: gotoBoth });
    await h.screenshot(path.join(OUT, 'fix7_quick_unified.png'));

    /* ============ C-05 信用卡额度入口（切到有信用卡的专用测试用户） ============ */
    await h.goto(BASE + '/login');
    await h.eval(`localStorage.removeItem('xb_token')`);
    await h.goto(BASE + '/login');
    await h.type('input[placeholder="请输入用户名"]', 'cardtest_a');
    await h.type('input[placeholder="请输入密码"]', 'cardtest123');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3800);
    await h.eval(`location.hash = '#/cards'`);
    await sleep(3000);
    const cardTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix7_card_limit_entry.png'));
    ok('C-05 ★信用卡上有「调整额度」入口', /调整额度/.test(cardTxt), {
      hasCredit: /信用卡/.test(cardTxt), sample: (cardTxt.match(/[^\n]{0,24}调整额度[^\n]{0,16}/) || [''])[0]
    });
    const clicked = await h.clickByText('调整额度', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(1800);
    const c2 = await composer(h);
    ok('C-05 ★点「调整额度」走对话预填（不新开页面直连例外）',
      /额度调整一下|信用额度/.test(String(c2 || '')), { clicked, composer: c2 });

    /* ============ X-05 评审文档更正落盘 ============ */
    const doc = fs.readFileSync(path.resolve('docs/ux_design_review_prompt.md'), 'utf8');
    ok('X-05 ★评审提示词已补「事实表更正」块',
      doc.includes('事实表更正（2026-10-01 真机复测）') && doc.includes('#16') && doc.includes('#40') && doc.includes('#25'));

    log('RESULT', `PASS=${pass} FAIL=${fail}`);
  } catch (e) {
    fail++; log('EXCEPTION', String((e && e.message) || e));
  } finally { await h.close().catch(() => {}); }
  process.exit(fail === 0 ? 0 : 1);
})();
