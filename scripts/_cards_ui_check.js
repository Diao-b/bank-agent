/* 卡片管理页（/chat#/cards）UI 深度检测：
   1) 注入登录态 → 打开 #/cards
   2) DOM 探针：卡片网格几何 / 每张卡的实际尺寸 / 溢出检测 / 文本内容 / 冻结灰度滤镜
   3) 两种视口宽度（1440 桌面 / 1092 窄窗，复刻用户截图）各截一张
   4) 点「注销」→ 采集弹层 DOM 与截图（复刻用户截图状态）
   用法：node scripts/_cards_ui_check.js <token> [outDir]
*/
const fs = require('fs');
const path = require('path');

const ARGV = process.argv.slice(2).filter(a => !a.startsWith('--'));
const TOK = ARGV[0];
const OUT = ARGV[1] || path.join(__dirname, '..', '_reports', 'cards-ui');
const VIEW_ARG = (process.argv.find(a => a.startsWith('--view=')) || '').slice(7);
const MODAL = process.argv.includes('--modal');
const BASE = 'http://127.0.0.1:8178';
const CDP = 'http://127.0.0.1:9222';

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function pickPageTarget() {
  for (let i = 0; i < 30; i++) {
    try {
      const list = await (await fetch(CDP + '/json/list')).json();
      const p = list.filter(t => t.type === 'page').pop();
      if (p && p.webSocketDebuggerUrl) return p;
    } catch (_) { /* retry */ }
    await sleep(500);
  }
  throw new Error('no CDP page target');
}

function cdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    }
  });
  const ready = new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', rej);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
  return { ready, send, close: () => ws.close() };
}

/* 页面侧探针：把卡片页几何/溢出/文本一次性扒出来 */
const PROBE = `(() => {
  const R = (el) => { const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; };
  const cs = (el) => getComputedStyle(el);

  // 卡片本体：暗色渐变卡（class 含 rounded-2xl 与 text-white）
  const cards = [...document.querySelectorAll('div')].filter(e =>
    typeof e.className === 'string' && e.className.includes('rounded-2xl')
    && e.className.includes('text-white') && e.className.includes('p-5'));

  const cardInfo = cards.map(c => {
    const rect = R(c);
    return {
      rect,
      text: (c.innerText || '').replace(/\\n+/g, ' | ').slice(0, 220),
      frozen: /grayscale/.test(c.className),
      bgLen: (cs(c).backgroundImage || '').length,
      bgHead: (cs(c).backgroundImage || '').slice(0, 70),
      filter: cs(c).filter,
      overflowX: c.scrollWidth - c.clientWidth,
      overflowY: c.scrollHeight - c.clientHeight,
      // 卡内出现横向溢出的子元素（被裁掉的元凶）
      innerOverflow: [...c.querySelectorAll('*')].filter(k => k.scrollWidth - k.clientWidth > 2)
        .slice(0, 5).map(k => ({ tag: k.tagName, cls: String(k.className).slice(0, 60),
                                 over: k.scrollWidth - k.clientWidth, text: (k.innerText || '').slice(0, 40) })),
    };
  });

  const grid = cards.length ? cards[0].parentElement : null;
  const gridInfo = grid ? {
    rect: R(grid),
    cls: String(grid.className).slice(0, 120),
    cols: cs(grid).gridTemplateColumns,
    gap: cs(grid).gap,
    childCount: grid.children.length,
    childRects: [...grid.children].map(R),
  } : null;

  // 页面级横向溢出：找出右边界超出 <main> 可视宽的元素
  const scrollHost = document.querySelector('main') || document.documentElement;
  const hostRect = R(scrollHost);
  const spill = [...document.querySelectorAll('*')].map(e => ({ e, r: e.getBoundingClientRect() }))
    .filter(o => o.r.width > 0 && o.r.height > 0 && o.r.right > hostRect.x + hostRect.w + 2)
    .slice(0, 8)
    .map(o => ({ tag: o.e.tagName, cls: String(o.e.className).slice(0, 70),
                 right: Math.round(o.r.right), text: (o.e.innerText || '').replace(/\\s+/g, ' ').slice(0, 50) }));

  return JSON.stringify({
    route: location.hash,
    viewport: { w: innerWidth, h: innerHeight },
    docScrollW: document.documentElement.scrollWidth,
    docClientW: document.documentElement.clientWidth,
    horizontalScroll: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    cardCount: cards.length,
    cards: cardInfo,
    grid: gridInfo,
    spill,
    // 顶部统计条 / 申请区 / 操作区是否存在
    hasStatRow: !!document.body.innerText.match(/持有卡片/),
    hasApply: !!document.body.innerText.match(/申请新卡/),
    hasOps: !!document.body.innerText.match(/卡片操作/),
    bodySnip: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 400),
  });
})()`;

const CLICK_DELETE = `(() => {
  const btns = [...document.querySelectorAll('button')].filter(b => b.textContent.trim() === '注销');
  if (!btns.length) return JSON.stringify({ found: 0 });
  btns[0].scrollIntoView({ block: 'center' });
  btns[0].click();
  return JSON.stringify({ found: btns.length, clicked: 0 });
})()`;

const PROBE_MODAL = `(() => {
  const overlay = [...document.querySelectorAll('div')].filter(d =>
    typeof d.className === 'string' && d.className.includes('fixed inset-0') && d.className.includes('z-[85]'));
  if (!overlay.length) return JSON.stringify({ open: false });
  const panel = overlay[0].querySelector('div[class*="w-[560px]"]') || overlay[0].firstElementChild;
  const inner = [...panel.querySelectorAll('*')];
  const bad = inner.filter(e => e.scrollWidth - e.clientWidth > 2 || e.scrollHeight - e.clientHeight > 2)
    .slice(0, 6).map(e => ({ tag: e.tagName, cls: String(e.className).slice(0, 70),
                             overX: e.scrollWidth - e.clientWidth, overY: e.scrollHeight - e.clientHeight,
                             text: (e.innerText || '').replace(/\\s+/g, ' ').slice(0, 60) }));
  const r = panel.getBoundingClientRect();
  const btns = [...panel.querySelectorAll('button')].map(b => ({ text: b.textContent.trim(), ...(() => {
    const q = b.getBoundingClientRect();
    return { x: Math.round(q.x), y: Math.round(q.y), w: Math.round(q.width), h: Math.round(q.height) };
  })() }));
  return JSON.stringify({
    open: true,
    panel: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    panelFilter: getComputedStyle(panel).backdropFilter,
    title: panel.querySelector('span') ? panel.querySelector('span').textContent : null,
    text: (panel.innerText || '').replace(/\\n+/g, ' | ').slice(0, 400),
    innerOverflow: bad,
    buttons: btns,
  });
})()`;

(async () => {
  if (!TOK) throw new Error('missing token');
  fs.mkdirSync(OUT, { recursive: true });
  const target = await pickPageTarget();
  const { ready, send, close } = cdp(target.webSocketDebuggerUrl);
  await ready;
  await send('Page.enable');
  await send('Runtime.enable');
  // ★ 必须禁用缓存 + 用 Page.reload(ignoreCache) 真刷新：
  //   Page.navigate 到「相同 URL + 相同 hash」是 no-op（浏览器视为同文档导航），
  //   会在第二个视口复用上一个视口的旧 DOM（曾因此把已修好的页面看成旧版）。
  await send('Network.enable');
  await send('Network.setCacheDisabled', { cacheDisabled: true });

  // ★ 必须先落到站点源（about:blank 上写 localStorage 不属于 127.0.0.1:8178，登录态不会生效）
  await send('Page.navigate', { url: BASE + '/' });
  await sleep(3000);
  await send('Runtime.evaluate', {
    expression: `localStorage.setItem('xb_token', ${JSON.stringify(TOK)});
                 localStorage.setItem('xb_user', 'user1'); 'seeded'`,
  });

  const VIEWS = [
    { name: 'desktop-1440', w: 1440, h: 1000 },
    { name: 'narrow-1092', w: 1092, h: 620 },   // 复刻用户截图窗口
  ];
  if (VIEW_ARG) {
    const [w, h] = VIEW_ARG.split('x').map(Number);
    VIEWS.length = 0;
    VIEWS.push({ name: `view-${w}x${h}`, w, h });
  }

  for (const v of VIEWS) {
    await send('Emulation.setDeviceMetricsOverride', {
      width: v.w, height: v.h, deviceScaleFactor: 2, mobile: false,
    });
    await send('Page.navigate', { url: BASE + '/chat#/cards' });
    await sleep(1200);
    await send('Page.reload', { ignoreCache: true });   // 真刷新，杜绝跨视口复用旧 DOM
    await sleep(6000);

    const probe = JSON.parse((await send('Runtime.evaluate', { expression: PROBE, returnByValue: true })).result.value);
    console.log('\n================ ' + v.name + ' ================');
    console.log('[viewport]', JSON.stringify(probe.viewport), 'docScrollW=', probe.docScrollW, 'hScroll=', probe.horizontalScroll);
    console.log('[layout] 卡片数=', probe.cardCount, ' 统计条=', probe.hasStatRow, ' 申请区=', probe.hasApply, ' 操作区=', probe.hasOps);
    console.log('[grid]', JSON.stringify(probe.grid && { rect: probe.grid.rect, cols: probe.grid.cols, gap: probe.grid.gap, childCount: probe.grid.childCount }));
    probe.cards.forEach((c, i) => {
      console.log(`  card#${i} rect=${JSON.stringify(c.rect)} frozen=${c.frozen} bgLen=${c.bgLen} filter=${c.filter} overX=${c.overflowX} overY=${c.overflowY}`);
      console.log(`     text: ${c.text}`);
      if (c.innerOverflow.length) console.log('     innerOverflow:', JSON.stringify(c.innerOverflow));
    });
    if (probe.spill.length) console.log('[spill 超出内容区]', JSON.stringify(probe.spill, null, 1));
    else console.log('[spill] 无');

    const shot = await send('Page.captureScreenshot', { format: 'png' });
    const p = path.join(OUT, `cards-${v.name}.png`);
    fs.writeFileSync(p, Buffer.from(shot.data, 'base64'));
    console.log('[shot]', p, fs.statSync(p).size, 'bytes');

    // 只有第一个视口下点开注销弹层（复刻用户截图状态）
    if (MODAL) {
      const cl = await send('Runtime.evaluate', { expression: CLICK_DELETE, returnByValue: true });
      console.log('[click 注销]', cl.result.value);
      await sleep(1800);
      const m = JSON.parse((await send('Runtime.evaluate', { expression: PROBE_MODAL, returnByValue: true })).result.value);
      console.log('[modal]', JSON.stringify(m, null, 1));
      const shot2 = await send('Page.captureScreenshot', { format: 'png' });
      const p2 = path.join(OUT, `cards-${v.name}-modal.png`);
      fs.writeFileSync(p2, Buffer.from(shot2.data, 'base64'));
      console.log('[shot modal]', p2, fs.statSync(p2).size, 'bytes');
      // 关掉弹层，避免影响后续
      await send('Runtime.evaluate', {
        expression: `(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='取消'); if(b){b.click();return 'closed';} return 'no-cancel'; })()`,
        returnByValue: true,
      });
    }
  }

  close();
})().catch(e => { console.error('ERROR', e.message); process.exit(1); });
