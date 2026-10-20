/* scripts/test_nav_intent_fe.js —— 前端「零延迟导航」行为测试（真跑函数，不是查字符串）

背景（2026-10-03，用户反馈）：
  「我只要提交和某个页面有关的词语，就会打开那个页面」——
  例如只说「我的数据安全。」（陈述/咨询）也会被跳到安全中心。
  根因在前端 `detectNavIntent()` 的第二条判定：「**只要 ≤10 字且命中话题词就跳**」，
  以及第一条里过于宽泛的动词表（含 看/查/显示/帮我/我想/我要/到）。
  ★ 之前的 test_nav_intent.py 只做**静态字符串检查**（"前端仅在 nav.auto 时 navigate()"），
    查不到这条与 nav.auto 无关的**独立跳转路径** —— 这就是"没彻底解决"的原因。

本脚本把前端那份实现**原样抽出来真跑**（new Function），用正/负语料双向验证。
不改任何文件、不起服务、无副作用。

用法：node scripts/test_nav_intent_fe.js
*/
const fs = require('fs');
const path = require('path');

const HTML = path.join(__dirname, '..', 'web', 'index.html');
const src = fs.readFileSync(HTML, 'utf8');

const START = src.indexOf('const NAV_LEAD_RE');
const END = src.indexOf('/* 侧边栏快捷操作的数据源已合并');
if (START < 0 || END < 0 || END <= START) {
  console.error('无法从 index.html 抽出 NAV_LEAD_RE / NAV_INTENTS / detectNavIntent（代码块边界变了？）');
  process.exit(2);
}
const block = src.slice(START, END);
let detectNavIntent;
try {
  detectNavIntent = new Function(block + '\nreturn detectNavIntent;')();
} catch (e) {
  console.error('前端导航判定块无法求值：' + e.message);
  process.exit(2);
}

/* ---------- 语料 ---------- */
// 必须【不跳】—— 陈述 / 咨询 / 询问 / 非导航祈使
const NEGATIVE = [
  ['我的数据安全。', '用户原话（本次反馈的触发句）'],
  ['我的数据安全', '同上，无句号'],
  ['数据安全', '纯话题'],
  ['安全中心是干嘛的？', '在问功能'],
  ['我想了解下安全中心', '了解 = 咨询'],
  ['这个月花了多少', '咨询'],
  ['可以查看我最近的流水账单吗?', '历史事故原句'],
  ['能看看我的订阅吗', '问"能不能"'],
  ['我的卡片有哪些', '列举式提问'],
  ['账单分析一下', '祈使但无导航动词'],
  ['去掉这个订阅', '「去」+掉 = 不是导航'],
  ['去年我的账单', '「去」+年 = 不是导航'],
  ['打开', '只有动词、无话题'],
  ['冻结卡', '侧栏快捷操作文案，不是导航'],
];
// 必须【跳】，且落到指定页面 —— 真·祈使式打开指令
const POSITIVE = [
  ['打开我的账单', '/bills'],
  ['去转账', '/transfer'],
  ['进入安全中心', '/safe'],
  ['跳到卡片管理', '/cards'],
  ['前往理财页面', '/wealth'],
  ['回到对话', '/chat'],
  ['帮我打开订阅管理', '/subs'],
  ['查看我的持仓', '/wealth'],
  ['看看我的账单', '/bills'],
  ['切换到安全中心', '/safe'],
  ['打开我的银行卡', '/cards'],
  ['看一下这个月的消费流水', '/bills'],
];

let pass = 0, fail = 0;
console.log('='.repeat(70));
console.log(' 前端「零延迟导航」行为测试（真跑 detectNavIntent）');
console.log('='.repeat(70));
console.log('\n[A] 不应跳转（陈述 / 咨询 / 非导航祈使）');
for (const [t, why] of NEGATIVE) {
  const got = detectNavIntent(t);
  const ok = got === null;
  ok ? pass++ : fail++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  「${t}」 → ${got ? got.to : 'null'}` + (ok ? '' : `  ★应为 null（${why}）`));
}
console.log('\n[B] 应跳转（真·打开指令）');
for (const [t, want] of POSITIVE) {
  const got = detectNavIntent(t);
  const ok = !!got && got.to === want;
  ok ? pass++ : fail++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  「${t}」 → ${got ? got.to : 'null'}` + (ok ? '' : `  ★应为 ${want}`));
}

console.log('\n' + '='.repeat(70));
console.log(` 结果：${pass}/${pass + fail} 通过`);
if (fail) {
  console.log(' 存在失败项 —— 前端仍会把"提及页面"当成"要求打开页面"');
  process.exit(1);
}
console.log(' ALL GREEN');
console.log('='.repeat(70));
