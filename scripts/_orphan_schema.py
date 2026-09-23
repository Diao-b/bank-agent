# -*- coding: utf-8 -*-
"""只读诊断 3：所有表 / 是否有 user_id / 外键 / 孤儿统计（临时脚本）"""
import pymysql

conn = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="x5",
                       database="bank_agent", charset="utf8mb4",
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

cur.execute("SHOW TABLES")
tables = [list(r.values())[0] for r in cur.fetchall()]
print("== 表清单 ==")
print(" ", ", ".join(tables))

print("\n== user_id 覆盖情况 ==")
with_uid = ["transactions"]
without_uid = []
for t in tables:
    cur.execute(f"SHOW COLUMNS FROM {t} LIKE 'user_id'")
    if cur.fetchone():
        cur.execute(f"SELECT COUNT(*) c FROM {t} WHERE user_id NOT IN (SELECT id FROM users)")
        orphan = cur.fetchone()["c"]
        with_uid.append(t)
        print(f"  {t:24s} 有 user_id   孤儿行={orphan}")
    else:
        without_uid.append(t)
print("  无 user_id 的表：", ", ".join(without_uid))

print("\n== accounts 孤儿明细（按 uid 汇总）==")
cur.execute("""
SELECT user_id, COUNT(*) accts, SUM(balance) bal FROM accounts
WHERE user_id NOT IN (SELECT id FROM users) GROUP BY user_id ORDER BY user_id
""")
rows = cur.fetchall()
tot_acct = sum(r["accts"] for r in rows)
print(f"  孤儿 uid 共 {len(rows)} 个，账户 {tot_acct} 个，余额合计 {sum(r['bal'] for r in rows)}")
cur.execute("""
SELECT COUNT(*) c FROM transactions t
WHERE t.account_id IN (SELECT id FROM accounts WHERE user_id NOT IN (SELECT id FROM users))
""")
print(f"  孤儿 transactions 行数 = {cur.fetchone()['c']}")

print("\n== 外键约束 ==")
cur.execute("""
SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA='bank_agent' AND REFERENCED_TABLE_NAME IS NOT NULL
""")
fk = cur.fetchall()
for r in fk:
    print(f"  {r['TABLE_NAME']}.{r['COLUMN_NAME']} -> {r['REFERENCED_TABLE_NAME']}.{r['REFERENCED_COLUMN_NAME']} ({r['CONSTRAINT_NAME']})")
print(f"  外键总数 = {len(fk)}")

conn.close()
