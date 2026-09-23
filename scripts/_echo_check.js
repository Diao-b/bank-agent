/* 从 web/index.html 抽出真实源码（vdLcsLen / vdLooksLikeEcho）验证自听判定，
   —— 不手抄逻辑，测的就是页面里实际跑的那份代码。
   用法：node scripts/_echo_check.js */
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

/* 从 `const NAME = (...) => {` 起做花括号配对，取出完整函数体 */
function extract(name) {
  const start = src.indexOf('const ' + name + ' = (');
  if (start < 0) throw new Error('not found: ' + name);
  const open = src.indexOf('{', start);
  let depth = 0, i = open;
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}

const lcsSrc = extract('vdLcsLen').replace(/^const vdLcsLen = /, '');
const vdLcsLen = new Function('return ' + lcsSrc + ';')();

const echoBody = extract('vdLooksLikeEcho')
  .replace(/^const vdLooksLikeEcho = \(text\) => \{/, '')
  .replace(/\}$/, '');            // 去掉函数体最后一个 }（尾随分号不在抽取范围内）
const vdLooksLikeEcho = new Function(
  'text', 'vdLcsLen', 'vdListenAtRef', 'vdSpokenPoolRef', 'Date', echoBody
);

const strip = (s) => String(s || '').replace(/[\s\p{P}\p{S}]/gu, '');

function run(text, pool, msSinceMicOpen) {
  const vdListenAtRef = { current: Date.now() - msSinceMicOpen };
  const vdSpokenPoolRef = { current: pool.map(strip) };
  return vdLooksLikeEcho(text, vdLcsLen, vdListenAtRef, vdSpokenPoolRef, Date);
}

/* 旧逻辑（对照）：整句包含 or 字符命中率 >= 92% */
function oldLogic(text, pool, msSinceMicOpen) {
  const a = strip(text);
  if (a.length < 6) return false;
  if (msSinceMicOpen > 15000) return false;
  const b = strip(pool[0] || '');
  if (!b) return false;
  if (b.includes(a)) return true;
  const setB = new Set(b);
  let hit = 0;
  for (const ch of a) if (setB.has(ch)) hit++;
  return hit / a.length >= 0.92;
}

/* 真实漏网案例：播报「收款人簿中无…请先通过新增收款人完成账户验证后转账」被麦克风录回 */
const SPOKEN_TRANSFER = '收款人簿中无「李雷」账户，请先通过「新增收款人」完成账户验证后转账。请补充：收款人。';
const ECHO_REAL = '漂流记不到哪里就有东西，请先通过新增收款人完成账户验证后转账。';

console.log('=== 真实漏网案例（用户会话里实际发生的那一幕）===');
console.log('  新逻辑判定为自听:', run(ECHO_REAL, [SPOKEN_TRANSFER], 3000));
console.log('  旧逻辑判定为自听:', oldLogic(ECHO_REAL, [SPOKEN_TRANSFER], 3000), '  <-- 漏的就是它');
console.log('  LCS 长度:', vdLcsLen(strip(ECHO_REAL), strip(SPOKEN_TRANSFER)));

console.log('\n=== 真实用户说话（必须不被过滤）===');
const replyText = '刁先生，本月（2026-09-01 ~ 09-20）您的支出情况如下：总支出 ¥4256.67，共 11 笔。转账，¥2969.34，69.76%。购物，¥887.86，20.86%。';
let bad = 0;
for (const u of ['这个月钱都花哪了', '查一下我的余额', '你好呀', '帮我转账2000给妈妈', '取消', '我有几张银行卡', '冻结我的银行卡']) {
  const r = run(u, [replyText, SPOKEN_TRANSFER], 3000);
  if (r) bad++;
  console.log('  ' + (r ? 'FAIL(被误杀)' : 'OK') + '  ' + u);
}

console.log('\n=== ASR 错字变体（应判为自听）===');
for (const v of [
  '漂流记不到哪里就有东西请先通过新增收款人完成账户验证后转账',
  '请先通过新增收款人完成账户验证后转账',
  '新增收款人完成账户验证后转账',
  '请先通过新增收款人完成账户验证',
]) {
  console.log('  ' + (run(v, [SPOKEN_TRANSFER], 3000) ? 'OK' : 'FAIL') + '  ' + v);
}

console.log('\n=== 超出时间窗（25s 后说话不算自听）===');
console.log('  30s 后:', run(ECHO_REAL, [SPOKEN_TRANSFER], 30000), '(期望 false，此时用户是真在说话)');

console.log('\n结论:', bad === 0 ? '用户真实语句 0 误杀' : `${bad} 条真实语句被误杀`);
