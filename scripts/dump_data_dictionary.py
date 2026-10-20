"""dump_data_dictionary.py —— 从真实库导出全库数据字典（Markdown）

读取 information_schema，输出 docs/db_dictionary_full.md：表名 + 表备注 + 每个字段
（名称 / 类型 / 是否可空 / 默认值 / 备注）。库结构或备注变更后重跑即可刷新。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_bank import db

OUT = Path(__file__).resolve().parent.parent / "docs" / "db_dictionary_full.md"


def main() -> None:
    tabs = db.fetch_all(
        "SELECT TABLE_NAME, TABLE_COMMENT, TABLE_ROWS FROM information_schema.tables "
        "WHERE table_schema=DATABASE() AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME")

    lines = ["# 数据字典 · bank_agent（全库）", "",
             "> 由 `scripts/dump_data_dictionary.py` 从真实库导出；字段「说明」即数据库内 COMMENT。",
             f"> 共 {len(tabs)} 张表。", ""]

    for t in tabs:
        name = t["TABLE_NAME"]
        tcmt = t.get("TABLE_COMMENT") or ""
        cols = db.fetch_all(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY, "
            "COLUMN_COMMENT FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s ORDER BY ORDINAL_POSITION",
            (name,))
        lines.append(f"## {name}" + (f" — {tcmt}" if tcmt else ""))
        lines.append("")
        lines.append("| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for c in cols:
            key = "PK" if c["COLUMN_KEY"] == "PRI" else ("UQ" if c["COLUMN_KEY"] == "UNI" else ("IDX" if c["COLUMN_KEY"] == "MUL" else ""))
            d = c["COLUMN_DEFAULT"]
            d = "" if d is None else str(d)
            lines.append("| {n} | {t} | {nu} | {d} | {k} | {cm} |".format(
                n=c["COLUMN_NAME"], t=c["COLUMN_TYPE"],
                nu="是" if c["IS_NULLABLE"] == "YES" else "否",
                d=d.replace("|", "\\|"), k=key,
                cm=(c["COLUMN_COMMENT"] or "").replace("|", "\\|")))
        lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已导出 {OUT}")
    print(f"表 {len(tabs)} 张")


if __name__ == "__main__":
    main()
