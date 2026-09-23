"""purge_orphan_data.py —— 清理「users 表里已不存在」的孤儿用户数据

背景
----
bank_agent 库历史上没有任何外键约束，`DELETE FROM users WHERE ...` 只删了账号行，
accounts / cards / payees / subscriptions / transactions / conversations / chat_messages
等全部留在库里。后果不只是垃圾数据：
  `utils/user_provision.provision_new_user()` 的幂等闸门是
  「该 user_id 已有 accounts 记录就跳过初始化」——
  一旦 MySQL 自增值被复用，新注册用户会**静默继承死账号的全部数据**。

本脚本做两件事
----
1) **修复**：uid=58（李哥 / lige）是「站内转账」演示的收款方，`scripts/test_account_system.py`
   还把它的主账户卡号写成了常量。它的 users 行曾被误删，但资产必须保留 —— 所以这里按
   `sql/upgrade_insite_transfer.sql` 的同一份固定哈希把 users 行补回来（幂等）。
2) **清理**：其余孤儿 uid 的业务数据全部删除（按子表→父表顺序，单事务）。

用法（bank-agent/ 下，.venv）
----
    ./.venv/Scripts/python.exe scripts/purge_orphan_data.py --dry-run   # 只打印计划
    ./.venv/Scripts/python.exe scripts/purge_orphan_data.py             # 实际执行

★ 执行前务必先备份：mysqldump bank_agent > backup.sql
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402
import pymysql  # noqa: E402

DRY = "--dry-run" in sys.argv

# 需要「修复 users 行」而不是「删除资产」的 uid（值 = (name, username, password_hash, role)）
# 哈希取自 sql/upgrade_insite_transfer.sql（固定盐 + 密码 lige123），保证可复现。
REPAIR_USERS = {
    58: ("李哥", "lige",
         "pbkdf2$sha256$100000$bElnRTIwMjZzZWVkMDE=$xTX66P4Aro6H7JrBS34YzS7aNSLyB3P/jkKprAA5jts=",
         "user"),
}

# 所有带 user_id 的业务表（顺序无所谓，单事务 + 无跨表引用）
USER_TABLES = [
    "holdings", "subscriptions", "payees", "pending_settlements",
    "notifications", "operation_logs", "verification_codes",
    "chat_messages", "conversations", "cards", "accounts",
    "idempotency_records", "transfer_daily_quota",
]

env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
conn = pymysql.connect(host=env["DB_HOST"], port=int(env["DB_PORT"]), user=env["DB_USER"],
                       password=env["DB_PASSWORD"], database=env["DB_NAME"], charset="utf8mb4",
                       autocommit=False, cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()


def q(sql, args=None):
    cur.execute(sql, args) if args is not None else cur.execute(sql)
    return cur.fetchall()


def one(sql, args=None):
    rows = q(sql, args)
    return rows[0] if rows else {}


def cleanup_dangling() -> None:
    """跨表悬空（非用户维度）：chat_messages 挂着不存在的会话、transactions/cards 指向
    不存在的账户、payees 的关联账户或站内绑定指向不存在的行。这些同样会挡住建外键。"""
    print("\n跨表悬空清理：")
    cur.execute("""DELETE FROM chat_messages WHERE conversation_id NOT IN
                   (SELECT conversation_id FROM conversations)""")
    print(f"  deleted chat_messages(悬空会话) = {cur.rowcount}")
    cur.execute("""DELETE FROM transactions WHERE NOT EXISTS
                   (SELECT 1 FROM accounts a WHERE a.id=transactions.account_id)""")
    print(f"  deleted transactions(悬空账户) = {cur.rowcount}")
    cur.execute("""DELETE FROM cards WHERE account_id IS NOT NULL AND NOT EXISTS
                   (SELECT 1 FROM accounts a WHERE a.id=cards.account_id)""")
    print(f"  deleted cards(悬空账户) = {cur.rowcount}")
    cur.execute("""UPDATE payees SET account_id=NULL WHERE account_id IS NOT NULL AND NOT EXISTS
                   (SELECT 1 FROM accounts a WHERE a.id=payees.account_id)""")
    print(f"  nulled payees.account_id(悬空) = {cur.rowcount}")
    cur.execute("""UPDATE payees SET linked_user_id=NULL WHERE linked_user_id IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM users u WHERE u.id=payees.linked_user_id)""")
    print(f"  nulled payees.linked_user_id(悬空) = {cur.rowcount}")
    conn.commit()


def final_check() -> None:
    print("\n残留悬空行：", one("""SELECT
        (SELECT COUNT(*) FROM accounts WHERE user_id NOT IN (SELECT id FROM users)) AS accounts,
        (SELECT COUNT(*) FROM cards WHERE user_id NOT IN (SELECT id FROM users)) AS cards,
        (SELECT COUNT(*) FROM transactions t WHERE NOT EXISTS
           (SELECT 1 FROM accounts a WHERE a.id=t.account_id)) AS transactions,
        (SELECT COUNT(*) FROM subscriptions WHERE user_id NOT IN (SELECT id FROM users)) AS subs,
        (SELECT COUNT(*) FROM conversations WHERE user_id NOT IN (SELECT id FROM users)) AS convs,
        (SELECT COUNT(*) FROM idempotency_records WHERE user_id NOT IN (SELECT id FROM users)) AS idem,
        (SELECT COUNT(*) FROM transfer_daily_quota WHERE user_id NOT IN (SELECT id FROM users)) AS quota,
        (SELECT COUNT(*) FROM chat_messages m WHERE NOT EXISTS
           (SELECT 1 FROM conversations c WHERE c.conversation_id=m.conversation_id)) AS msgs"""))


def main() -> None:
    # ---------------------------------------------------------------- 1) 修复
    for uid, (name, username, ph, role) in REPAIR_USERS.items():
        exists = one("SELECT id FROM users WHERE id=%s", (uid,))
        if exists:
            print(f"[repair] uid={uid} 已存在，跳过")
            continue
        # 该 uid 是否真有遗留资产（没资产就别凭空造人）
        has = one("""SELECT
            (SELECT COUNT(*) FROM accounts WHERE user_id=%s) +
            (SELECT COUNT(*) FROM cards WHERE user_id=%s) +
            (SELECT COUNT(*) FROM transactions t JOIN accounts a ON a.id=t.account_id
              WHERE a.user_id=%s) AS n""", (uid, uid, uid))
        if not has["n"]:
            print(f"[repair] uid={uid} 无遗留资产，跳过")
            continue
        clash = one("SELECT id FROM users WHERE username=%s", (username,))
        if clash:
            print(f"[repair] username={username!r} 已被 uid={clash['id']} 占用，跳过（需人工处理）")
            continue
        if DRY:
            print(f"[repair][dry] 将补建 users 行 uid={uid} name={name!r} username={username!r}")
            continue
        cur.execute(
            "INSERT INTO users (id, name, username, password_hash, role) VALUES (%s,%s,%s,%s,%s)",
            (uid, name, username, ph, role),
        )
        print(f"[repair] 已补建 users 行：uid={uid} name={name!r} username={username!r}"
              f"（遗留资产 {has['n']} 行保留）")

    # ---------------------------------------------------------------- 2) 清理
    orphans = [r["id"] for r in q(
        """SELECT DISTINCT uid AS id FROM (
             SELECT user_id AS uid FROM accounts
             UNION SELECT user_id FROM cards
             UNION SELECT user_id FROM subscriptions
             UNION SELECT user_id FROM payees
             UNION SELECT user_id FROM holdings
             UNION SELECT user_id FROM notifications
             UNION SELECT user_id FROM operation_logs
             UNION SELECT user_id FROM verification_codes
             UNION SELECT user_id FROM conversations
             UNION SELECT user_id FROM chat_messages
             UNION SELECT user_id FROM pending_settlements
             UNION SELECT user_id FROM idempotency_records
             UNION SELECT user_id FROM transfer_daily_quota
           ) u WHERE u.uid IS NOT NULL AND u.uid NOT IN (SELECT id FROM users)
           ORDER BY id"""
    )]
    print(f"\n孤儿 uid 共 {len(orphans)} 个：{orphans}")
    if not orphans:
        print("\n没有孤儿用户数据。")
        if DRY:
            conn.close()
            return
        cleanup_dangling()
        final_check()
        conn.close()
        return

    ph = ",".join(["%s"] * len(orphans))
    acct_ids = [r["id"] for r in q(
        f"SELECT id FROM accounts WHERE user_id IN ({ph})", orphans)]
    print(f"其中孤儿账户 {len(acct_ids)} 个")

    plan: list[tuple[str, int]] = []
    if acct_ids:
        aph = ",".join(["%s"] * len(acct_ids))
        plan.append(("transactions（按孤儿 account_id）",
                     one(f"SELECT COUNT(*) AS n FROM transactions WHERE account_id IN ({aph})",
                         acct_ids)["n"]))
    # 跨表悬空：属于活人的卡/收款人指向了孤儿账户（体检显示当前为 0，仍兜底处理）
    # ★排除「卡自己就属于孤儿用户」的那些，避免与下面的 cards 计数重复
    if acct_ids:
        aph = ",".join(["%s"] * len(acct_ids))
        plan.append(("cards（属于活人但指向孤儿账户）",
                     one(f"SELECT COUNT(*) AS n FROM cards WHERE account_id IN ({aph}) "
                         f"AND user_id NOT IN ({ph})", acct_ids + orphans)["n"]))
    for t in USER_TABLES:
        plan.append((t, one(f"SELECT COUNT(*) AS n FROM {t} WHERE user_id IN ({ph})",
                            orphans)["n"]))

    print("\n清理计划：")
    total = 0
    for label, n in plan:
        if n:
            print(f"  - {label}: {n} 行")
            total += n
    print(f"  合计 {total} 行")

    if DRY:
        print("\n[dry-run] 未修改任何数据。去掉 --dry-run 即执行。")
        conn.close()
        return

    # 按子表 → 父表顺序删（transactions 先于 accounts；cards 先于 accounts）
    if acct_ids:
        aph = ",".join(["%s"] * len(acct_ids))
        cur.execute(f"DELETE FROM transactions WHERE account_id IN ({aph})", acct_ids)
        print(f"  deleted transactions(by account) = {cur.rowcount}")
        cur.execute(f"DELETE FROM cards WHERE account_id IN ({aph})", acct_ids)
        print(f"  deleted cards(by account) = {cur.rowcount}")

    for t in ["chat_messages", "conversations", "verification_codes", "operation_logs",
              "notifications", "pending_settlements", "holdings", "subscriptions",
              "payees", "cards", "accounts", "idempotency_records",
              "transfer_daily_quota"]:
        cur.execute(f"DELETE FROM {t} WHERE user_id IN ({ph})", orphans)
        print(f"  deleted {t} = {cur.rowcount}")

    conn.commit()
    print("\n清理完成（已提交）。")

    cleanup_dangling()
    final_check()
    conn.close()


if __name__ == "__main__":
    main()
