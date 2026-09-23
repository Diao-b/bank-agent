/**
 * 自听过滤器验收 —— 直接跑 web/index.html 里的**真实源码**（不是复制一份来测）。
 *
 * 背景：ASR 会把数字人的回声啃成半鬼话，例如
 *   播报原文：…我可以帮您打开理财页面或订阅**管理页面**，**您自己操作**。要我帮您打开吗？
 *   识别结果：**管理页面**不是**您自己操作**以蜘蛛那个电电电
 *   → 连续命中被鬼话截断，最长公共子串只有 5 字，卡在旧阈值（≥12 / ≥6且占60%）之下，漏网。
 *
 * 做法：从 index.html 中按花括号配平抠出 vdLcsLen / vdLooksLikeEcho 源码并求值，
 *      这样测的就是线上真的在跑的那段逻辑，改了源码这里会立刻反映。
 *
 * 用法：node scripts/test_echo_filter.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const HTML = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');

let PASS = 0, FAIL = 0;
function record(name, ok, extra) {
  if (ok) { PASS++; console.log(`  PASS  ${name}${extra ? '   ' + extra : ''}`); }
  else { FAIL++; console.log(`  FAIL  ${name}${extra ? '   ' + extra : ''}`); }
}

/** 从 startIdx 起按花括号配平截取一段 `const name = ... ;` 源码 */
function extractConst(name) {
  const marker = `const ${name} =`;
  const i = HTML.indexOf(marker);
  if (i < 0) throw new Error(`未找到 ${name}`);
  const braceStart = HTML.indexOf('{', i);
  if (braceStart < 0) throw new Error(`${name} 不是块级函数`);
  let depth = 0, j = braceStart;
  for (; j < HTML.length; j++) {
    const c = HTML[j];
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) { j++; break; } }
  }
  return HTML.slice(i, j) + ';';
}

function buildFilter() {
  const srcLcs = extractConst('vdLcsLen');
  const srcEcho = extractConst('vdLooksLikeEcho');
  // 两个函数都在同一作用域，vdLooksLikeEcho 内部直接调 vdLcsLen
  const factory = new Function(
    'vdEchoUntilRef', 'vdSpokenPoolRef',
    `${srcLcs}\n${srcEcho}\nreturn { vdLcsLen, vdLooksLikeEcho };`
  );
  return factory;
}

/* 播报原文（done.payload.reply，与前端同样的归一化：去掉空白/标点/符号） */
const SPOKEN = [
  '理财产品需要您本人在确认卡里核对金额验证码后再执行不能让我代替您确认'
  + '取消订阅同样需要您本人在订阅管理里确认'
  + '如果您想现在就开始我可以帮您打开理财页面或订阅管理页面您自己操作要我帮您打开吗',
];

function makeFilter({ untilAheadMs = 25000, pool = SPOKEN } = {}) {
  const until = { current: Date.now() + untilAheadMs };
  const poolRef = { current: pool };
  return buildFilter()(until, poolRef);
}

console.log('='.repeat(68));
console.log('自听过滤器验收（跑 index.html 真实源码）');
console.log('='.repeat(68));

// ---------------- 1) 真实漏网案例 ----------------
console.log('\n[1] ★ 实测漏网案例：回声被 ASR 啃成半鬼话');
{
  const { vdLooksLikeEcho, vdLcsLen } = makeFilter();
  const a = '管理页面不是您自己操作以蜘蛛那个电电电';
  const lcs = vdLcsLen(a, SPOKEN[0]);
  console.log(`       识别结果 a = ${a}`);
  console.log(`       最长公共子串 = ${lcs}（旧阈值 ≥12 / ≥6且占60% 都够不到）`);
  record('捕获该回声（判为自听 → 丢弃）', vdLooksLikeEcho(a) === true, `lcs=${lcs}`);
}

// 完整回声
{
  const { vdLooksLikeEcho } = makeFilter();
  record('完整回声（整句命中）', vdLooksLikeEcho('我可以帮您打开理财页面或订阅管理页面') === true);
}

// ---------------- 2) 正常用户的话必须放行（不能误伤） ----------------
console.log('\n[2] ★ 正常用户的话必须放行（防误伤）');
{
  const { vdLooksLikeEcho } = makeFilter();
  const ok = [
    '帮我查一下这个月的流水',
    '我要转两千块钱给李雷',
    '好的那就这样吧',
    '我自己来操作就行',           // vs 播报「您自己操作」→ 连续命中仅「自己操作」4 字
    '不用了谢谢',
    '帮我看看理财产品有哪些',
    '订阅管理页面在哪儿',         // 与播报撞词，但连续命中不足 5 字/占比不足
  ];
  for (const t of ok) {
    record(`放行：「${t}」`, vdLooksLikeEcho(t) === false);
  }
}

// ---------------- 3) 边界与闸门 ----------------
console.log('\n[3] 边界与闸门');
{
  const { vdLooksLikeEcho } = makeFilter();
  record('太短（<6 字）不做判定', vdLooksLikeEcho('打开吗') === false);

  // 自听时间窗外 → 一律不判为回声
  const expired = makeFilter({ untilAheadMs: -1000 });
  record('超出自听时间窗 → 不判为回声（防误伤旧会话）',
    expired.vdLooksLikeEcho('我可以帮您打开理财页面或订阅管理页面') === false);

  // 播报池为空 → 无从比对
  const noPool = makeFilter({ pool: [] });
  record('播报池为空 → 不判为回声', noPool.vdLooksLikeEcho('管理页面您自己操作') === false);

  // 多条播报逐一比对（最近 3 条）
  const multi = makeFilter({
    pool: ['第一段无关内容', '完全不同的另一段话', SPOKEN[0]],
  });
  record('与最近播报之一命中即捕获（不只比对最后一条）',
    multi.vdLooksLikeEcho('管理页面不是您自己操作以蜘蛛那个电电电') === true);
}

// ---------------- 4) 前端硬门禁与接线（静态断言） ----------------
console.log('\n[4] ★ 硬门禁：以「Fay 还在出声」为准（比文本相似度可靠）');
{
  const checks = [
    ['播报结束时刻按音频帧实时后推', 'vdAudioEndAtRef.current = vdAudioStartRef.current + vdAudioTotalRef.current;'],
    ['★ 还在出声时识别结果一律丢弃', 'Date.now() < vdAudioEndAtRef.current + VD_AUDIO_TAIL_MS'],
    ['余音尾巴窗口已定义', 'const VD_AUDIO_TAIL_MS = 500;'],
    ['打断后计时清零（等新帧重新起算）', 'vdAudioEndAtRef.current = 0;'],
    ['★ 半鬼话兜底档（≥5字且占25%）', 'lcs >= 5 && lcs / a.length >= 0.25'],
  ];
  for (const [name, needle] of checks) {
    record(name, HTML.includes(needle), HTML.includes(needle) ? '' : `缺少：${needle}`);
  }
}

console.log('\n' + '='.repeat(68));
console.log(`结果：PASS ${PASS} / ${PASS + FAIL}` + (FAIL === 0 ? '   ALL GREEN' : '   有失败项'));
console.log('='.repeat(68));
process.exit(FAIL === 0 ? 0 : 1);
