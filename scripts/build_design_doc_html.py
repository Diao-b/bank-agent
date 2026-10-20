"""scripts/build_design_doc_html.py —— 把设计文档 Markdown 渲染成**单文件 HTML**

用途：设计文档 `docs/PROJECT_DESIGN.md` 在普通 Markdown 预览器里表格会被挤成一团，
      这里生成一份自带样式的 HTML —— 左侧目录、卡片式提示块、zebra 表格、
      打印样式（`@media print`），可直接用于评审/答辩投屏或导出 PDF（浏览器打印→另存为 PDF）。

设计要点：
  · **单文件、零运行时依赖**（只有 mermaid 图走 CDN；断网时退化显示源码，不报错）；
  · 深浅色跟随系统 `prefers-color-scheme`；
  · 表格 `thead` 吸顶、长表格不跨页断开；
  · 提示块按首字符着色：⚠️ 琥珀（注意）、★ 靛蓝（铁律）。

依赖：`markdown`（含 tables / fenced_code / toc 扩展）。
      若用隔离 venv：
      C:/Users/Dwm/.workbuddy/binaries/python/envs/default/Scripts/python.exe
          scripts/build_design_doc_html.py

用法：
  python scripts/build_design_doc_html.py                     # 默认渲染 docs/PROJECT_DESIGN.md
  python scripts/build_design_doc_html.py <in.md> <out.html>
"""
from __future__ import annotations

import html as _html
import io
import os
import re
import sys

try:
    import markdown
    from markdown.extensions.toc import slugify_unicode
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖：pip install markdown（或在隔离 venv 里运行本脚本）")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "PROJECT_DESIGN.md")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(SRC)[0] + ".html"

CSS = """
:root{
  --bg:#ffffff; --fg:#1f2430; --muted:#6b7280; --line:#e6e8ee;
  --panel:#f7f8fb; --code-bg:#f4f6fa; --code-fg:#2b3244;
  --gold:#a8791b; --gold-soft:#fdf7e6; --warn:#b45309; --warn-bg:#fff8ec; --warn-line:#f0d9a8;
  --th:#eef1f7; --zebra:#fafbfd; --link:#1d4ed8;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0e1218; --fg:#e6e8ee; --muted:#9aa3b2; --line:#242b36;
    --panel:#141a22; --code-bg:#141a22; --code-fg:#dbe2ee;
    --gold:#e8c460; --gold-soft:#241f14; --warn:#f0b357; --warn-bg:#241d12; --warn-line:#4a3a1c;
    --th:#1a212b; --zebra:#121821; --link:#7aa2ff;
  }
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
}
.wrap{display:grid; grid-template-columns:268px minmax(0,1fr); gap:0; max-width:1380px; margin:0 auto}
nav{
  position:sticky; top:0; align-self:start; height:100vh; overflow:auto;
  padding:26px 18px; border-right:1px solid var(--line); background:var(--panel);
  font-size:13.5px;
}
nav .brand{font-weight:700; font-size:14px; letter-spacing:.2px; margin:0 0 4px}
nav .sub{color:var(--muted); font-size:12px; margin-bottom:16px}
nav a{display:block; color:var(--fg); text-decoration:none; padding:3px 8px; border-radius:6px; margin:1px 0}
nav a:hover{background:rgba(127,127,127,.13)}
nav a.h3{padding-left:20px; color:var(--muted); font-size:12.5px}
main{padding:40px 52px 120px; min-width:0}
h1{font-size:30px; line-height:1.35; margin:0 0 6px}
h2{font-size:21px; margin:46px 0 14px; padding-bottom:8px; border-bottom:2px solid var(--line)}
h3{font-size:16.5px; margin:30px 0 10px}
h4{font-size:15px; margin:22px 0 8px}
p{margin:12px 0}
a{color:var(--link)}
hr{border:0; border-top:1px solid var(--line); margin:34px 0}
ul,ol{padding-left:22px; margin:12px 0}
li{margin:5px 0}
li>code, p>code, td>code, th>code{
  background:var(--code-bg); border:1px solid var(--line); border-radius:5px;
  padding:1.5px 5px; font-size:.88em; font-family:"Cascadia Code",Consolas,monospace; color:var(--code-fg);
}
pre{
  background:var(--code-bg); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; overflow:auto; font-size:13px; line-height:1.6;
}
pre code{background:none; border:0; padding:0; font-family:"Cascadia Code",Consolas,"Microsoft YaHei",monospace}
table{
  border-collapse:collapse; width:100%; margin:16px 0; font-size:14px;
  display:table; table-layout:auto;
}
th,td{border:1px solid var(--line); padding:9px 12px; text-align:left; vertical-align:top}
th{background:var(--th); font-weight:600; white-space:nowrap}
tbody tr:nth-child(even){background:var(--zebra)}
blockquote{
  margin:16px 0; padding:12px 16px; border-left:4px solid var(--gold);
  background:var(--gold-soft); border-radius:0 10px 10px 0;
}
blockquote p{margin:6px 0}
blockquote.warn{border-left-color:var(--warn); background:var(--warn-bg)}
blockquote.warn p:first-child{color:var(--warn); font-weight:600}
.mermaid{margin:20px 0; text-align:center; background:transparent; border:0}
.mermaid svg{max-width:100%; height:auto}
.meta{color:var(--muted); font-size:13px; margin:0 0 4px}
.offline{display:none; color:var(--warn); font-size:13px; margin:6px 0 18px}
@media print{
  nav{display:none} .wrap{display:block; max-width:none} main{padding:0 6mm}
  h2{page-break-after:avoid} table,blockquote,pre{page-break-inside:avoid}
  a{color:inherit; text-decoration:none}
}
@media (max-width:900px){
  .wrap{grid-template-columns:1fr} nav{position:static; height:auto; border-right:0; border-bottom:1px solid var(--line)}
  main{padding:22px 18px 80px}
}
"""

JS = """
/* mermaid：**本地优先**（docs/vendor/mermaid.min.js），缺失才走 CDN；
   两者都不可用时把源码原样显示并给一句提示，绝不留空白。 */
(function(){
  function showOffline(){
    var n = document.getElementById('mermaid-offline');
    if (n) n.style.display = 'block';
  }
  function init(){
    if (!window.mermaid) return false;
    try{
      window.mermaid.initialize({
        startOnLoad:false, securityLevel:'loose',
        theme: window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'neutral'
      });
      window.mermaid.run({ nodes: document.querySelectorAll('pre.mermaid') });
      return true;
    }catch(e){ showOffline(); return false; }
  }
  if (!init()){
    var s = document.createElement('script');
    /* 兜底走国内可达镜像（jsdelivr 在本机不可达，实测 HTTP 000） */
    s.src = 'https://registry.npmmirror.com/mermaid/10.9.1/files/dist/mermaid.min.js';
    s.onload = function(){ if(!init()) showOffline(); };
    s.onerror = showOffline;
    document.head.appendChild(s);
  }
})();

/* 侧栏当前章节高亮 */
(function(){
  var links=[].slice.call(document.querySelectorAll('nav a[href^="#"]'));
  var map={}; links.forEach(function(a){ map[decodeURIComponent(a.getAttribute('href').slice(1))]=a; });
  var heads=[].slice.call(document.querySelectorAll('main h2, main h3'));
  if(!heads.length) return;
  function onScroll(){
    var y=window.scrollY+120, cur=null;
    heads.forEach(function(h){ if(h.offsetTop<=y) cur=h; });
    links.forEach(function(a){ a.style.fontWeight=''; a.style.background=''; });
    if(cur && map[cur.id]){ var a=map[cur.id]; a.style.fontWeight='700'; a.style.background='rgba(127,127,127,.13)'; }
  }
  window.addEventListener('scroll', onScroll, {passive:true}); onScroll();
})();
"""


def _slug_links(toc_html: str) -> str:
    """把 python-markdown 生成的 TOC 转成侧栏目录。

    层级靠 `<ul>` 嵌套深度识别：深度 1 = h2（一级）、深度 ≥2 = h3（缩进显示）。
    跳过「目录」自身条目 —— 侧栏本身就是目录，不需要再列一次。
    """
    depth = 0
    out = []
    for m in re.finditer(r'<ul>|</ul>|<li><a href="(#[^"]+)">(.*?)</a>', toc_html):
        tok = m.group(0)
        if tok == "<ul>":
            depth += 1
        elif tok == "</ul>":
            depth -= 1
        else:
            href, text = m.group(1), m.group(2)
            if text.strip() in ("目录",):
                continue
            cls = ' class="h3"' if depth >= 2 else ""
            out.append(f'<a href="{href}"{cls}>{text}</a>')
    return "\n".join(out)


def build() -> str:
    src = io.open(SRC, encoding="utf-8").read()
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
                           extension_configs={"toc": {
                               "toc_depth": "2-3",
                               "permalink": False,
                               # ★ 必须用 unicode slugify：默认 slugify 会把中文整段剥掉，
                               #   `## 2. 系统架构` 的 id 会退化成 `2`，文档内部锚点（如目录表里的
                               #   `#2-系统架构`）就全指向不存在的 id —— 实测踩过。
                               "slugify": slugify_unicode,
                           }})
    body = md.convert(src)
    toc = _slug_links(getattr(md, "toc", ""))

    # mermaid：```mermaid → <pre class="mermaid">（去掉 code 包裹）
    body = re.sub(r'<pre><code class="language-mermaid">(.*?)</code></pre>',
                  lambda m: '<pre class="mermaid">%s</pre>' % m.group(1), body, flags=re.S)
    # 提示块着色：首段以 ⚠ 开头 → warn
    def _tag(m):
        inner = m.group(1)
        cls = ' class="warn"' if ("⚠" in inner[:40]) else ''
        return "<blockquote%s>%s</blockquote>" % (cls, inner)
    body = re.sub(r"<blockquote>(.*?)</blockquote>", _tag, body, flags=re.S)

    title = "小微 AI 银行助手 · 系统设计文档"
    mt = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    if mt:
        title = re.sub(r"<[^>]+>", "", mt.group(1)).strip()
        # ★ H1 保留在正文里 —— 侧栏会显示标题，但**打印时侧栏是隐藏的**，
        #   正文没有 H1 的话，导出的 PDF 会连标题都没有。

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <nav>
    <div class="brand">{_html.escape(title)}</div>
    <div class="sub">目录（点击跳转）</div>
    {toc}
  </nav>
  <main>
    <div class="offline" id="mermaid-offline">⚠️ 未找到 <code>docs/vendor/mermaid.min.js</code> 且 CDN 不可达，下图以源码形式显示。联网或补齐该文件后即可渲染。</div>
    {body}
  </main>
</div>
<script src="vendor/mermaid.min.js"></script>
<script>{JS}</script>
</body>
</html>
"""


def main() -> int:
    if not os.path.isfile(SRC):
        print("找不到源文件:", SRC)
        return 1
    out = build()
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(out)
    print("已生成:", OUT, "| %.1f KB" % (len(out.encode("utf-8")) / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
