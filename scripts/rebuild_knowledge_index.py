"""rebuild_knowledge_index.py —— RAG 知识库索引重建（C6 知识库管理）

重扫 docs/knowledge/*.md，提取每篇文档的标题与加粗数字规则，
重建 README.md 的「## 索引表」—— 保证索引与文档实际内容一致。

CLI：python scripts/rebuild_knowledge_index.py
（管理端 /api/admin/knowledge/rebuild 亦调用本模块的 rebuild()）
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import sys

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "knowledge"

THEME_BY_FILE = {
    "transfer-rules.md": "转账规则",
    "wealth-products.md": "理财产品说明",
    "risk-disclosure.md": "风险揭示",
    "account-security.md": "账户安全",
    "subscription-faq.md": "订阅与代扣",
    "service-scope.md": "业务办理须知",
    "knowledge-boundaries.md": "知识边界与安全引导",
}


def _extract(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(.+)$", text, flags=re.M)
    title = m.group(1).strip() if m else md_path.stem
    # 加粗数字规则（含数字的 **粗体** 片段，去重取前若干个）
    bolds = re.findall(r"\*\*([^*\n]+)\*\*", text)
    nums = []
    for b in bolds:
        b = b.strip()
        if re.search(r"\d", b) and b not in nums:
            nums.append(b)
    return {
        "name": md_path.name,
        "title": title,
        "theme": THEME_BY_FILE.get(md_path.name, title.split("·")[-1].strip() or md_path.stem),
        "nums": nums[:4],
    }


def rebuild() -> dict:
    """重建 README.md 索引表。返回统计（docs / numbers）。幂等可重复执行。"""
    if not KNOWLEDGE_DIR.exists():
        raise FileNotFoundError(f"知识库目录不存在：{KNOWLEDGE_DIR}")
    entries = [_extract(p) for p in sorted(KNOWLEDGE_DIR.glob("*.md"))
               if p.name != "README.md"]
    rows = ["| 文档名 | 主题 | 关键数字摘要 |", "| --- | --- | --- |"]
    total_nums = 0
    for e in entries:
        total_nums += len(e["nums"])
        rows.append(f"| {e['name']} | {e['theme']} | {'；'.join(e['nums']) or '—'} |")
    table = "## 索引表\n\n" + "\n".join(rows) + "\n"

    readme = KNOWLEDGE_DIR / "README.md"
    text = readme.read_text(encoding="utf-8") if readme.exists() else ""
    if "## 索引表" in text:
        head = text[: text.index("## 索引表")]
        tail_match = re.search(r"\n## ", text[text.index("## 索引表") + len("## 索引表"):])
        tail = text[text.index("## 索引表") + len("## 索引表") + tail_match.start():] if tail_match else ""
        new_text = head + table + ("\n" + tail.lstrip("\n") if tail else "")
    else:
        new_text = text.rstrip() + "\n\n" + table

    new_text = re.sub(
        r"（索引重建于 [^）]*）",
        "",
        new_text,
    )
    new_text = new_text.rstrip() + f"\n\n（索引重建于 {datetime.now():%Y-%m-%d %H:%M}）\n"
    readme.write_text(new_text, encoding="utf-8")
    return {"docs": len(entries), "numbers": total_nums, "dir": str(KNOWLEDGE_DIR)}


if __name__ == "__main__":
    print(rebuild())
    sys.exit(0)
