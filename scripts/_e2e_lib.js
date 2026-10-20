/* E2E 真浏览器驱动底座（chrome-devtools / Edge CDP）
 * 连浏览器级端点 → 新建隔离标签页 → flatten 会话 → 路由所有命令。
 * 用法：const H = require('./_e2e_lib.js'); (async()=>{ const h = await H.launch(); ... })();
 */
const fs = require('fs');
const path = require('path');

const CDP = 'http://127.0.0.1:9222';
const BASE = 'http://127.0.0.1:8178';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ---- 浏览器级连接 ----
async function browserWs() {
  for (let i = 0; i < 30; i++) {
    try {
      const v = await (await fetch(CDP + '/json/version')).json();
      if (v && v.webSocketDebuggerUrl) return v.webSocketDebuggerUrl;
    } catch (_) {}
    await sleep(400);
  }
  throw new Error('no CDP browser endpoint');
}

function wsConn(wsUrl, onEvent) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const conn = { onEvent: onEvent || (() => {}) };
  const ready = new Promise((res, rej) => {
    ws.addEventListener('open', res);
    ws.addEventListener('error', (e) => rej(e.error || new Error('ws error')));
  });
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    } else if (!msg.id && conn.onEvent) {
      conn.onEvent(msg.method, msg.params);
    }
  });
  const send = (method, params = {}, sessionId = null) => new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    const payload = { id: mid, method, params };
    if (sessionId) payload.sessionId = sessionId;
    ws.send(JSON.stringify(payload));
  });
  conn.ready = ready;
  conn.send = send;
  conn.close = () => ws.close();
  return conn;
}

class Harness {
  constructor(browser, sessionId) {
    this.b = browser; this.sid = sessionId;
    this.dialogs = [];
  }
  async init() {
    await this.b.send('Page.enable', {}, this.sid);
    await this.b.send('Runtime.enable', {}, this.sid);
    await this.b.send('Network.enable', {}, this.sid);
    await this.b.send('Network.setCacheDisabled', { cacheDisabled: true }, this.sid);
    // 弹窗自动处理：原生 dialog 会阻塞 Runtime.evaluate，必须自动响应。
    // alert→dismiss；confirm/beforeunload→默认接受（让"确认"类操作能继续），并记录供断言。
    this.b.onEvent = (method, params) => {
      if (method === 'Runtime.javascriptDialogOpening') {
        const type = params.type;
        this.dialogs.push({ type, message: params.message });
        const accept = (type === 'alert') ? false : true;
        this.b.send('Page.handleJavaScriptDialog', { accept }, this.sid).catch(() => {});
      }
    };
    return this;
  }
  async eval(js) {
    const r = await this.b.send('Runtime.evaluate', {
      expression: js, returnByValue: true, awaitPromise: true,
    }, this.sid);
    if (r.exceptionDetails) throw new Error('eval exception: ' + JSON.stringify(r.exceptionDetails));
    return r.result.value;
  }
  // 传 url → 导航到该 url（缓存已全局禁用，必然拉到最新）；不传 url + reload → 原地硬刷新
  async goto(url, { reload = false } = {}) {
    if (url) {
      await this.b.send('Page.navigate', { url }, this.sid);
      if (reload) { await sleep(1200); await this.b.send('Page.reload', { ignoreCache: true }, this.sid); }
    } else if (reload) {
      await this.b.send('Page.reload', { ignoreCache: true }, this.sid);
    }
    await sleep(3500);
  }
  async setToken(tok) {
    await this.eval(`localStorage.setItem('xb_token', ${JSON.stringify(tok)})`);
  }
  async clearToken() {
    await this.eval(`localStorage.removeItem('xb_token')`);
  }
  async getToken() { return await this.eval(`localStorage.getItem('xb_token')`); }
  // 切换到 SPA 某 hash 路由（等价于点侧栏）
  async nav(hash) {
    const h = hash.startsWith('#') ? hash : ('#' + hash);
    await this.eval(`location.hash = ${JSON.stringify(h)}`);
    await sleep(1800);
  }
  async setViewport(w, h) {
    await this.b.send('Emulation.setDeviceMetricsOverride', {
      width: w, height: h, deviceScaleFactor: 1, mobile: false,
    }, this.sid);
    await sleep(600);
  }
  async exists(sel) { return await this.eval(`!!document.querySelector(${JSON.stringify(sel)})`); }
  async count(sel) { return await this.eval(`document.querySelectorAll(${JSON.stringify(sel)}).length`); }
  // 在容器内按文字找可点击项（React onClick 元素常是 div/button）
  async clickIn(containerSel, text, { exact = false, nth = 0 } = {}) {
    const js = `(() => {
      const c = document.querySelector(${JSON.stringify(containerSel)});
      if (!c) return JSON.stringify({found:0, why:'no container'});
      const els = [...c.querySelectorAll('button,a,[role=button],div,span,li')];
      const pred = ${exact ? `s => s.trim() === ${JSON.stringify(text)}` : `s => s.includes(${JSON.stringify(text)})`};
      const hits = els.filter(e => pred((e.innerText||e.textContent||'').trim()) && e.offsetParent !== null);
      if (!hits.length) return JSON.stringify({found:0});
      const t = hits[${nth}];
      t.scrollIntoView({block:'center'}); t.click();
      return JSON.stringify({found: hits.length, clicked: (t.innerText||t.textContent||'').trim().slice(0,60)});
    })()`;
    return await this.eval(js);
  }
  async bodyText() { return await this.eval(`(document.body && document.body.innerText) || ''`); }
  async text(sel) {
    return await this.eval(`(()=>{const e=document.querySelector(${JSON.stringify(sel)});return e?(e.innerText||e.textContent||''):null})()`);
  }
  async waitForText(text, timeout = 15000, poll = 300) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const t = await this.bodyText();
      if (t && t.includes(text)) return true;
      await sleep(poll);
    }
    return false;
  }
  async clickByText(text, { tag = '*', exact = false, nth = 0, visibleOnly = false } = {}) {
    const js = `(() => {
      const SKIP = /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|HEAD|TITLE|META)$/;
      const els = [...document.querySelectorAll(${JSON.stringify(tag)})].filter(e => !SKIP.test(e.tagName));
      const pred = ${exact
        ? `s => s.trim() === ${JSON.stringify(text)}`
        : `s => s.includes(${JSON.stringify(text)})`};
      let found = els.filter(e => pred((e.innerText || '').trim()));
      if (${visibleOnly}) found = found.filter(e => e.offsetParent !== null);
      if (!found.length) return JSON.stringify({found:0});
      const t = found[${nth}];
      t.scrollIntoView({block:'center'});
      t.click();
      return JSON.stringify({found: found.length, tag: t.tagName, clicked: (t.innerText||'').trim().slice(0,60)});
    })()`;
    return await this.eval(js);
  }
  async clickBySelector(sel, nth = 0) {
    const js = `(() => {
      const els = [...document.querySelectorAll(${JSON.stringify(sel)})];
      if (!els.length) return JSON.stringify({found:0});
      const t = els[${nth}];
      t.scrollIntoView({block:'center'});
      t.click();
      return JSON.stringify({found: els.length, clicked: (t.innerText||t.textContent||'').trim().slice(0,60)});
    })()`;
    return await this.eval(js);
  }
  async type(sel, text, { tag = 'input' } = {}) {
    const js = `(() => {
      const el = document.querySelector(${JSON.stringify(sel)});
      if (!el) return JSON.stringify({found:false});
      const proto = (el.tagName === 'TEXTAREA')
        ? window.HTMLTextAreaElement.prototype
        : (el.tagName === 'INPUT' ? window.HTMLInputElement.prototype : null);
      el.focus();
      if (proto) {
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(el, ${JSON.stringify(text)});
      } else { el.value = ${JSON.stringify(text)}; }
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      return JSON.stringify({found:true});
    })()`;
    return await this.eval(js);
  }
  async screenshot(file, { full = false } = {}) {
    const dir = path.dirname(file);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    const r = await this.b.send('Page.captureScreenshot', {
      format: 'png', captureBeyondViewport: full,
    }, this.sid);
    fs.writeFileSync(file, Buffer.from(r.data, 'base64'));
    return file;
  }
  async close() {
    try { await this.b.send('Target.closeTarget', { targetId: this.targetId }, this.sid); } catch (_) {}
  }
}

async function launch({ width = 1440, height = 920, watchdogMs = 150000 } = {}) {
  const wsUrl = await browserWs();
  const b = wsConn(wsUrl, () => {});
  await b.ready;
  const { targetId } = await b.send('Target.createTarget', {
    url: 'about:blank',
  });
  const { sessionId } = await b.send('Target.attachToTarget', { targetId, flatten: true });
  const h = new Harness(b, sessionId);
  h.targetId = targetId;
  await h.init();
  /* ★ 视口铁律：Target.createTarget 不传尺寸时新标签页视口是 746x483（不是 1440！），
     参数 width/height 此前被静默忽略 —— 结果所有"1440 布局"结论其实都来自 746 窄屏。
     必须显式 Emulation.setDeviceMetricsOverride。 */
  await h.setViewport(width, height);
  // 看门狗：防止某步 await 永久挂起（如未处理的弹窗/网络死等）
  h._wd = setTimeout(() => {
    console.error('WATCHDOG_TIMEOUT @' + Date.now());
    try { fs.appendFileSync(h._logf || '/tmp/e2e_wd.log', 'WATCHDOG_TIMEOUT\n'); } catch (_) {}
    process.exit(2);
  }, watchdogMs);
  return h;
}

// 结果落文件（绕过管道块缓冲），同时打印
function makeLogger(file) {
  try {
    const dir = require('path').dirname(file);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(file, '');
  } catch (_) {}
  return (k, v) => {
    const line = k + '\t' + (typeof v === 'string' ? v : JSON.stringify(v));
    try { fs.appendFileSync(file, line + '\n'); } catch (_) {}
    console.log(line);
  };
}

module.exports = { launch, BASE, sleep, makeLogger };
