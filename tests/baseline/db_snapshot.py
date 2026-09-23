"""
db_snapshot.py —— 无 mysqldump 时的 MySQL bank_agent 快照/恢复（保证基线可复现）

为什么需要：行为零变化 diff 要求「关键表 count」在重构前后完全一致。但回归脚本会改动
数据，所以每次重录基线前先把库还原到录制黄金基线那一刻的快照。

仅用于演示库 bank_agent 的「数据」还原，不动 schema（重构不迁移表结构）。

用法（bank-agent/ 下，.venv）：
    .venv/Scripts/python.exe tests/baseline/db_snapshot.py save    [--out tests/baseline/bank_agent.snapshot.json]
    .venv/Scripts/python.exe tests/baseline/db_snapshot.py restore [--in  tests/baseline/bank_agent.snapshot.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pymysql
import config

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bank_agent.snapshot.json")


def conn():
    # config.db_config() 已含 cursorclass=DictCursor
    return pymysql.connect(**config.db_config())


def _ser(v):
    from decimal import Decimal
    from datetime import datetime, date

    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def save(out=OUT) -> None:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SHOW TABLES")
            tbls = [list(r.values())[0] for r in cur.fetchall()]
            data: dict[str, list] = {}
            for t in tbls:
                cur.execute(f"SELECT * FROM `{t}`")
                rows = cur.fetchall()
                data[t] = [{k: _ser(v) for k, v in r.items()} for r in rows]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str)
    print(f"snapshot saved: {out} ({len(data)} tables, "
          f"{sum(len(v) for v in data.values())} rows)")


def restore(inp=OUT) -> None:
    with open(inp, encoding="utf-8") as f:
        data = json.load(f)
    failed = []
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for t, rows in data.items():
                try:
                    cur.execute(f"DELETE FROM `{t}`")
                    if not rows:
                        continue
                    cols = list(rows[0].keys())
                    placeholders = ", ".join(["%s"] * len(cols))
                    colsql = ", ".join(f"`{col}`" for col in cols)
                    sql = f"INSERT INTO `{t}` ({colsql}) VALUES ({placeholders})"
                    cur.executemany(sql, [[r.get(col) for col in cols] for r in rows])
                except Exception as exc:  # noqa: BLE001
                    failed.append((t, str(exc)))
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        c.commit()
    if failed:
        print(f"snapshot restored with {len(failed)} table errors:")
        for t, e in failed:
            print(f"  - {t}: {e}")
    else:
        print(f"snapshot restored: {inp} ({len(data)} tables)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["save", "restore"])
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--in", dest="inp", default=OUT)
    a = ap.parse_args()
    if a.cmd == "save":
        save(a.out)
    else:
        restore(a.inp)
