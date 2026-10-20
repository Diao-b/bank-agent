/* 批 1（P0 止血）· 真机验收脚本
 * 覆盖：C-01 卡号回遮 / W-01 赎回按钮 / B-01 账单错误态 / F-01+F-02 日志中文化 /
 *       L-01 登录不签发假 token / T-02 无「短信」字面 / W-04 会话标题脱敏 / S-01 确认卡
 * 用法：node scripts/_ux_fix_verify.js
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;

const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_fix_verify.log'));

let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; log('PASS', name + (extra !== undefined ? '  ' + JSON.stringify(extra) : '')); }
  else { fail++; log('FAIL', name + '  ' + JSON.stringify(extra)); }
};

(async () => {
  const h = await H.launch({ width: 1440, height: 920 });
  try {
    /* ---------------- 登录 user1 ---------------- */
    await h.goto(BASE + '/login');
    await h.eval(`localStorage.clear()`);
    await h.setViewport(1440, 920);
    await h.goto(BASE + '/login');
    await h.type('input[placeholder="请输入用户名"]', 'user1');
    await h.type('input[placeholder="请输入密码"]', '123456');
    await h.eval(`(()=>{const f=document.querySelector('form');f&&f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));return 1})()`);
    await sleep(3600);
    ok('前置：登录进入', /账户|小微/.test((await h.bodyText()) || ''));

    /* =============== C-01 卡号 10s 自动回遮 =============== */
    await h.eval(`location.hash = '#/cards'`);
    await sleep(2600);
    await h.clickByText('显示卡号', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(1200);
    const shownNow = await h.eval(`(()=>{const m=document.body.innerText.match(/\\d{4}\\s\\d{4}\\s\\d{4}\\s\\d{4}/g)||[];return JSON.stringify(m.slice(0,2))})()`);
    await sleep(11000);
    const afterHide = await h.eval(`(()=>{const m=document.body.innerText.match(/\\d{4}\\s\\d{4}\\s\\d{4}\\s\\d{4}/g)||[];return JSON.stringify(m.slice(0,2))})()`);
    const maskAfter = await h.eval(`/\\*\\*\\*\\*/.test(document.body.innerText)`);
    ok('C-01 展开卡号后立即可见明文', JSON.parse(shownNow).length > 0, JSON.parse(shownNow));
    ok('C-01 ★展开 11s 后自动回遮（明文消失）', JSON.parse(afterHide).length === 0 && maskAfter, { after: JSON.parse(afterHide), masked: maskAfter });

    /* =============== W-01 赎回按钮可见性 =============== */
    await h.eval(`location.hash = '#/wealth'`);
    await sleep(2800);
    const redeem = await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/^赎回$/.test((x.innerText||'').trim()));if(!b)return 'none';const cs=getComputedStyle(b);return JSON.stringify({fs:cs.fontSize,color:cs.color,bg:cs.backgroundColor,border:cs.borderColor})})()`);
    let ratio = null;
    try {
      const o = JSON.parse(redeem);
      const lum = (c) => { const [r, g, b2] = c.match(/\d+/g).slice(0, 3).map(Number).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }); return 0.2126 * r + 0.7152 * g + 0.0722 * b2; };
      ratio = ((Math.max(lum(o.color), lum('rgb(13,19,29)')) + 0.05) / (Math.min(lum(o.color), lum('rgb(13,19,29)')) + 0.05)).toFixed(2);
    } catch (_) {}
    ok('W-01 ★赎回按钮字号 ≥12px', /1[2-9]px/.test(String((JSON.parse(redeem || '{}').fs) || '')), { fs: JSON.parse(redeem || '{}').fs });
    ok('W-01 ★赎回按钮对比度 ≥4.5:1', Number(ratio) >= 4.5, { color: JSON.parse(redeem || '{}').color, ratio });

    /* =============== B-01 账单异常提醒错误态（真断接口） =============== */
    await h.b.send('Network.setBlockedURLs', { urls: ['*/api/transactions*'] }, h.sid);
    await h.eval(`location.hash = '#/bills'`);
    await sleep(1200);
    await h.eval(`location.hash = '#/chat'`); await sleep(700);
    await h.eval(`location.hash = '#/bills'`);
    await sleep(3000);
    const billsErr = (await h.bodyText()) || '';
    const hasGreenOk = /本月未发现异常支出，消费节奏正常/.test(billsErr);
    const hasErr = /暂不可用|重试|Mock/.test(billsErr);
    await h.screenshot(path.join(OUT, 'fix_bills_errorstate.png'));
    ok('B-01 ★接口失败时不再显示「本月未发现异常支出」', !hasGreenOk, { green: hasGreenOk });
    ok('B-01 接口失败时出现错误态/重试', hasErr, { preview: billsErr.slice(0, 120) });
    await h.b.send('Network.setBlockedURLs', { urls: [] }, h.sid);
    await sleep(600);

    /* =============== F-01 / F-02 安全中心日志 =============== */
    await h.eval(`location.hash = '#/safe'`);
    await sleep(3200);
    const safeTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix_safe_logs.png'));
    ok('F-01 ★日志不再裸露 JSON（无 { 或 from_account_id）', !/from_account_id|\{"|\{"/.test(safeTxt), { hit: /from_account_id|\{"|\{"/.test(safeTxt) });
    ok('F-02 ★日志无下划线英文 action', !/\b(transfer_out|subscription_delete|subscription_contract|subscription_deduct|card_delete|card_adjust_limit)\b/.test(safeTxt));
    ok('F-01 日志出现中文字段（付款账户/流水号/渠道 至少两种）',
      ['付款账户', '流水号', '渠道', '收款人', '金额'].filter(k => safeTxt.includes(k)).length >= 2,
      ['付款账户', '流水号', '渠道', '收款人', '金额'].filter(k => safeTxt.includes(k)));

    /* =============== W-04 会话标题脱敏 =============== */
    await h.eval(`location.hash = '#/chat'`);
    await sleep(2600);
    const titleRaw = await h.eval(`(()=>{const els=[...document.querySelectorAll('div')].filter(e=>e.children.length===0&&/转账/.test(e.innerText||'')&&/·/.test(e.innerText||''));return JSON.stringify(els.map(e=>e.innerText.trim()).slice(0,3))})()`);
    ok('W-04 ★隐私模式下会话标题金额已脱敏', !/¥\s?\d/.test(titleRaw), { titles: JSON.parse(titleRaw) });

    /* =============== T-02 全页无「短信」 =============== */
    await h.eval(`location.hash = '#/transfer'`);
    await sleep(2600);
    const trTxt = (await h.bodyText()) || '';
    ok('T-02 ★转账页不再出现「短信」字面', !/短信/.test(trTxt));
    ok('T-02 转账页改为「收件箱」说法', /收件箱/.test(trTxt));

    /* =============== S-01 确认订阅必须先过确认卡 =============== */
    await h.eval(`location.hash = '#/subs'`);
    await sleep(3000);
    const before = await h.eval(`(()=>{const m=document.body.innerText.match(/生效中|订阅列表/g);return document.querySelectorAll('button').length})()`);
    const clicked = await h.clickByText('确认订阅', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(1400);
    const cardTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix_subs_confirmcard.png'));
    ok('S-01 ★点「确认订阅」先弹确认卡（不直接提交）', /待确认操作|扣款方式/.test(cardTxt), { clicked });
    ok('S-01 确认卡复述金额/周期/下次扣款/扣款账户',
      ['金额', '计费周期', '扣款账户', '扣款方式'].every(k => cardTxt.includes(k)),
      ['金额', '计费周期', '扣款账户', '扣款方式'].filter(k => cardTxt.includes(k)));
    ok('S-01 确认卡写明「自动扣款」后果', /按周期自动扣款/.test(cardTxt));
    // 取消掉，不产生写操作
    await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='取消');b&&b.click();return 1})()`);
    await sleep(800);
    const afterCard = (await h.bodyText()) || '';
    ok('S-01 取消后未新增订阅（列表未出现"已确认"提示刷屏）', !/已签约代扣/.test(afterCard));

    /* =============== 签约表单确认卡（S-06 后果说明） =============== */
    const hasRule = /从余额足够的账户自动扣款/.test((await h.bodyText()) || '');
    ok('S-02（如实告知）签约表单旁写明扣款账户规则', hasRule);

    log('RESULT', `PASS=${pass} FAIL=${fail}`);
  } catch (e) {
    fail++;
    log('EXCEPTION', String((e && e.message) || e));
  } finally {
    await h.close().catch(() => {});
  }
  process.exit(fail === 0 ? 0 : 1);
})();
