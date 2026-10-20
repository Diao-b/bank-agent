/* 用户级状态隔离 · 前端探针
   —— 从 web/index.html / web/login.html 中原样抽取分桶 helper 代码，
      在 mock localStorage 上跑真实场景，输出 JSON 供 Python 断言。
      目的：验证「换号不串功能态」「退出重进后回到干净默认态」。
*/
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

/** 造一个最小 localStorage（含 length / key(i)，供 xbClearUser 遍历） */
function makeLS() {
  const m = new Map();
  const o = {
    getItem: (k) => (m.has(String(k)) ? m.get(String(k)) : null),
    setItem: (k, v) => { m.set(String(k), String(v)); },
    removeItem: (k) => { m.delete(String(k)); },
    clear: () => m.clear(),
    key: (i) => Array.from(m.keys())[i],
    _dump: () => Array.from(m.keys()).sort(),
  };
  Object.defineProperty(o, 'length', { get: () => m.size });
  return o;
}

/** 从源码中截取一段 JS 并注入 mock localStorage 求值 */
function loadFn(src, startMarker, endMarker, exportNames) {
  const i = src.indexOf(startMarker);
  if (i < 0) throw new Error('marker not found: ' + startMarker);
  const j = src.indexOf(endMarker, i);
  if (j < 0) throw new Error('end marker not found: ' + endMarker);
  const code = src.slice(i, j);
  const fn = new Function('localStorage', code + '\nreturn {' + exportNames.join(',') + '};');
  return (ls) => fn(ls);
}

const indexSrc = read('web/index.html');
const loginSrc = read('web/login.html');

// index.html：helper 段 = `_xbUser` 定义 → `const PRIVACY_KEY` 之前（内含 xbGet/xbSet/xbDel/xbClearUser）
const mkIndex = loadFn(indexSrc, 'const _xbUser', 'const PRIVACY_KEY',
  ['_xbUser', 'xbGet', 'xbSet', 'xbDel', 'xbClearUser']);
// login.html：helper 段 = `_xbUser` 定义 → `const go = (role)` 之前（内含 xbClearOldUser）
const mkLogin = loadFn(loginSrc, 'const _xbUser', 'const go = (role)',
  ['_xbUser', 'xbGet', 'xbSet', 'xbDel', 'xbClearUser', 'xbClearOldUser']);

const R = {};
const idx = mkIndex(makeLS());           // 应用内（index.html）
const lg = mkLogin(makeLS());            // 登录页（login.html）

/* ---------- 场景 1：应用内 —— 用户A 开数字人 + 建会话 ---------- */
const ls1 = makeLS();
ls1.setItem('xb_user', 'alice');
const a = mkIndex(ls1);
a.xbSet('digital_human', '1');
a.xbSet('voice_dialog', '1');
a.xbSet('dh_mute', '1');
a.xbSet('thread', 'web-abc123');
a.xbSet('privacy', 'off');
R.s1_keysAfterA = ls1._dump();
R.s1_writtenToGlobal = ls1._dump().filter((k) => !/^xb:u:/.test(k));   // 除身份键外不应有全局键

/* ---------- 场景 2：换号到 B（未走退出，直接开登录页换号）---------- */
ls1.setItem('xb_user', 'bob');
R.s2_bobSeesDigitalHuman = a.xbGet('digital_human');    // 期望 null（关键：不得串号）
R.s2_bobSeesVoiceDialog = a.xbGet('voice_dialog');
R.s2_bobSeesMute = a.xbGet('dh_mute');
R.s2_bobSeesThread = a.xbGet('thread');                 // 期望 null（换号拿到全新会话）
R.s2_bobSeesPrivacy = a.xbGet('privacy');               // 期望 null（隐私回到「开」）
R.s2_keysContainBob = ls1._dump().some((k) => k === 'xb:u:bob:digital_human');

/* ---------- 场景 3：B 自己开数字人 → 只写进 B 的分桶 ---------- */
a.xbSet('digital_human', '0');   // B 主动关掉
R.s3_bobKey = ls1._dump().filter((k) => k === 'xb:u:bob:digital_human');
R.s3_aliceBucketIntact = ls1._dump().some((k) => k.indexOf('xb:u:alice:') === 0);

/* ---------- 场景 4：用户A 走退出登录（应用内 logout）---------- */
ls1.setItem('xb_user', 'alice');
a.xbClearUser();
R.s4_afterLogout = ls1._dump();                          // alice 分桶应被清空，xb_user 保留
R.s4_dhAfterReenter = a.xbGet('digital_human');          // 期望 null → 重进后数字人默认关
R.s4_threadAfterReenter = a.xbGet('thread');             // 期望 null → 重进开新会话
R.s4_userKept = ls1.getItem('xb_user');                  // 期望 'alice'（身份信息不应被清）

/* ---------- 场景 5：登录页换号清理（覆盖「直接开登录页换号」路径）---------- */
const ls2 = makeLS();
ls2.setItem('xb_user', 'carol');
const c = mkLogin(ls2);
c.xbSet('digital_human', '1');
c.xbSet('thread', 'web-carol');
c.xbClearOldUser();
R.s5_afterLoginPurge = ls2._dump();                      // carol 分桶应被清、xb_user 保留
const c2 = mkLogin(ls2);
ls2.setItem('xb_user', 'dave');
R.s5_daveSeesDH = c2.xbGet('digital_human');             // 期望 null
R.s5_daveSeesThread = c2.xbGet('thread');                // 期望 null

console.log(JSON.stringify(R, null, 2));
