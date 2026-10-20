const fs = require('fs');
const D = 'D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_dbg.log';
fs.writeFileSync(D, '');
const L = (s) => fs.appendFileSync(D, s + '\n');
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const wsUrlBase = 'http://127.0.0.1:9222';

L('start');
(async () => {
  const v = await (await fetch(wsUrlBase + '/json/version')).json();
  L('version ' + v.webSocketDebuggerUrl);
  const ws = new WebSocket(v.webSocketDebuggerUrl);
  let id = 0; const pending = new Map();
  const ready = new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', e => rej(e.error||new Error('err'))); });
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const {resolve,reject}=pending.get(m.id); pending.delete(m.id); m.error?reject(new Error(JSON.stringify(m.error))):resolve(m.result); }
    else L('EVENT ' + m.method);
  });
  const send = (method, params={}) => new Promise((resolve,reject)=>{ const mid=++id; pending.set(mid,{resolve,reject}); ws.send(JSON.stringify({id:mid,method,params})); });
  await ready; L('wsReady');
  const {targetId} = await send('Target.createTarget', { url:'about:blank' }); L('createTarget ' + targetId);
  const {sessionId} = await send('Target.attachToTarget', { targetId, flatten:true }); L('attach ' + sessionId);
  await send('Page.enable', {}, sessionId); L('Page.enable');
  await send('Runtime.enable', {}, sessionId); L('Runtime.enable');
  await send('Network.enable', {}, sessionId); L('Network.enable');
  await send('Network.setCacheDisabled', {cacheDisabled:true}, sessionId); L('setCacheDisabled');
  L('DONE_LAUNCH');
  process.exit(0);
})().catch(e => { L('CATCH ' + (e&&e.stack||e)); process.exit(1); });
