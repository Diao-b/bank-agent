"""
scripts/seed_account_system.py —— 账户体系重构 · 数据种子（幂等）

配套 sql/upgrade_account_system.sql 使用：
  SQL 负责 DDL（accounts.card_no / bank_directory / payees 新字段）+ 银行目录种子
        + 存量收款人按旧 bank 名回填 bank_id；
  本脚本负责 Python 才能做的部分（Luhn 校验位无法用 SQL 优雅生成）：
    1) 给所有存量 accounts 补「微众银行」完整卡号（16 位 Luhn 合法）；
    2) 确保「李哥」用户存在并初始化独立演示资产（复用 seed_insite_transfer）；
    3) 存量收款人逐条补 card_no / account_tail / real_name / bank / bank_id：
       - 妈妈 → 招商银行（尾号 6688）/ 工商银行（尾号 8899）
       - 李雷 → 微众银行 + link 李哥（card_no = 李哥主账户卡号，站内闭环生效）
       - 房东王姐 → 中国银行 / 张伟 → 交通银行 / 自己 → 工商银行
  4) 双妈妈候选流程保留（两个妈妈用不同银行 + 尾号区分）。

用法（bank-agent/ 下，项目 venv）：
    .venv/Scripts/python.exe scripts/seed_account_system.py
可重复执行，已生成卡号的账户 / 收款人自动跳过。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import config
from utils import card_validator
from utils import user_provision
import seed_insite_transfer as sit   # 复用 李哥确保 / 绑定逻辑


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


def apply_sql() -> None:
    """执行 upgrade_account_system.sql（按 ';' 切分，幂等语句安全重复执行）。

    ★ MySQL 8.0 不支持 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（那是 MariaDB
      语法），所以对 ADD COLUMN 语句用 information_schema 先判列是否存在，
      已存在则跳过，否则去掉 IF NOT EXISTS 后执行。CREATE TABLE IF NOT EXISTS
      与 INSERT ... ON DUPLICATE KEY UPDATE 在 MySQL 8.0 原生幂等。
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "sql", "upgrade_account_system.sql")
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    # 切分语句（忽略注释行）
    stmts = []
    buf = ""
    for line in raw.splitlines():
        if line.strip().startswith("--"):
            continue
        buf += line + "\n"
        if ";" in buf:
            for s in buf.split(";"):
                if s.strip():
                    stmts.append(s.strip())
            buf = ""
    if buf.strip():
        stmts.append(buf.strip())
    conn = _conn()
    try:
        with conn.cursor() as cur:
            for st in stmts:
                if not st:
                    continue
                # ADD COLUMN IF NOT EXISTS：MySQL 8.0 不支持，改为先判列存在
                m = re.search(r"ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+IF NOT EXISTS\s+(\w+)",
                              st, re.I)
                if m:
                    tbl, col = m.group(1), m.group(2)
                    if _column_exists(conn, tbl, col):
                        continue
                    st = re.sub(r"IF NOT EXISTS\s+", "", st, flags=re.I)
                try:
                    cur.execute(st)
                except pymysql.err.OperationalError as e:  # 个别已存在等，忽略
                    if "Duplicate" in str(e) or "already exists" in str(e).lower():
                        continue
                    raise
        conn.commit()
    finally:
        conn.close()


def seed_accounts_cards() -> int:
    """所有 card_no 为空的账户补微众卡号。返回补卡数。"""
    conn = _conn()
    n = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM accounts WHERE card_no IS NULL OR card_no=''")
            ids = [r["id"] for r in cur.fetchall()]
            for aid in ids:
                # 序号与账户 id 绑定 → 稳定且唯一
                seq = 1_000_000 + int(aid) * 7919
                cn = card_validator.generate_card_no("61022620", seq)
                cur.execute("UPDATE accounts SET card_no=%s WHERE id=%s", (cn, aid))
                n += 1
            # account_tail 同步（accounts 表无该列，忽略）
        conn.commit()
    finally:
        conn.close()
    return n


def lige_primary_card(cur) -> str | None:
    cur.execute(
        "SELECT a.card_no FROM accounts a JOIN users u ON u.id=a.user_id "
        "WHERE u.username='lige' AND a.card_no IS NOT NULL AND a.card_no<>'' "
        "ORDER BY a.id ASC LIMIT 1"
    )
    row = cur.fetchone()
    return row["card_no"] if row else None


def seed_payees(lige_uid: int, lige_card: str) -> dict:
    """存量收款人补卡号 / 开户行 / 实名。返回各收款人处理摘要。"""
    # (昵称, user_id, 期望银行 bank_code, 尾号) —— 跨行演示卡号
    cross = {
        ("妈妈", 1, "CMBC", "6688"): "招商银行",
        ("妈妈", 1, "ICBC", "8899"): "工商银行",
        ("房东王姐", 1, "BOC", "1102"): "中国银行",
        ("张伟", 1, "BCM", "5566"): "交通银行",
        ("自己（工资卡）", 1, "ICBC", "7788"): "工商银行",
    }
    summary: dict = {}
    conn = _conn()
    try:
        with conn.cursor() as cur:
            # ---- 跨行收款人：按 (昵称, 尾号) 唯一匹配旧记录 ----
            for (nick, uid, code, tail), bank_name in cross.items():
                cur.execute("SELECT id FROM bank_directory WHERE bank_code=%s", (code,))
                b = cur.fetchone()
                if not b:
                    continue
                cur.execute(
                    "SELECT id FROM payees WHERE user_id=%s AND nickname=%s "
                    "AND account_tail=%s AND card_no IS NULL LIMIT 1",
                    (uid, nick, tail),
                )
                row = cur.fetchone()
                if not row:
                    continue
                # ★ 必须用 bin_of_bank 反查 BIN：BIN_TO_BANK 是以「BIN 前缀」为键，
                #   拿 bank_code 去查只会拿到默认值，导致他行卡号 BIN 全变成招商。
                _bin4 = card_validator.bin_of_bank(code) or "6225"
                cn = card_validator.generate_card_no_with_tail(_bin4, tail)
                cur.execute(
                    "UPDATE payees SET card_no=%s, account_tail=%s, bank_id=%s, "
                    "real_name=NULL, bank=%s WHERE id=%s",
                    (cn, tail, int(b["id"]), bank_name, int(row["id"])),
                )
                summary[f"{nick}/{tail}"] = {"track": "cross", "card": cn, "bank": bank_name}

            # ---- 李雷 → 微众 + link 李哥（站内闭环）----
            cur.execute(
                "SELECT id FROM payees WHERE user_id=1 AND nickname='李雷' "
                "AND card_no IS NULL LIMIT 1"
            )
            row = cur.fetchone()
            if row and lige_card:
                cur.execute("SELECT id FROM bank_directory WHERE bank_code='WEBANK'")
                web = cur.fetchone()
                cur.execute(
                    "UPDATE payees SET card_no=%s, account_tail=%s, bank_id=%s, "
                    "linked_user_id=%s, real_name=%s, bank=%s WHERE id=%s",
                    (lige_card, lige_card[-4:], int(web["id"]), lige_uid, "李哥", "微众银行",
                     int(row["id"])),
                )
                summary["李雷/insite"] = {"track": "insite", "card": lige_card,
                                          "linked_user_id": lige_uid}

            # 兜底：仍缺卡号、但已绑定银行的收款人 → 按该银行 BIN 生成（保留原尾号）。
            #   覆盖历史遗留记录（如以往验收脚本建的测试收款人），保证收款人簿里
            #   每一条都绑定了一个可校验的账号 —— 不允许"没有卡号的收款人"。
            cur.execute("SELECT id, account_tail, bank_id FROM payees "
                        "WHERE card_no IS NULL AND bank_id IS NOT NULL")
            for row in cur.fetchall():
                cur.execute("SELECT bank_code FROM bank_directory WHERE id=%s", (row["bank_id"],))
                b = cur.fetchone()
                if not b:
                    continue
                _b4 = card_validator.bin_of_bank(b["bank_code"]) or "6225"
                tail = str(row["account_tail"] or "")[:4].rjust(4, "0")
                cn = card_validator.generate_card_no_with_tail(_b4, tail)
                if not cn:
                    continue
                cur.execute("UPDATE payees SET card_no=%s, account_tail=%s WHERE id=%s",
                            (cn, cn[-4:], int(row["id"])))
                summary[f"fallback#{row['id']}"] = {"track": "cross", "card": cn}

            # 清理：新模型下 bank_id 为空 = 未绑定任何银行目录条目，属无效记录
            cur.execute("DELETE FROM payees WHERE bank_id IS NULL")
            summary["_cleaned_no_bank"] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return summary


def main() -> dict:
    apply_sql()

    # 确保 李哥存在 + 资产初始化 + 绑定
    conn = _conn()
    try:
        with conn.cursor() as cur:
            lige_uid = sit.ensure_lige_user(cur)
            sit.bind_li_lei(cur, lige_uid)
        conn.commit()
    finally:
        conn.close()
    user_provision.provision_new_user(lige_uid, "李哥")   # 资产初始化（幂等）
    conn = _conn()
    try:
        with conn.cursor() as cur:
            sit.bind_li_lei(cur, lige_uid)
        conn.commit()
    finally:
        conn.close()

    n_acc = seed_accounts_cards()

    # 取李哥主账户卡号（用于 李雷 站内绑定）
    conn = _conn()
    try:
        with conn.cursor() as cur:
            lige_card = lige_primary_card(cur)
    finally:
        conn.close()

    payee_summary = seed_payees(lige_uid, lige_card or "")

    return {
        "lige_uid": lige_uid,
        "lige_card": lige_card,
        "accounts_card_seeded": n_acc,
        "payees": payee_summary,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(main(), ensure_ascii=False, indent=2, default=str))
