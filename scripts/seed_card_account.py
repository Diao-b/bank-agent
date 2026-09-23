"""
scripts/seed_card_account.py —— 卡片-账户一体化 · 数据迁移与种子（幂等）

配套 sql/upgrade_card_account.sql 使用（该文件是 DDL 文档，真正执行以本脚本为准）：

  DDL（Python 判列后执行，绕开 MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS 的限制）
    1) accounts.account_type  VARCHAR(16) NOT NULL DEFAULT 'debit'
    2) accounts.is_primary    TINYINT     NOT NULL DEFAULT 0
    3) cards.card_no_full     VARCHAR(19) NULL
    4) payees.account_id      BIGINT      NULL

  数据回填
    5) 每人 id 最小的账户 → is_primary=1（主账户；「归集到主账户」的默认目标，
       也是转账付款的默认扣款账户，与 /api/transfer 的 ORDER BY id ASC 口径一致）
    6) 存量卡的 card_no_full ← 挂靠账户的 accounts.card_no（卡与账户同号）
    7) ★ 遗留装饰卡清理：account_id IS NULL 的卡（旧模型产物，card_no 是伪造掩码
       如 '6225****1234'，既不是合法卡号也无法收付）按新原则「没有账户的卡片
       不允许存在」软删除（status='deleted'，可回溯），并打印明细。

用法（bank-agent/ 下，项目 venv）：
    .venv/Scripts/python.exe scripts/seed_card_account.py

可重复执行：已存在的列 / 已回填的字段 / 已注销的卡会自动跳过。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import config


def _conn() -> pymysql.connections.Connection:
    return pymysql.connect(**config.db_config())


def _column_exists(conn, table: str, col: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
            (config.DB_NAME, table, col),
        )
        return bool(cur.fetchone())


DDL = [
    ("accounts", "account_type",
     "ALTER TABLE accounts ADD COLUMN account_type VARCHAR(16) NOT NULL DEFAULT 'debit' "
     "COMMENT 'debit=储蓄账户 credit=信用账户（共享额度，不参与收付）' AFTER card_no"),
    ("accounts", "is_primary",
     "ALTER TABLE accounts ADD COLUMN is_primary TINYINT NOT NULL DEFAULT 0 "
     "COMMENT '1=主账户（每人唯一，id 最小的账户）' AFTER account_type"),
    ("cards", "card_no_full",
     "ALTER TABLE cards ADD COLUMN card_no_full VARCHAR(19) NULL "
     "COMMENT '完整卡号（Luhn 合法）；仅申请人可见一次，列表仅返回尾号' AFTER card_no"),
    ("payees", "account_id",
     "ALTER TABLE payees ADD COLUMN account_id BIGINT NULL "
     "COMMENT '站内收款对应的目标账户（跨行收款人为 NULL）' AFTER linked_user_id"),
]


def apply_ddl(conn) -> None:
    with conn.cursor() as cur:
        for table, col, sql in DDL:
            if _column_exists(conn, table, col):
                print(f"  · {table}.{col} 已存在，跳过")
                continue
            cur.execute(sql)
            print(f"  + {table}.{col} 已新增")
    conn.commit()


def backfill_primary(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts a "
            "JOIN (SELECT user_id, MIN(id) AS mid FROM accounts GROUP BY user_id) t "
            "  ON t.mid = a.id "
            "SET a.is_primary = 1 WHERE a.is_primary = 0"
        )
        n = cur.rowcount
    conn.commit()
    print(f"  · 主账户标记回填：{n} 个")
    return n


def backfill_card_no_full(conn) -> int:
    """存量卡的完整卡号 ← 挂靠账户的 card_no（卡与账户同号）。"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE cards c JOIN accounts a ON a.id = c.account_id "
            "SET c.card_no_full = a.card_no "
            "WHERE c.card_no_full IS NULL AND a.card_no IS NOT NULL"
        )
        n = cur.rowcount
    conn.commit()
    print(f"  · 存量卡补完整卡号：{n} 张")
    return n


def cleanup_accountless_cards(conn) -> list[dict]:
    """注销「没有账户」的遗留装饰卡（软删除，可回溯）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, user_id, card_no, card_name, card_type, tail, status FROM cards "
            "WHERE account_id IS NULL AND status <> 'deleted' ORDER BY id"
        )
        rows = list(cur.fetchall() or [])
        if not rows:
            print("  · 无「无账户卡」需要注销")
            return []
        print(f"  · 发现 {len(rows)} 张无账户的遗留卡（旧模型装饰记录），注销：")
        for r in rows:
            print(f"      - card#{r['id']} user#{r['user_id']} "
                  f"{r['card_name']}（{r['card_type']} / {r['card_no']}）")
        cur.execute(
            "UPDATE cards SET status='deleted', deleted_at=NOW() "
            "WHERE account_id IS NULL AND status <> 'deleted'"
        )
    conn.commit()
    return rows


def backfill_payee_account(conn) -> int:
    """站内收款人补 account_id（转账入账落点）。

    ★ 卡片-账户一体化后，转账必须入账到「这张卡对应的账户」。
      存量站内收款人（如 李雷 → 李哥）account_id 为空，会退化到「对方 id 最小的
      账户」这条老路 —— 能跑通但语义不完整。这里按收款人卡号反查补上目标账户，
      查不到就退回对方主账户（is_primary，缺省 id 最小者）。
    """
    from utils import card_service
    from utils.transfer import lookup_local_card
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, user_id, nickname, card_no, linked_user_id FROM payees "
            "WHERE linked_user_id IS NOT NULL AND account_id IS NULL"
        )
        rows = list(cur.fetchall() or [])
        n = 0
        for p in rows:
            info = lookup_local_card(p["card_no"], cur=cur) if p["card_no"] else None
            target = (info or {}).get("account_id")
            if not target:
                target = card_service.primary_account_id(cur, int(p["linked_user_id"]))
            if not target:
                continue
            cur.execute("UPDATE payees SET account_id=%s WHERE id=%s", (int(target), int(p["id"])))
            print(f"      · 收款人 {p['nickname']}(#{p['id']}) → 目标账户 #{target}")
            n += 1
    conn.commit()
    print(f"  · 站内收款人补目标账户：{n} 条")
    return n


def align_demo_accounts(conn) -> None:
    """把默认演示账号的账户名词汇对齐本行口径（幂等、只动该用户、只动名称/缺失账户）。

    ★ 为什么需要：默认演示账号（uid=1）是早期种子数据，主账户名叫
      「招商银行储蓄卡（主账户）」但卡号是微众 BIN（6102 开头）—— 名实不符；
      而「归集到主账户 / 把零钱罐的钱归到微众卡」这套演示话术依赖
      「微众卡 / 零钱罐」这两个名字（新注册用户的沙箱用的就是这两个名字）。
    ★ 只做两件事：
      1) 主账户名 招商银行储蓄卡（主账户）→ 微众卡（主账户）；
      2) 若没有「零钱罐」账户则补一个（余额 ¥500，与新用户沙箱一致）。
    """
    from utils import card_service
    with conn.cursor() as cur:
        uid = config.DEFAULT_USER_ID
        cur.execute("UPDATE accounts SET name=%s WHERE user_id=%s AND name=%s",
                    ("微众卡（主账户）", uid, "招商银行储蓄卡（主账户）"))
        if cur.rowcount:
            print(f"  · 主账户改名 → 微众卡（主账户）")
        cur.execute("SELECT COUNT(*) AS n FROM accounts WHERE user_id=%s AND name=%s",
                    (uid, "零钱罐"))
        if int(cur.fetchone()["n"]) == 0:
            card_no = card_service.gen_unique_card_no(cur)
            cur.execute(
                "INSERT INTO accounts (user_id, name, card_no, balance, account_type, is_primary) "
                "VALUES (%s, '零钱罐', %s, 500.00, 'debit', 0)",
                (uid, card_no),
            )
            print(f"  · 补「零钱罐」账户（¥500.00，卡号尾号 {card_no[-4:]}）")
    conn.commit()


def verify(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE status <> 'deleted' AND account_id IS NULL"
        )
        bad = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(
            "SELECT COUNT(*) AS n FROM accounts WHERE is_primary = 1"
        )
        prim = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM accounts"
        )
        users = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT COUNT(*) AS n FROM accounts WHERE account_type='credit'")
        credit_acc = int((cur.fetchone() or {}).get("n") or 0)
    print(f"  · 校验：无账户卡 {bad} 张（应为 0）；主账户 {prim} 个 / 有账户用户 {users} 人；"
          f"信用账户 {credit_acc} 个")


def main() -> None:
    print("[seed_card_account] 卡片-账户一体化迁移开始")
    conn = _conn()
    try:
        print(" 1) DDL")
        apply_ddl(conn)
        print(" 2) 主账户标记")
        backfill_primary(conn)
        print(" 3) 存量卡补完整卡号")
        backfill_card_no_full(conn)
        print(" 4) 无账户遗留卡清理")
        cleanup_accountless_cards(conn)
        print(" 5) 站内收款人补目标账户")
        backfill_payee_account(conn)
        print(" 6) 默认演示账号账户名对齐（微众卡 / 零钱罐）")
        align_demo_accounts(conn)
        print(" 7) 校验")
        verify(conn)
    finally:
        conn.close()
    print("[seed_card_account] 完成（幂等，可重复执行）")


if __name__ == "__main__":
    main()
