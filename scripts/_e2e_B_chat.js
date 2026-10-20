/* B 对话实测：真发消息 → 等回复 → 抓页面渲染（气泡文本 / 图表 / 确认卡） */
const { launch, BASE, sleep, makeLogger } = require('./_e2e_lib');
const SHOTS = 'D:/Users/Dwm/Desktop/agaent/bank-agent/.shots/ui-e2e';
const log = makeLogger('D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_e2e_results/B_chat.log');

async function send(h, text) {
  await h.eval(`(() => { const el=document.querySelector('textarea'); if(!el) return false;
    const s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
    s.call(el, ${JSON.stringify(text)}); el.dispatchEvent(new Event('input',{bubbles:true})); return true; })()`);
  await sleep(350);
  const clicked = await h.eval(`(() => { const b=[...document.querySelectorAll('button')].find(x => (x.innerText||'').replace(/\\s/g,'')==='发送'); if(b){ b.click(); return true; } return false; })()`);
  return clicked;
}
async function waitReply(h, timeout = 60000) {
  const t0 = Date.now(); let saw = false;
  while (Date.now() - t0 < timeout) {
    const t = await h.bodyText();
    if (/小微正在输入/.test(t)) saw = true;
    else if (saw) return { done: true, sawTyping: true, ms: Date.now() - t0 };
    await sleep(600);
  }
  return { done: false, sawTyping: saw, ms: Date.now() - t0 };
}
// 抓所有消息气泡（含图表/确认卡等富块）
async function dumpTurn(h) {
  return await h.eval(`(() => {
    const main = document.querySelector('main') || document.body;
    const all = [...main.querySelectorAll('div')];
    // 消息气泡
    const bubbles = all.filter(d => typeof d.className==='string' && /rounded-2xl/.test(d.className)
      && (d.innerText||'').trim().length > 3 && (d.innerText||'').trim().length < 2000);
    // 去重：只保留最内层（无其它 bubble 子节点）
    const leaf = bubbles.filter(d => !bubbles.some(o => o!==d && d.contains(o)));
    const texts = leaf.map(d => (d.innerText||'').replace(/\\s+/g,' ').trim()).filter(Boolean);
    const hasSvg = main.querySelectorAll('svg').length;
    const hasCanvas = main.querySelectorAll('canvas').length;
    const confirmBtns = [...main.querySelectorAll('button')].map(b=>(b.innerText||'').trim()).filter(t=>/确认|取消|同意|授权|提交/.test(t));
    return JSON.stringify({ n: texts.length, last: texts[texts.length-1]||'', texts: texts.slice(-4), svg: hasSvg, canvas: hasCanvas, confirmBtns });
  })()`);
}

(async () => {
  const h = await launch();
  const TOK = process.argv[2];
  await h.goto(BASE + '/chat', { reload: false }); await h.setToken(TOK);
  await h.goto(BASE + '/chat#/chat', { reload: false }); await sleep(3500);
  log('start_real', /已接入真实账户/.test(await h.bodyText()));

  const MSGS = [
    { tag: 'normal',   text: '你好', shot: 'B01-normal' },
    { tag: 'balance',  text: '查一下我的余额', shot: 'B02-balance' },
    { tag: 'bills',    text: '这个月钱都花哪了', shot: 'B03-bills' },
    { tag: 'transfer', text: '给张三转 100 块', shot: 'B04-transfer-confirm' },
    { tag: 'compound', text: '把我的闲置订阅都取消掉，省下来的钱帮我买理财', shot: 'B05-compound-task' },
  ];
  for (const m of MSGS) {
    const ok = await send(h, m.text);
    const t0 = Date.now();
    const rep = await waitReply(h, 65000);
    const turn = await dumpTurn(h);
    log('msg:' + m.tag, JSON.stringify({ sent: ok, text: m.text, reply: rep, elapsedMs: Date.now()-t0, turn: JSON.parse(turn) }));
    await h.screenshot(SHOTS + '/' + m.shot + '.png');
    await sleep(500);
  }
  // 连续发 5 条 + 超长文本
  await h.eval(`(() => { const el=document.querySelector('textarea'); const s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set; s.call(el,'测试'+'很长'.repeat(400)); el.dispatchEvent(new Event('input',{bubbles:true})); return true; })()`);
  await sleep(400);
  const longShot = await h.eval(`(() => { const el=document.querySelector('textarea'); return JSON.stringify({ overflowX: el.scrollWidth-el.clientWidth, wrap: getComputedStyle(el).whiteSpace }); })()`);
  log('longtext_input', longShot);
  await h.screenshot(SHOTS + '/B06-longtext.png');

  clearTimeout(h._wd);
  await h.close();
  log('DONE', 'ok');
})().catch(e => { console.error('FATAL', e); process.exit(1); });
