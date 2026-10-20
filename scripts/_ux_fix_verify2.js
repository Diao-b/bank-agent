/* 批 2（P1 结构）· 真机验收脚本
 * 覆盖：S-04/S-05/S-08/S-09 订阅页分层与文案 · C-02/C-03/C-04 卡片页去重与危险操作 · B-02/B-03/N-02 账单页
 * 用法：node scripts/_ux_fix_verify2.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_fix_verify2.log'));
let pass = 0, fail = 0;
const ok = (n, c, e) => { if (c) { pass++; log('PASS', n + (e !== undefined ? '  ' + JSON.stringify(e) : '')); } else { fail++; log('FAIL', n + '  ' + JSON.stringify(e)); } };

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
    ok('前置：登录进入', /账户|小微/.test((await h.bodyText()) || ''));

    /* ================= 订阅页 ================= */
    await h.eval(`location.hash = '#/subs'`);
    await sleep(3200);
    let subTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix2_subs_grouped.png'));
    ok('S-04 ★不再出现「最后使用」（语义错），改为「上次扣款」', !/最后使用/.test(subTxt) && /上次扣款/.test(subTxt));
    ok('S-04 ★「闲置」判定口径改为距上次扣款天数', /距上次扣款超过 60 天/.test(subTxt), (subTxt.match(/「闲置」[^\n]*/) || [])[0]);
    const endedBtn = await h.eval(`JSON.stringify([...document.querySelectorAll('button')].map(b=>b.innerText.trim()).filter(t=>/已结束的订阅/.test(t)))`);
    ok('S-05 ★已结束订阅被折叠（不在主列表混排）', JSON.parse(endedBtn).length > 0, JSON.parse(endedBtn));
    const activeRows = await h.eval(`(()=>{const t=document.body.innerText;const m=t.match(/已取消/g)||[];return m.length})()`);
    ok('S-05 主列表不再直接渲染「已取消」行', activeRows === 0, { count: activeRows });
    // 排序：生效中按 next_due 升序
    const dueOrder = await h.eval(`(()=>{
      const t=document.body.innerText;
      const arr=[...t.matchAll(/(\\d{4}-\\d{2}-\\d{2}) 扣款/g)].map(m=>m[1]);
      return JSON.stringify(arr)} )()`);
    const dues = JSON.parse(dueOrder);
    const sorted = [...dues].sort();
    ok('S-08 ★生效中订阅按下次扣款时间升序', dues.length > 1 && JSON.stringify(dues) === JSON.stringify(sorted), { dues });
    // 展开已结束
    await h.clickByText('查看已结束的订阅', { tag: 'button', nth: 0 });
    await sleep(900);
    const afterExpand = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix2_subs_ended.png'));
    ok('S-05 展开后能看到已结束项，且给了「怎么恢复」的说明', /如需继续，请重新签约/.test(afterExpand));
    // 取消确认文案
    const cancelBtn = await h.clickByText('取消', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(1200);
    const cancelTxt = (await h.bodyText()) || '';
    ok('S-09 ★取消确认文案说清「不再扣款 + 已扣不退」', /不再产生新的扣款/.test(cancelTxt) && /已扣费用不退回/.test(cancelTxt), { cancelBtn });
    await h.screenshot(path.join(OUT, 'fix2_subs_cancelcopy.png'));
    await h.eval(`document.body.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}))`);
    await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='取消');b&&b.click();return 1})()`);
    await sleep(700);

    /* ================= 卡片页 ================= */
    await h.eval(`location.hash = '#/cards'`);
    await sleep(3000);
    const cardTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix2_cards.png'));
    ok('C-03 ★「卡片操作」重复区块已删除', !/卡片操作/.test(cardTxt));
    const masked = await h.eval(`JSON.stringify((document.body.innerText.match(/\\d{4} \\*{4} \\*{4} \\d{4}/g)||[]).length)`);
    const revealBtns = await h.eval(`JSON.stringify([...document.querySelectorAll('button')].filter(b=>/显示卡号|隐藏/.test(b.innerText||'')).length)`);
    ok('C-03 ★每张卡只出现一次（掩码数 = 卡片数 = 显示卡号按钮数）',
      Number(masked) === Number(revealBtns) && Number(masked) > 0, { masked: JSON.parse(masked), reveal: JSON.parse(revealBtns) });
    ok('C-04 状态胶囊按状态表达（使用中/已冻结/已挂失…）', /使用中|已冻结|已挂失/.test(cardTxt));
    ok('C-02 注销按钮独立在卡片操作行', /注销/.test(cardTxt));
    const del = await h.clickByText('注销', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(1100);
    const delTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix2_cards_delconfirm.png'));
    ok('C-02 ★点注销出现"输入卡号后 4 位"的独立确认', /请输入卡号后 4 位确认/.test(delTxt), { del });
    const disabled = await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/确认注销/.test(x.innerText||''));return b?b.disabled:null})()`);
    ok('C-02 未输满 4 位前「确认注销」不可点', disabled === true, { disabled });
    // 输错尾号 → 仍不可点
    await h.type('input[placeholder="4 位数字"]', '0000');
    await sleep(400);
    const disabled2 = await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/确认注销/.test(x.innerText||''));return b?b.disabled:null})()`);
    ok('C-02 尾号不匹配时仍不可点（防误点错卡）', disabled2 === true, { disabled2 });
    await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='取消');b&&b.click();return 1})()`);
    await sleep(700);

    /* ================= 账单页 ================= */
    await h.eval(`location.hash = '#/bills'`);
    await sleep(3200);
    const billTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix2_bills.png'));
    ok('N-02 ★手续费类提醒不再说"请确认是否重复扣费"',
      !/手续费本月被扣费[^\n]*请确认是否重复扣费/.test(billTxt));
    ok('N-02 手续费提醒给出澄清（属于转账本身的费用）',
      /转账本身产生的费用|与转账本金不是两笔扣款/.test(billTxt) || !/手续费/.test(billTxt),
      (billTxt.match(/[^\n]{0,20}手续费[^\n]{0,60}/) || [])[0]);
    // 隐私模式下金额按设计必须不可读；要验的是「金额走统一组件」：占位与全站一致（¥ ··· ··），
    // 不再是异常提醒独有的 ¥--（B-04 的两套占位问题），且随顶栏隐私开关联动。
    ok('B-03/B-04 ★异常提醒金额与全站同一脱敏占位（不再是 ¥--）',
      !/¥--/.test(billTxt) && /合计[\s\S]{0,40}¥ ··· ··/.test(billTxt),
      { hasOldPlaceholder: /¥--/.test(billTxt) });
    await h.eval(`(()=>{const b=[...document.querySelectorAll('header button')].find(x=>/隐私/.test(x.innerText||''));b&&b.click();return 1})()`);
    await sleep(1600);
    const billOpen = (await h.bodyText()) || '';
    const hasReveal = /合计[\s\S]{0,60}显示/.test(billOpen);
    await h.clickByText('显示', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(900);
    const billRevealed = (await h.bodyText()) || '';
    ok('B-03 ★关掉隐私后提醒内可「点显示」就地读到金额', hasReveal && /合计[\s\S]{0,60}¥\s?\d/.test(billRevealed),
      { hasReveal, sample: (billRevealed.match(/合计[\s\S]{0,30}/) || [''])[0] });
    await h.eval(`(()=>{const b=[...document.querySelectorAll('header button')].find(x=>/隐私/.test(x.innerText||''));b&&b.click();return 1})()`);
    await sleep(1200);

    /* B-02：只断上月请求（m1），看环比卡是否不再显示 ¥0.00 */
    await h.b.send('Network.setBlockedURLs', { urls: ['*/api/transactions?start=2026-09*'] }, h.sid);
    await h.eval(`location.hash = '#/chat'`); await sleep(600);
    await h.eval(`location.hash = '#/bills'`);
    await sleep(3200);
    const b2 = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix2_bills_prevfail.png'));
    ok('B-02 ★上月数据失败时不显示「上月支出 ¥0.00」', !/上月支出\s*¥\s?0\.00/.test(b2));
    ok('B-02 上月失败时给出「暂不可用」', /上月数据暂不可用/.test(b2), (b2.match(/[^\n]{0,30}暂不可用[^\n]{0,20}/) || [])[0]);
    await h.b.send('Network.setBlockedURLs', { urls: [] }, h.sid);

    log('RESULT', `PASS=${pass} FAIL=${fail}`);
  } catch (e) {
    fail++; log('EXCEPTION', String((e && e.message) || e));
  } finally { await h.close().catch(() => {}); }
  process.exit(fail === 0 ? 0 : 1);
})();
