"""
scripts/seed_insite_transfer.py —— 站内转账闭环 · 测试数据种子（幂等）

配套 sql/upgrade_insite_transfer.sql 使用：
  SQL 负责 DDL（linked_user_id 列）+ 建「李哥」用户行 + 绑定「李雷」；
  本脚本负责 Python 才能做的部分 —— 给「李哥」初始化一整套独立演示资产
  （账户 / 卡片 / 收款人 / 流水 / 订阅，见 utils/user_provision.provision_new_user），
  并幂等地把「李雷」绑定到李哥。

用法（在 bank-agent/ 下，用项目 venv）：
    .venv/Scripts/python.exe scripts/seed_insite_transfer.py
可重复执行，已初始化的用户/已绑定的收款人会自动跳过。
"""
from __future__ import annotations

import os
import sys

# 允许从任意 cwd 直接跑：python scripts/seed_insite_transfer.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import config
from utils import user_provision
import profile_db


def _conn() -> pymysql.connections.Connection:
    return pymysql.connect(**config.db_config())


def ensure_linked_column(cur) -> None:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name='payees' AND column_name='linked_user_id'",
        (config.DB_NAME,),
    )
    if not cur.fetchone():
        cur.execute(
            # ★ 必须是 BIGINT（= users.id 的类型），否则 sql/upgrade_delete_cascade.sql 的
            #   `payees.linked_user_id → users.id` 外键会因类型不一致建不上（errno 3780）
            "ALTER TABLE payees ADD COLUMN linked_user_id BIGINT NULL "
            "COMMENT '该收款人对应的站内用户 ID（NULL=行外收款人）' AFTER account_tail"
        )


def ensure_lige_user(cur) -> int:
    """确保存在 username=lige 的用户，返回其 id（幂等）。"""
    cur.execute("SELECT id, name, role FROM users WHERE username=%s", ("lige",))
    row = cur.fetchone()
    if row:
        uid = int(row["id"])
        # 修正 name/role（SQL 迁移已做，这里兜底）
        cur.execute("UPDATE users SET name=%s, role='user' WHERE id=%s", ("李哥", uid))
        return uid
    # 用与 profile_db.hash_password 同格式的固定哈希（密码 lige123）
    cur.execute(
        "INSERT INTO users (name, username, password_hash, role) "
        "VALUES (%s, %s, %s, 'user')",
        ("李哥", "lige",
         "pbkdf2$sha256$100000$bElnRTIwMjZzZWVkMDE=$xTX66P4Aro6H7JrBS34YzS7aNSLyB3P/jkKprAA5jts="),
    )
    return int(cur.lastrowid)


def bind_li_lei(cur, lige_uid: int) -> None:
    cur.execute(
        "UPDATE payees p SET p.linked_user_id=%s "
        "WHERE p.nickname='李雷' AND p.user_id=1 AND p.linked_user_id IS NULL",
        (lige_uid,),
    )


def main() -> dict:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            ensure_linked_column(cur)
            lige_uid = ensure_lige_user(cur)
            bind_li_lei(cur, lige_uid)
        conn.commit()
    finally:
        conn.close()

    # 资产初始化（独立事务，幂等：已有 accounts 则跳过）
    provision = user_provision.provision_new_user(lige_uid, "李哥")

    # 再确认绑定（provision 不会动 linked_user_id，但双保险）
    conn = _conn()
    try:
        with conn.cursor() as cur:
            bind_li_lei(cur, lige_uid)
        conn.commit()
    finally:
        conn.close()

    return {"lige_uid": lige_uid, "provision": provision}


if __name__ == "__main__":
    import json
    summary = main()
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
