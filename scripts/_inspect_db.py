import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymysql, config, json
conn = pymysql.connect(**config.db_config())
cur = conn.cursor(pymysql.cursors.DictCursor)
for t in ("accounts", "cards", "transactions", "payees", "operation_logs"):
    cur.execute("""SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT
                   FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION""", (config.DB_NAME, t))
    print("=" * 20, t)
    for r in cur.fetchall():
        print(f"  {r['COLUMN_NAME']:22} {r['COLUMN_TYPE']:18} null={r['IS_NULLABLE']:3} def={r['COLUMN_DEFAULT']} {r['COLUMN_COMMENT'] or ''}")
cur.execute("SELECT id,user_id,name,balance,card_no FROM accounts ORDER BY id")
print("=" * 20, "accounts data")
for r in cur.fetchall():
    print(" ", r)
cur.execute("SELECT id,user_id,account_id,card_no,card_name,card_type,tail,status,credit_limit,available_limit FROM cards ORDER BY id")
print("=" * 20, "cards data")
for r in cur.fetchall():
    print(" ", r)
cur.execute("SELECT id,user_id,name FROM users ORDER BY id LIMIT 20")
print("=" * 20, "users")
for r in cur.fetchall():
    print(" ", r)
conn.close()
