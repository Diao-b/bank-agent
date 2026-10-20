/* B 结构探测：对话页 DOM（输入框/发送按钮/消息容器/侧栏统计/操作日志） */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/B_struct.log');

(async () => {
  const h = await launch();
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false });
  await h.setToken(TOK);
  await h.goto(BASE + '/chat', { reload: false });
  await h.setToken(TOK);
  await h.goto(BASE + '/chat#/chat', { reload: false });
  await sleep(3000);

  const info = await h.eval(`(() => {
    const ta = document.querySelector('textarea');
    const btns = [...document.querySelectorAll('button')].map((b,i) => ({i, t:(b.innerText||'').replace(/\\n/g,'|').slice(0,30)})).filter(b=>b.t);
    // 发送按钮：文字去掉空白 === 发送
    const sendBtn = btns.find(b => b.t.replace(/\\s/g,'') === '发送');
    // 消息容器：找出包含轮次消息的滚动区
    const main = document.querySelector('main') || document.body;
    // 收集"消息气泡"候选：内部有大段文字且带 rounded 的元素
    const bubbles = [...main.querySelectorAll('div')].filter(d => typeof d.className==='string'
      && /rounded-2xl|rounded-xl/.test(d.className) && (d.innerText||'').length > 8 && (d.innerText||'').length < 600);
    const sample = bubbles.slice(0,6).map(d => ({ cls: String(d.className).slice(0,80), text: (d.innerText||'').replace(/\\s+/g,' ').slice(0,90) }));
    return JSON.stringify({
      hash: location.hash,
      realMode: /已接入真实账户/.test(document.body.innerText||''),
      textareaPh: ta ? ta.placeholder : null,
      sendBtn,
      hasQuick: /查余额|账单分析|智能转账|闲置订阅/.test(document.body.innerText||''),
      bubbleCount: bubbles.length,
      sample,
    });
  })()`);
  log('chat_struct', info);

  // 侧栏统计数字（已知缺陷#4 关注）
  const stats = await h.eval(`(() => {
    const t = document.body.innerText || '';
    const grab = (re) => { const m = t.match(re); return m ? m[0] : null; };
    return JSON.stringify({
      tailCard: grab(/储蓄卡\\s*[·.]?\\s*尾号\\s*\\d+/),
      todaySpend: grab(/今日支出[^\\n]{0,20}/),
      monthBalance: grab(/本月结余[^\\n]{0,20}/),
      chenxiaomin: /陈晓敏/.test(t),
      tail3344: /3344/.test(t),
      sidebarHead: t.replace(/\\s+/g,' ').slice(0, 240),
    });
  })()`);
  log('sidebar_stats', stats);

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
