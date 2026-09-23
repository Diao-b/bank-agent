# -*- coding: utf-8 -*-
"""只读诊断 2：对比 uid=166/167 的本月汇总，并与截图数字比对（临时脚本）"""
import pymysql
from decimal import Decimal

conn = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="x5",
                       database="bank_agent", charset="utf8mb4",
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

for uid in (152, 153, 154, 155, 156, 157, 166, 167):
    cur.execute("SELECT id, name, card_no FROM accounts WHERE user_id=%s ORDER BY id", (uid,))
    accts = cur.fetchall()
    ids = [a["id"] for a in accts]
    if not ids:
        print(f"uid={uid}: 无账户")
        continue
    ph = ",".join(["%s"] * len(ids))
    cur.execute(
        f"SELECT COUNT(*) c, "
        f"COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0) exp, "
        f"COALESCE(SUM(CASE WHEN type='income' THEN amount END),0) inc "
        f"FROM transactions WHERE account_id IN ({ph}) AND `date` BETWEEN '2026-09-01' AND '2026-09-21'",
        ids)
    r = cur.fetchone()
    cur.execute(
        f"SELECT COUNT(*) c, COALESCE(SUM(CASE WHEN type='expense' THEN amount END),0) exp "
        f"FROM transactions WHERE account_id IN ({ph}) AND `date` BETWEEN '2026-08-01' AND '2026-08-31'",
        ids)
    r2 = cur.fetchone()
    cur.execute(
        f"SELECT `date`, amount, category, merchant, type FROM transactions "
        f"WHERE account_id IN ({ph}) AND `date` BETWEEN '2026-09-01' AND '2026-09-21' "
        f"ORDER BY `date` DESC LIMIT 6", ids)
    sample = cur.fetchall()
    names = [a["name"] for a in accts]
    print(f"uid={uid:>4} accts={names}")
    print(f"      9月(1-21): {r['c']} 笔 支出={r['exp']} 收入={r['inc']}  | 8月支出={r2['exp']} ({r2['c']}笔)")
    for s in sample:
        print(f"        {s['date']} {s['amount']:>10} {s['category']:<6} {s['merchant']}")

print("\n== cleanup_test_data.py 的账号匹配规则 ==")
cur.execute("SELECT id, name, card_no, user_id FROM accounts WHERE card_no LIKE 'TEST%' OR name LIKE '%测试%' LIMIT 10")
for r in cur.fetchall():
    print(" ", r)

conn.close()
