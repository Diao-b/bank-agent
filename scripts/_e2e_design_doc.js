/* 真机校验：设计文档 HTML（docs/PROJECT_DESIGN.html）是否**真的渲染正确**
 *
 *  为什么需要：Markdown 源码看着没问题，但 mermaid 语法错一格就会在页面上
 *  渲染成一张 "Syntax error in text" 的红炸弹图（实测踩过：`[(("x"))]` 多了一层括号）。
 *  纯静态检查看不出来，必须用真浏览器跑一遍。
 *
 *  校验项：
 *    ① 页面可打开、标题/侧栏目录/表格数量符合预期；
 *    ② **每一张 mermaid 都渲染成 SVG**（`pre.mermaid svg` 数量 == `pre.mermaid` 数量）；
 *    ③ 页面上**不存在** "Syntax error" 字样；
 *    ④ 正文里有 H1（否则导出的 PDF 会没有标题）。
 *
 *  用法：node scripts/_e2e_design_doc.js        （先跑 build_design_doc_html.py 生成 HTML）
 */
const H = require('./_e2e_lib.js');
const path = require('path');
const fs = require('fs');

const HTML = 'file:///' + path.resolve('docs/PROJECT_DESIGN.html').replace(/\\/g, '/');

let ok = 0, fail = 0;
const check = (name, cond, extra) => {
  if (cond) { ok++; console.log('PASS ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
  else { fail++; console.log('FAIL ' + name + (extra !== undefined ? '   ' + JSON.stringify(extra) : '')); }
};

(async () => {
  const t0 = Date.now();
  const h = await H.launch({ width: 1500, height: 1000, watchdogMs: 120000 });
  try {
    if (!fs.existsSync(path.resolve('docs/PROJECT_DESIGN.html'))) {
      console.log('FAIL 缺少 docs/PROJECT_DESIGN.html，请先运行 build_design_doc_html.py');
      process.exit(1);
    }
    await h.goto(HTML);
    await H.sleep(6000);   // 等本地 mermaid 渲染

    const info = await h.eval(`(() => {
      const main = document.querySelector('main');
      const blocks = [...document.querySelectorAll('pre.mermaid')];
      const texts = blocks.map(b => (b.textContent || ''));
      return {
        title: document.title,
        h1: (document.querySelector('main h1') || {}).textContent || '',
        navLinks: document.querySelectorAll('nav a').length,
        tables: document.querySelectorAll('table').length,
        blocks: blocks.length,
        svgs: document.querySelectorAll('pre.mermaid svg').length,
        leftoverCode: blocks.filter(b => !b.querySelector('svg')).length,
        bodyHasSyntaxError: /Syntax error/i.test(document.body.innerText || ''),
        mermaidLoaded: typeof window.mermaid !== 'undefined',
        offlineShown: getComputedStyle(document.getElementById('mermaid-offline')).display !== 'none',
        leakedMd: texts.filter(t => /^(flowchart|sequenceDiagram|erDiagram)/m.test(t.trim())).length,
        // ★ 锚点完整性：正文与侧栏里每个 #链接都必须能落到真实存在的 id
        brokenAnchors: [...document.querySelectorAll('main a[href^="#"], nav a[href^="#"]')]
          .map(a => decodeURIComponent(a.getAttribute('href').slice(1)))
          .filter(id => id && !document.getElementById(id)),
        sampleIds: [...document.querySelectorAll('main h2')].slice(0, 3).map(x => x.id),
      };
    })()`);
    console.log('INFO ' + JSON.stringify(info));

    check('页面可打开且有标题', !!info.title && info.title.includes('设计文档'), info.title);
    check('正文含 H1（打印时才不会丢标题）', /设计文档/.test(info.h1), info.h1);
    check('侧栏目录已生成', info.navLinks > 20, info.navLinks);
    check('表格已渲染', info.tables >= 25, info.tables);
    check('★ 标题锚点保留中文（不是退化成 1/2/3）',
          info.sampleIds.every(id => /[\u4e00-\u9fa5]/.test(id)), info.sampleIds);
    check('★ 所有内部锚点都能跳到位（无死链）',
          info.brokenAnchors.length === 0, info.brokenAnchors);
    check('mermaid 脚本已加载（本地 vendor 优先）', info.mermaidLoaded === true);
    check('★ 每张 mermaid 图都渲染成 SVG',
          info.blocks > 0 && info.svgs === info.blocks && info.leftoverCode === 0,
          { 图块: info.blocks, 已渲染: info.svgs, 未渲染: info.leftoverCode });
    check('★ 页面上没有 "Syntax error"', info.bodyHasSyntaxError === false);
    check('离线提示未误显示', info.offlineShown === false);
    check('没有残留未转换的 mermaid 源码', info.leakedMd === 0, info.leakedMd);
    console.log('\n' + ok + ' passed, ' + fail + ' failed  (' + ((Date.now() - t0) / 1000).toFixed(1) + 's)');
  } catch (e) {
    console.log('ERR ' + (e && e.message || e));
  } finally {
    try { await h.close(); } catch (e) {}
    process.exit(fail ? 1 : 0);
  }
})();
