const fs = require('fs');
const D = 'D:/Users/Dwm/Desktop/agaent/bank-agent/scripts/_dbg2.log';
fs.writeFileSync(D, '');
const L = (s) => fs.appendFileSync(D, s + '\n');
const { launch, makeLogger } = require('./_e2e_lib');
(async () => {
  L('calling launch');
  const h = await launch();
  L('launched ok');
  await h.goto('http://127.0.0.1:8178/chat', { reload: false });
  L('goto done');
  const t = await h.bodyText();
  L('bodyText len=' + (t ? t.length : 0));
  await h.close();
  L('closed');
  clearTimeout(h._wd);
  process.exit(0);
})().catch(e => { L('CATCH ' + (e && e.stack || e)); process.exit(1); });
