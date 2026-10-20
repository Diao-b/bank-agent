/* 全站 UX 评审 · 取证脚本 4：精准验证 4 个可疑点（真机）
 *  1) 隐私模式下是否仍有明文金额（#24 RollingAmount）
 *  2) 卡片「显示卡号」是否有自动回遮（#43）
 *  3) 主按钮配色跨页对比：签约代扣(青) vs 申请新卡(?) vs 确认卡(绿)（#46/#49）
 *  4) 形象设置页 普通用户的保存态与机读名暴露（#57）
 */
const fs = require('fs');
const path = require('path');
const H = require('./_e2e_lib.js');
const sleep = H.sleep;
const BASE = 'http://127.0.0.1:8178';
const OUT = path.resolve('_reports/ux_audit');
const log = H.makeLogger(path.join(OUT, '_run4.log'));

const PLAIN_MONEY = `(() => {
  const out = [];
  document.querySelectorAll('*').forEach(e => {
    if (e.children.length) return;
    const t = (e.innerText || '').trim();
    if (!t) return;
    if (/¥\\s?[0-9][0-9,\\.]*/.test(t) && !/·/.test(t)) out.push({ t: t.slice(0,40), cls: (e.className||'').toString().slice(0,50), fs: getComputedStyle(e).fontSize });
  });
  return JSON.stringify(out.slice(0, 20));
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

    /* 1) 隐私模式下的明文金额扫描 */
    const privacy = await h.eval(`(()=>{const b=[...document.querySelectorAll('header button')].find(x=>/隐私/.test(x.innerText||''));return b?b.innerText.trim():'none'})()`);
    log('PRIVACY', '顶栏隐私按钮文案 = ' + privacy);
    for (const [name, hash] of [['chat', '#/chat'], ['bills', '#/bills'], ['subs', '#/subs'], ['cards', '#/cards'], ['wealth', '#/wealth']]) {
      await h.eval(`location.hash = ${JSON.stringify(hash)}`);
      await sleep(2400);
      const plain = JSON.parse(await h.eval(PLAIN_MONEY));
      log('PLAIN_MONEY', name + ' 明文金额 ' + plain.length + ' 处 ' + JSON.stringify(plain.slice(0, 6)));
    }

    /* 2) 卡片「显示卡号」是否自动回遮 */
    await h.eval(`location.hash = '#/cards'`);
    await sleep(2600);
    const before = await h.eval(`(()=>{const m=document.body.innerText.match(/6\\d{3}[\\s*]*\\d{4}/g)||[];return JSON.stringify(m.slice(0,4))})()`);
    const c = await h.clickByText('显示卡号', { tag: 'button', nth: 0, visibleOnly: true });
    await sleep(1200);
    const shown = await h.eval(`(()=>{const m=document.body.innerText.match(/\\d{4}\\s?\\d{4}\\s?\\d{4}\\s?\\d{4}|\\d{16,19}/g)||[];return JSON.stringify(m.slice(0,4))})()`);
    await h.screenshot(path.join(OUT, 'cards_cardon.png'));
    await sleep(12000);
    const after12 = await h.eval(`(()=>{const m=document.body.innerText.match(/\\d{4}\\s?\\d{4}\\s?\\d{4}\\s?\\d{4}|\\d{16,19}/g)||[];return JSON.stringify(m.slice(0,4))})()`);
    log('CARDNO', '点前=' + before + ' 点后=' + shown + ' +12s=' + after12 + ' click=' + c);
    await h.screenshot(path.join(OUT, 'cards_cardon_12s.png'));

    /* 3) 开卡主按钮配色 */
    const apply = await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/申请新卡|立即申请|申请银行卡/.test(x.innerText||''));if(!b)return 'none';const cs=getComputedStyle(b);return JSON.stringify({t:b.innerText.trim(),color:cs.color,bg:cs.backgroundColor,bgImg:cs.backgroundImage.slice(0,60),fs:cs.fontSize})})()`);
    log('BTN', '开卡主按钮 ' + apply);
    const allMain = await h.eval(`(()=>{const bs=[...document.querySelectorAll('button,[role=button]')].filter(b=>{const cs=getComputedStyle(b);const bg=cs.backgroundColor+'|'+cs.backgroundImage;return /74, 236, 212|212, 160, 23|2FD07A|16A34A|6, 148, 162|cyan/.test(bg)}).slice(0,10).map(b=>{const cs=getComputedStyle(b);return {t:(b.innerText||'').trim().replace(/\\s+/g,' ').slice(0,14),bg:cs.backgroundColor,img:(cs.backgroundImage||'').slice(0,45)}});return JSON.stringify(bs)})()`);
    log('BTN', 'cards 页主色按钮 ' + allMain);

    /* 4) 形象设置页 */
    await h.eval(`location.hash = '#/appearance'`);
    await sleep(2600);
    await h.screenshot(path.join(OUT, 'appearance_1440b.png'));
    const ax = await h.eval(`(()=>{const btns=[...document.querySelectorAll('button')].map(b=>({t:(b.innerText||'').trim().slice(0,14),dis:b.disabled}));const sels=[...document.querySelectorAll('select')].map(s=>({dis:s.disabled,opts:[...s.options].map(o=>o.value).slice(0,6)}));return JSON.stringify({btns:btns.slice(0,10),sels,text:(document.body.innerText||'').slice(0,400)})})()`);
    log('APPEARANCE', ax);

    /* 5) 对话页输入区快捷卡（两套入口） */
    await h.eval(`location.hash = '#/chat'`);
    await sleep(2400);
    const qa = await h.eval(`(()=>{const bs=[...document.querySelectorAll('button')].filter(b=>/查余额|账单分析|智能转账|闲置订阅/.test(b.innerText||'')).map(b=>({t:(b.innerText||'').replace(/\\s+/g,' ').trim().slice(0,16)}));return JSON.stringify(bs)})()`);
    log('QUICK', '对话页快捷卡 ' + qa);

    log('DONE', 'ok4');
  } catch (e) {
    log('EXCEPTION', String((e && e.message) || e));
  } finally {
    await h.close().catch(() => {});
  }
  process.exit(0);
})();
