# -*- coding: utf-8 -*-
"""只读诊断：用户表 vs 资产表 的归属关系（临时脚本）"""
import pymysql

conn = pymysql.connect(host="127.0.0.1", port=3306, user="root", password="x5",
                       database="bank_agent", charset="utf8mb4",
                       cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

print("== users ==")
cur.execute("SELECT id, username, name, role, locked FROM users ORDER BY id")
users = cur.fetchall()
ids = {u["id"] for u in users}
for u in users:
    print(f"  #{u['id']:>3}  username={u['username']!r}  name={u['name']!r}  role={u.get('role')} locked={u.get('locked')}")

cur.execute("SHOW TABLE STATUS LIKE 'users'")
print("\nusers AUTO_INCREMENT =", cur.fetchone()["Auto_increment"])

for t in ("accounts", "cards", "payees", "subscriptions", "transactions"):
    try:
        cur.execute(f"SELECT COUNT(*) AS c FROM {t}")
        print(f"{t:16s} 总行数={cur.fetchone()['c']}")
    except Exception as e:
        print(f"{t:16s} ERR {e}")

print("\n== 每个 user_id 的资产分布（含孤儿）==")
q = """
SELECT a.user_id AS uid,
       (SELECT COUNT(*) FROM accounts x WHERE x.user_id=a.user_id) AS acct,
       (SELECT COUNT(*) FROM cards x WHERE x.user_id=a.user_id) AS card,
       (SELECT COUNT(*) FROM payees x WHERE x.user_id=a.user_id) AS payee,
       (SELECT COUNT(*) FROM subscriptions x WHERE x.user_id=a.user_id) AS subs
FROM (SELECT DISTINCT user_id FROM accounts
      UNION SELECT DISTINCT user_id FROM cards
      UNION SELECT DISTINCT user_id FROM payees
      UNION SELECT DISTINCT user_id FROM subscriptions) a
ORDER BY a.user_id
"""
cur.execute(q)
for r in cur.fetchall():
    flag = "" if r["uid"] in ids else "   <== 孤儿（users 表里没有这个 id）"
    print(f"  uid={r['uid']:>4}  accounts={r['acct']} cards={r['card']} payees={r['payee']} subs={r['subs']}{flag}")

print("\n== 资金流水条数（按账户）==")
cur.execute("""
SELECT a.user_id AS uid, a.id AS account_id, a.name, a.balance,
       (SELECT COUNT(*) FROM transactions t WHERE t.account_id=a.id) AS txn
FROM accounts a ORDER BY a.user_id, a.id
""")
for r in cur.fetchall():
    print(f"  uid={r['uid']:>4} acct#{r['account_id']:>3} {r['name']:<10} bal={r['balance']} txn={r['txn']}")

conn.close()
