"""check_delete_cascade.py —— 验证「删除用户 → 业务数据级联清空」真的生效

做法：在**单个事务**里造一个用户 + 一整套资产/会话/消息，`DELETE FROM users`，
逐表断言归零，最后 ROLLBACK —— 库不会被改动（只消耗一次自增值）。

同时验证反向保护：往 accounts 插一个不存在的 user_id 必须被外键拒绝（errno 1452）。

用法（bank-agent/ 下，.venv）：
    ./.venv/Scripts/python.exe scripts/check_delete_cascade.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402
import pymysql  # noqa: E402

env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
conn = pymysql.connect(host=env["DB_HOST"], port=int(env["DB_PORT"]), user=env["DB_USER"],
                       password=env["DB_PASSWORD"], database=env["DB_NAME"], charset="utf8mb4",
                       autocommit=False, cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

# 删除后必须一起归零的表（transactions 靠 account_id 两级级联）
CHILD_TABLES = ["accounts", "cards", "transactions", "payees", "holdings", "subscriptions",
                "pending_settlements", "notifications", "operation_logs",
                "verification_codes", "conversations", "chat_messages",
                "idempotency_records"]

TAG = "_cascade_selftest"
ok = True


def count(uid: int) -> dict[str, int]:
    out = {}
    for t in CHILD_TABLES:
        if t == "transactions":
            cur.execute("""SELECT COUNT(*) AS n FROM transactions t JOIN accounts a
                           ON a.id=t.account_id WHERE a.user_id=%s""", (uid,))
        elif t == "chat_messages":
            cur.execute("""SELECT COUNT(*) AS n FROM chat_messages m WHERE m.user_id=%s
                           OR m.conversation_id IN
                             (SELECT conversation_id FROM conversations WHERE user_id=%s)""",
                        (uid, uid))
        else:
            cur.execute(f"SELECT COUNT(*) AS n FROM {t} WHERE user_id=%s", (uid,))
        out[t] = cur.fetchone()["n"]
    return out


try:
    # ---------------------------------------------------------------- 造数据
    cur.execute("INSERT INTO users (name, username, password_hash) VALUES (%s,%s,%s)",
                ("级联自测", TAG, "x"))
    uid = int(cur.lastrowid)
    cur.execute("INSERT INTO accounts (user_id, name, balance, card_no, account_type, is_primary) "
                "VALUES (%s,'自测卡',1.00,%s,'debit',1)", (uid, "6102999900000001"))
    aid = int(cur.lastrowid)
    cur.execute("INSERT INTO cards (user_id, account_id, card_no, card_no_full, card_name, "
                "card_type, tail, bank, credit_limit, available_limit, status) "
                "VALUES (%s,%s,%s,%s,'自测卡','debit','0001','微众银行',0,0,'normal')",
                (uid, aid, "6102********0001", "6102999900000001"))
    cur.execute("INSERT INTO transactions (account_id, `date`, amount, category, merchant, type) "
                "VALUES (%s, CURDATE(), 9.90, '餐饮', '自测商户', 'expense')", (aid,))
    txn_id = int(cur.lastrowid)
    cur.execute("INSERT INTO payees (user_id, nickname, bank, account_tail) "
                "VALUES (%s,'自测收款人','招商银行','9999')", (uid,))
    cur.execute("INSERT INTO subscriptions (user_id, service_name, fee, cycle, last_used, status) "
                "VALUES (%s,'自测订阅',9.90,'月付',CURDATE(),'active')", (uid,))
    cur.execute("INSERT INTO notifications (user_id, type, title, content) "
                "VALUES (%s,'notice','自测通知','内容')", (uid,))
    cur.execute("INSERT INTO operation_logs (user_id, action, result) "
                "VALUES (%s,'selftest','ok')", (uid,))
    cur.execute("INSERT INTO verification_codes (user_id, scene, code_plain, code_hash, expires_at) "
                "VALUES (%s,'transfer','000000',%s,NOW())", (uid, "0" * 64))
    cur.execute("INSERT INTO pending_settlements (txn_id, user_id, amount, expected_at) "
                "VALUES (%s,%s,9.90,NOW())", (txn_id, uid))
    cur.execute("SELECT id FROM products LIMIT 1")
    pid = cur.fetchone()["id"]
    cur.execute("INSERT INTO holdings (user_id, product_id, amount, buy_date, status) "
                "VALUES (%s,%s,100.00,CURDATE(),'holding')", (uid, pid))
    cur.execute("INSERT INTO conversations (conversation_id, user_id, title) "
                "VALUES (%s,%s,'自测会话')", (TAG, uid))
    cur.execute("INSERT INTO chat_messages (conversation_id, user_id, role, content, seq) "
                "VALUES (%s,%s,'user','你好',1)", (TAG, uid))
    cur.execute("INSERT INTO idempotency_records "
                "(idem_key, user_id, action, request_hash, expires_at) "
                "VALUES (%s,%s,'selftest',%s,DATE_ADD(NOW(), INTERVAL 1 HOUR))",
                (TAG, uid, "0" * 64))

    before = count(uid)
    print("删除前各表行数：")
    for t, n in before.items():
        print(f"  {t:22s} {n}")
    empty = [t for t, n in before.items() if n == 0]
    if empty:
        ok = False
        print(f"  ✗ 以下表没有造出数据，自测不完整：{empty}")

    # ---------------------------------------------------------------- 删用户
    cur.execute("DELETE FROM users WHERE id=%s", (uid,))
    print(f"\nDELETE FROM users WHERE id={uid} → 影响 {cur.rowcount} 行")

    after = count(uid)
    print("\n删除后各表行数：")
    leftovers = []
    for t, n in after.items():
        flag = "" if n == 0 else "   <== 残留！"
        if n:
            leftovers.append(t)
        print(f"  {t:22s} {n}{flag}")
    if leftovers:
        ok = False
        print(f"\n✗ 级联失败，以下表仍有残留：{leftovers}")
    else:
        print("\n✓ 所有业务表均已级联清空")

    # -------------------------------------------------- 反向：孤儿写入必须被拒
    cur.execute("SAVEPOINT sp_fk")
    try:
        cur.execute("INSERT INTO accounts (user_id, name, balance, card_no) "
                    "VALUES (999999999,'孤儿账户',0,'6102888888888888')")
        ok = False
        print("✗ 外键未生效：竟然允许插入 user_id=999999999 的账户")
    except pymysql.err.IntegrityError as exc:
        print(f"✓ 外键拦住了孤儿写入：{exc.args[0]} {exc.args[1][:80]}")
        cur.execute("ROLLBACK TO SAVEPOINT sp_fk")

finally:
    conn.rollback()
    print("\n已回滚，数据库未发生净变更。")
    conn.close()

print("\n结论：" + ("自测通过" if ok else "自测失败，请检查上面的 ✗"))
sys.exit(0 if ok else 1)
