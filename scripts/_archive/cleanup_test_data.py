"""cleanup_test_data.py —— 清理验收测试灌入的数据（C6/C7 验收遗留）

清理范围（只动测试数据，不碰真实用户与审计日志）：
  1) 用户名匹配 c6_% / %_c6 的测试账号及其全部业务数据
     （notifications / verification_codes / operation_logs / chat_messages /
       conversations / accounts / cards / holdings / subscriptions /
       transactions / payees / pending_settlements / external_accounts / users）
  2) 群发给真实用户的"C6 验收公告"通知
  3) 探针产生的非法 scene 验证码（scene='x'）
保留：admin_logs（审计不可删）、真实用户数据。

运行：python scripts/cleanup_test_data.py   （支持 --dry-run 只统计不删除）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402
import pymysql  # noqa: E402

env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")

DRY = "--dry-run" in sys.argv
conn = pymysql.connect(host=env["DB_HOST"], port=int(env["DB_PORT"]),
                       user=env["DB_USER"], password=env["DB_PASSWORD"],
                       database=env["DB_NAME"], autocommit=False,
                       cursorclass=pymysql.cursors.DictCursor)

USER_TABLES = [
    "notifications", "verification_codes", "operation_logs", "chat_messages",
    "accounts", "cards", "holdings", "subscriptions", "transactions",
    "payees", "pending_settlements", "external_accounts",
    "idempotency_records", "transfer_daily_quota",
]

with conn.cursor() as cur:
    # 1) 测试账号 id 集合
    cur.execute(
        "SELECT id, username FROM users "
        "WHERE username LIKE 'c6\\_%' OR username LIKE '%\\_c6'"
    )
    test_users = cur.fetchall()
    ids = [u["id"] for u in test_users]

    print(f"测试账号 {len(ids)} 个：", [u["username"] for u in test_users][:8], "..." if len(ids) > 8 else "")

    def run(sql, args=None, label=""):
        # ★ 含 % 字面量的 SQL（如 LIKE '%…%'）不能给 pymysql 传空元组，否则被当格式化模板
        if args is None:
            cur.execute(sql)
        else:
            cur.execute(sql, args)
        n = cur.rowcount
        print(f"  - {label}: {n} 行")
        return n

    if DRY:
        acct_ids = []
        if ids:
            ph = ",".join(["%s"] * len(ids))
            cur.execute(f"SELECT id FROM accounts WHERE user_id IN ({ph})", ids)
            acct_ids = [r["id"] for r in cur.fetchall()]
            if acct_ids:
                aph = ",".join(["%s"] * len(acct_ids))
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM transactions WHERE account_id IN ({aph})",
                    acct_ids,
                )
                print(f"  [dry] transactions(按账户): {cur.fetchone()['n']} 行")
        for t in USER_TABLES:
            if ids:
                cur.execute(f"SHOW COLUMNS FROM {t} LIKE 'user_id'")
                if not cur.fetchone():
                    print(f"  [dry] {t}: 无 user_id 列，跳过")
                    continue
                ph = ",".join(["%s"] * len(ids))
                cur.execute(f"SELECT COUNT(*) AS n FROM {t} WHERE user_id IN ({ph})", ids)
                print(f"  [dry] {t}: {cur.fetchone()['n']} 行")
        cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE title LIKE %s", ("%C6 验收公告%",))
        print("  [dry] 验收公告通知:", cur.fetchone()["n"], "行")
        cur.execute("SELECT COUNT(*) AS n FROM verification_codes WHERE scene='x'")
        print("  [dry] 非法 scene 验证码:", cur.fetchone()["n"], "行")
        sys.exit(0)

    # 测试账号名下的账户 id（transactions 按 account_id 关联，须在删 accounts 前收集）
    acct_ids = []
    if ids:
        ph = ",".join(["%s"] * len(ids))
        cur.execute(f"SELECT id FROM accounts WHERE user_id IN ({ph})", ids)
        acct_ids = [r["id"] for r in cur.fetchall()]

    # ---- 删除测试账号的业务数据（无 user_id 列的表自动跳过）----
    for t in USER_TABLES:
        if not ids:
            continue
        cur.execute(f"SHOW COLUMNS FROM {t} LIKE 'user_id'")
        if not cur.fetchone():
            print(f"  - {t}: 无 user_id 列，跳过")
            continue
        ph = ",".join(["%s"] * len(ids))
        run(f"DELETE FROM {t} WHERE user_id IN ({ph})", ids, f"{t}")

    if acct_ids:
        ph = ",".join(["%s"] * len(acct_ids))
        run(f"DELETE FROM transactions WHERE account_id IN ({ph})", acct_ids, "transactions(按账户)")

    # 测试账号的会话（chat_messages 按 conversation_id 关联，上面已按 user 删消息；这里删会话行）
    if ids:
        ph = ",".join(["%s"] * len(ids))
        run(f"DELETE FROM conversations WHERE user_id IN ({ph})", ids, "conversations")

    # ---- 删除测试账号本身 ----
    if ids:
        ph = ",".join(["%s"] * len(ids))
        run("DELETE FROM users WHERE id IN (" + ph + ")", ids, "users(测试账号)")

    # ---- 群发给真实用户的验收公告 ----
    run("DELETE FROM notifications WHERE title LIKE '%C6 验收公告%'", None, "验收公告通知")

    # ---- 探针非法 scene 验证码 ----
    run("DELETE FROM verification_codes WHERE scene='x'", None, "非法 scene 验证码")

conn.commit()
conn.close()
print("清理完成。admin_logs 审计日志按规保留未动。")
