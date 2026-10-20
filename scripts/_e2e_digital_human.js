/* 验证数字人链路：打开 Live2D 渲染端(5173) → 是否连上 Fay WS(10002) → 状态 digital_human 是否 True
 * 只读 + 截图，不改任何数据。
 */
const H = require('./_e2e_lib.js');
const fs = require('fs');
const LOG = '_reports/_e2e_digital_human.log';
const log = (...a) => { try { fs.appendFileSync(LOG, a.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ') + '\n'); } catch (e) {} };

async function status() {
  const r = await fetch('http://127.0.0.1:5000/api/get-system-status?username=User');
  return await r.json();
}

(async () => {
  try { fs.writeFileSync(LOG, ''); } catch (e) {}
  const h = await H.launch({ width: 1280, height: 800, watchdogMs: 120000 });
  try {
    log('BEFORE=' + JSON.stringify(await status()));
    await h.goto('http://127.0.0.1:5173/');
    await H.sleep(8000);

    const dom = JSON.parse(await h.eval(`JSON.stringify((() => ({
      url: location.href,
      title: document.title,
      canvases: document.querySelectorAll('canvas').length,
      canvasSize: [...document.querySelectorAll('canvas')].map(c => c.width + 'x' + c.height),
      bodyLen: (document.body.innerText || '').trim().length,
      bodyHead: (document.body.innerText || '').trim().slice(0, 120)
    }))())`));
    log('DOM=' + JSON.stringify(dom));
    log('AFTER_LOAD=' + JSON.stringify(await status()));

    await H.sleep(6000);
    log('AFTER_6S=' + JSON.stringify(await status()));
    await h.screenshot('_reports/_e2e_digital_human.png');
    log('SHOT=_reports/_e2e_digital_human.png');
  } catch (e) {
    log('ERR ' + (e && e.stack || e));
  } finally {
    try { await h.close(); } catch (e) {}
    log('AFTER_CLOSE=' + JSON.stringify(await status()));
    process.exit(0);
  }
})();
