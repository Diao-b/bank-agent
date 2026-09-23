# -*- coding: utf-8 -*-
"""_fk_consistency.py —— 只读体检：所有拟建外键列上的"指向虚空"行数

用途：在加 FOREIGN KEY 之前，先看清哪些行会挡住建表。
      只统计，不修改任何数据。
运行：cd bank-agent && ./.venv/Scripts/python.exe scripts/_fk_consistency.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402
import pymysql  # noqa: E402

env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
conn = pymysql.connect(host=env["DB_HOST"], port=int(env["DB_PORT"]), user=env["DB_USER"],
                       password=env["DB_PASSWORD"], database=env["DB_NAME"],
                       charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

# (子表, 子列, 父表, 父列, 期望动作)
CHECKS = [
    ("accounts", "user_id", "users", "id", "CASCADE"),
    ("cards", "user_id", "users", "id", "CASCADE"),
    ("cards", "account_id", "accounts", "id", "CASCADE"),
    ("transactions", "account_id", "accounts", "id", "CASCADE"),
    ("holdings", "user_id", "users", "id", "CASCADE"),
    ("subscriptions", "user_id", "users", "id", "CASCADE"),
    ("payees", "user_id", "users", "id", "CASCADE"),
    ("payees", "account_id", "accounts", "id", "SET NULL"),
    ("payees", "linked_user_id", "users", "id", "SET NULL"),
    ("pending_settlements", "user_id", "users", "id", "CASCADE"),
    ("notifications", "user_id", "users", "id", "CASCADE"),
    ("operation_logs", "user_id", "users", "id", "CASCADE"),
    ("verification_codes", "user_id", "users", "id", "CASCADE"),
    ("conversations", "user_id", "users", "id", "CASCADE"),
    ("chat_messages", "user_id", "users", "id", "CASCADE"),
    ("chat_messages", "conversation_id", "conversations", "conversation_id", "CASCADE"),
    ("idempotency_records", "user_id", "users", "id", "CASCADE"),
]

print(f"{'子表.子列':<42}{'父表.父列':<32}{'动作':<10}{'悬空行':>8}{'总行':>8}")
print("-" * 104)
bad = []
for child, ccol, parent, pcol, act in CHECKS:
    cur.execute(f"SHOW COLUMNS FROM {child} LIKE %s", (ccol,))
    if not cur.fetchone():
        print(f"{child + '.' + ccol:<42}{parent + '.' + pcol:<32}{act:<10}{'列不存在':>8}")
        continue
    cur.execute(f"SELECT COUNT(*) AS c FROM {child}")
    total = cur.fetchone()["c"]
    cur.execute(
        f"SELECT COUNT(*) AS c FROM {child} c "
        f"WHERE c.`{ccol}` IS NOT NULL AND NOT EXISTS "
        f"(SELECT 1 FROM {parent} p WHERE p.`{pcol}` = c.`{ccol}`)"
    )
    n = cur.fetchone()["c"]
    print(f"{child + '.' + ccol:<42}{parent + '.' + pcol:<32}{act:<10}{n:>8}{total:>8}")
    if n:
        bad.append((child, ccol, parent, pcol, act, n))

print("-" * 104)
print(f"共 {len(bad)} 个列存在悬空行，必须先清理才能建外键。")
for child, ccol, parent, pcol, act, n in bad:
    cur.execute(
        f"SELECT c.`{ccol}` AS v, COUNT(*) AS n FROM {child} c "
        f"WHERE c.`{ccol}` IS NOT NULL AND NOT EXISTS "
        f"(SELECT 1 FROM {parent} p WHERE p.`{pcol}` = c.`{ccol}`) "
        f"GROUP BY c.`{ccol}` ORDER BY n DESC LIMIT 12"
    )
    vals = [f"{r['v']}({r['n']})" for r in cur.fetchall()]
    print(f"  {child}.{ccol} -> {parent}.{pcol} [{act}]  {', '.join(vals)}")

conn.close()
