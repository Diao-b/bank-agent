// 校验 web/*.html 里内联的 babel 脚本能否被 Babel 转译（防整页白屏）
// 用法：node scripts/_check_babel.js [文件名...]   不给参数则检查 web/ 下所有含 text/babel 的页面
const fs = require('fs');
const path = require('path');
const Babel = require(path.join(__dirname, '..', 'web', 'vendor', 'babel.js'));

const WEB = path.join(__dirname, '..', 'web');
const args = process.argv.slice(2);
const files = args.length
  ? args.map(a => path.resolve(a))
  : fs.readdirSync(WEB).filter(f => f.endsWith('.html'))
      .map(f => path.join(WEB, f))
      .filter(f => fs.readFileSync(f, 'utf8').includes('<script type="text/babel"'));

if (!files.length) { console.error('NO_BABEL_BLOCK'); process.exit(1); }

let failed = 0;
for (const file of files) {
  const html = fs.readFileSync(file, 'utf8');
  const name = path.basename(file);
  const start = html.indexOf('<script type="text/babel"');
  if (start < 0) { console.log(`${name}: 无内联 babel 块，跳过`); continue; }
  const open = html.indexOf('>', start) + 1;
  const end = html.indexOf('</script>', open);
  const code = html.slice(open, end);
  try {
    const out = Babel.transform(code, { presets: ['react'] });
    console.log(`BABEL_OK   ${name}  script_chars=${code.length} out_chars=${(out.code || '').length}`);
  } catch (e) {
    failed++;
    console.error(`BABEL_FAIL ${name}: ${e.message}`);
  }
}
if (failed) { console.error(`${failed} 个页面转译失败（保存后必然是白屏）`); process.exit(1); }
