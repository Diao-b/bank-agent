/* 批 3 第 3 组 · 真机验收：G-02b 弹窗焦点陷阱 · A-01b 形象页只读 · G-05 小字收口 · C-05 挂失入口
 *
 * 依赖：Edge 无头 CDP 9222（免沙箱启动）、主站 8178、Live2D 5173 可选。
 * 纪律：断言只来自页面可见文字 / DOM / 计算样式 / 真实按键；截图留证。
 * ★ 焦点必须用 CDP Input.dispatchKeyEvent 真按键 —— 在页面里 dispatchEvent
 *   合成 KeyboardEvent 不会触发浏览器默认行为（Tab 不放焦点），会假绿。
 */
const path = require('path');
const fs = require('fs');
const L = require('./_e2e_lib.js');

const BASE = 'http://127.0.0.1:8178';
const OUT = path.join(__dirname, '..', '_reports', 'ux_audit');
const LOG = path.join(__dirname, '..', '_reports', 'ux_fix_verify8.log');

let pass = 0, fail = 0;
const lines = [];
const ok = (name, cond, extra) => {
  const bad = !cond;
  if (bad) fail++; else pass++;
  const tail = extra === undefined ? '' : '  <= ' + JSON.stringify(extra);
  const line = `[${bad ? 'FAIL' : 'PASS'}] ${name}${tail}`;
  console.log(line); lines.push(line);
};
const log = (t, m) => { console.log(`  (${t}) ${m}`); lines.push(`  (${t}) ${m}`); };

(async () => {
  const h = await L.launch({ width: 1440, height: 920 });
  try {
    const sleep = L.sleep;

    const key = async (k, shift) => {
      const code = { Tab: 'Tab', Escape: 'Escape' }[k];
      const vk = { Tab: 9, Escape: 27 }[k];
      const p = { key: k, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers: shift ? 8 : 0 };
      await h.b.send('Input.dispatchKeyEvent', { ...p, type: 'rawKeyDown' }, h.sid);
      await h.b.send('Input.dispatchKeyEvent', { ...p, type: 'keyUp' }, h.sid);
      await sleep(120);
    };
    const activeInfo = () => h.eval(`(()=>{
      const a = document.activeElement;
      if (!a) return 'null';
      const scrim = a.closest('[data-modal-scrim]');
      return JSON.stringify({ tag: a.tagName, txt: (a.innerText||a.value||'').trim().slice(0,18),
        inModal: !!scrim, cls: (a.className||'').toString().slice(0,40) });
    })()`);
    const modalState = () => h.eval(`(()=>{
      const all = [...document.querySelectorAll('[data-modal-scrim]')].filter(e=>e.offsetParent||e.getClientRects().length);
      const bgHidden = [...document.querySelectorAll('[aria-hidden="true"]')].length;
      return JSON.stringify({ modals: all.length, bgHidden });
    })()`);

    const login = async (u, p) => {
      await h.goto(BASE + '/login');
      await sleep(900);
      await h.type('input[name="username"], input[placeholder*="账号"], input[type="text"]', u);
      await h.type('input[type="password"]', p);
      await h.eval(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/立即登录|登\\s*录/.test(x.innerText||''));if(b)b.click();return 1})()`);
      await sleep(3200);
    };

    /* ============ 登录 + 进卡片页 ============ */
    await login('user1', '123456');
    ok('前置：登录成功进入主站', /小微/.test((await h.bodyText()) || ''));

    await h.eval(`location.hash = '#/cards'`);
    await sleep(2600);

    /* ============ G-02b 焦点陷阱 ============ */
    const bgBase = JSON.parse(await modalState()).bgHidden;   // 基线：页面自带的装饰性 aria-hidden
    const opened = await h.eval(`(()=>{
      const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='注销');
      if(!b) return 'no-btn';
      b.scrollIntoView({block:'center'}); b.focus(); b.click(); return 'ok';
    })()`);
    await sleep(900);
    const st1 = JSON.parse(await modalState());
    ok('G-02b 打开注销确认弹窗（有 data-modal-scrim 且背景被标记 aria-hidden）',
      opened === 'ok' && st1.modals >= 1 && st1.bgHidden > 0, { opened, ...st1 });
    await h.screenshot(path.join(OUT, 'fix8_focus_in.png'));

    const in1 = JSON.parse(await activeInfo());
    ok('G-02b ★弹窗打开后焦点自动移入弹窗（不再停在背景按钮上）', in1.inModal === true, in1);

    // 连按 Tab 12 次：焦点必须始终留在弹窗内
    let escaped = 0; const trace = [];
    for (let i = 0; i < 12; i++) {
      await key('Tab', false);
      const a = JSON.parse(await activeInfo());
      trace.push(a.txt);
      if (!a.inModal) escaped++;
    }
    ok('G-02b ★连按 Tab 12 次，焦点始终锁在弹窗内（不跑到背景）', escaped === 0, { escaped, trace: trace.slice(0, 6) });

    // Shift+Tab 反向同样锁住
    let escapedBack = 0;
    for (let i = 0; i < 6; i++) {
      await key('Tab', true);
      const a = JSON.parse(await activeInfo());
      if (!a.inModal) escapedBack++;
    }
    ok('G-02b ★Shift+Tab 反向循环同样不逃逸', escapedBack === 0, { escapedBack });

    // ESC 关闭 → 焦点归还给触发按钮
    await key('Escape', false);
    await sleep(700);
    const st2 = JSON.parse(await modalState());
    const after = JSON.parse(await activeInfo());
    ok('G-02b ESC 关闭弹窗且背景 aria-hidden 已还原（回到基线）',
      st2.modals === 0 && st2.bgHidden === bgBase, { ...st2, bgBase });
    ok('G-02b ★关闭后焦点归还给打开它的按钮（键盘用户不迷路）',
      after.inModal === false && /注销/.test(after.txt || ''), after);
    await h.screenshot(path.join(OUT, 'fix8_focus_restored.png'));

    /* ============ C-05 挂失入口 ============ */
    const lostBtn = await h.eval(`(()=>{
      const b=[...document.querySelectorAll('button')].find(x=>(x.innerText||'').trim()==='挂失');
      if(!b) return 'no-btn';
      b.scrollIntoView({block:'center'}); b.click(); return 'ok';
    })()`);
    await sleep(2000);
    const pre = JSON.parse(await h.eval(`JSON.stringify({ hash: location.hash,
      input: (()=>{const t=document.querySelector('textarea'); return t?t.value:null})() })`));
    ok('C-05 ★卡片页有「挂失」入口，点击进入对话并预填挂失诉求',
      lostBtn === 'ok' && pre.hash.includes('/chat') && /挂失/.test(String(pre.input || '')), { lostBtn, ...pre });
    // 预填必须带卡名 + 尾号：挂失是"不可逆地停卡"，挂错卡比输错金额更严重
    ok('C-05 预填文案含卡名与尾号（避免挂错卡）',
      /（尾号\s?\d{4}）/.test(String(pre.input || '')) && /卡/.test(String(pre.input || '')), pre.input);
    // 清掉预填，避免误发
    await h.eval(`(()=>{const t=document.querySelector('textarea'); if(t){t.value='';t.dispatchEvent(new Event('input',{bubbles:true}));} return 1})()`);

    /* ============ A-01b 形象页只读 ============ */
    await h.eval(`location.hash = '#/appearance'`);
    await sleep(2600);
    const apTxt = (await h.bodyText()) || '';
    await h.screenshot(path.join(OUT, 'fix8_appearance_readonly.png'));
    const edit = JSON.parse(await h.eval(`JSON.stringify({
      selects: document.querySelectorAll('select').length,
      save: [...document.querySelectorAll('button,a')].filter(b=>/保存形象|上传新背景图/.test(b.innerText||'')).length,
      fileInputs: document.querySelectorAll('input[type=file]').length })`));
    ok('A-01b ★用户端不再有任何编辑控件（下拉 / 保存 / 上传全部移除）',
      edit.selects === 0 && edit.save === 0 && edit.fileInputs === 0, edit);
    ok('A-01b 展示「当前形象」只读信息（中文名，不含机读值）',
      /当前形象/.test(apTxt) && /(晴（Haru）|前台接待台背景|标准教室背景)/.test(apTxt) && !/nokia_reception_desk\.png/.test(apTxt),
      (apTxt.match(/当前形象[\s\S]{0,90}/) || [''])[0].replace(/\n/g, ' '));
    ok('A-01b 指明「全局配置 · 修改请到管理后台」', /管理后台/.test(apTxt) && /全局/.test(apTxt));
    ok('A-01b 页面不显示内网预览 URL', !/127\.0\.0\.1:5173/.test(apTxt));

    /* ============ G-05 小字收口（真机计算样式） ============ */
    const pages = [['/chat', '#/chat'], ['/bills', '#/bills'], ['/subs', '#/subs'],
                   ['/cards', '#/cards'], ['/wealth', '#/wealth'], ['/safe', '#/safe'],
                   ['/appearance', '#/appearance'], ['/transfer', '#/transfer']];
    const fontReport = [];
    for (const [name, hash] of pages) {
      await h.eval(`location.hash = '${hash}'`);
      await sleep(2200);
      const small = await h.eval(`(()=>{
        const out = [];
        document.querySelectorAll('body *').forEach(e => {
          if (!(e.offsetParent || e.getClientRects().length)) return;
          const own = [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
          if (!own) return;
          const fs = parseFloat(getComputedStyle(e).fontSize);
          if (fs < 12) out.push((e.innerText||'').trim().slice(0, 14) + '@' + fs);
        });
        return JSON.stringify({ n: out.length, sample: out.slice(0, 4) });
      })()`);
      const r = JSON.parse(small);
      fontReport.push(`${name}:${r.n}`);
      if (r.n > 0) log('SMALL', `${name} -> ${JSON.stringify(r.sample)}`);
    }
    const total = fontReport.map(s => Number(s.split(':')[1])).reduce((a, b) => a + b, 0);
    ok('G-05 ★8 个页面均无 <12px 的文本节点（收口前实测约 300 处）', total === 0, fontReport);

    /* ============ G-05 窄屏回归（字号变大后不溢出） ============ */
    await h.setViewport(746, 800);
    await h.eval(`location.hash = '#/cards'`); await sleep(1800);
    await h.eval(`location.hash = '#/chat'`); await sleep(1800);
    const narrow = JSON.parse(await h.eval(`JSON.stringify({
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth })`));
    ok('G-05 746px 窄屏无横向溢出（字号放大后仍不撑破）', narrow.overflow <= 1, narrow);
    await h.screenshot(path.join(OUT, 'fix8_narrow_after_font.png'));
    await h.setViewport(1440, 920);

  } catch (e) {
    ok('执行未抛异常', false, String(e && e.message || e));
    lines.push(String(e && e.stack || e));
  } finally {
    try { await h.close(); } catch (e) { /* ignore */ }
    fs.writeFileSync(LOG, lines.join('\n') + `\n\n结果：${pass}/${pass + fail} 通过\n`, 'utf8');
    console.log(`\n结果：${pass}/${pass + fail} 通过（日志 ${LOG}）`);
    process.exit(fail ? 1 : 0);
  }
})();
