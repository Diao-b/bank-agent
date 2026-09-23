"""
utils/user_provision.py —— 新用户数据沙箱初始化（注册即开户）

背景：
    profile_db.create_user() 只往 users 表插一行，accounts / cards / payees /
    transactions / subscriptions 全空 —— 新用户注册后查余额为空、转账报"账户不存在"，
    整个产品等于不可用。这里补上"注册成功 → 立即拥有一套独立演示数据"。

对外接口：
    provision_new_user(user_id, real_name) -> dict    注册成功后调用（幂等）
    generate_transactions(account_id, months) -> int  流水生成（可单独复用）
    is_provisioned(user_id) -> bool                   是否已初始化过

★ 事务约定：
    provision_new_user 全程只用【一个连接 + 一个事务】，任何一步失败整体回滚，
    绝不留下"有账户没卡片"的半初始化状态。
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterator

import pymysql

import config
from mock_bank import db  # 连接收口到单一源（组 R）

# --------------------------------------------------------------------------- #
#  沙箱默认值（改这里就能调演示数据的规模）
# --------------------------------------------------------------------------- #
MAIN_ACCOUNT_NAME = "微众卡"
MAIN_ACCOUNT_BALANCE = Decimal("20000.00")
SUB_ACCOUNT_NAME = "零钱罐"
SUB_ACCOUNT_BALANCE = Decimal("500.00")

CARD_BANK = "微众银行"
CARD_NAME = "微众储蓄卡"

MIN_TXN_PER_MONTH = 30
MAX_TXN_PER_MONTH = 50

BANKS = ["招商银行", "工商银行", "建设银行", "中国银行", "交通银行"]

# 分类 → 商户池（商户名, 金额下限, 金额上限）
_MERCHANT_POOL: dict[str, list[tuple[str, int, int]]] = {
    "餐饮": [("瑞幸咖啡", 15, 45), ("海底捞", 80, 220), ("麦当劳", 25, 70),
             ("学校食堂", 12, 32), ("星巴克", 30, 60)],
    "购物": [("优衣库", 99, 420), ("天猫超市", 30, 280), ("名创优品", 20, 130),
             ("京东商城", 120, 900)],
    "交通": [("滴滴出行", 12, 65), ("地铁出行", 4, 16), ("高铁12306", 90, 420)],
    "娱乐": [("CGV影城", 35, 95), ("Steam 游戏商店", 30, 210), ("KTV", 60, 190)],
    "订阅": [("视频会员", 15, 30), ("云存储", 6, 25), ("音乐会员", 12, 20)],
}
_EXPENSE_CATEGORIES = ["餐饮", "购物", "交通", "娱乐", "订阅"]
# 支出权重要比 3:3:2:1:1，让"餐饮占比最高"这个分析结论成立
_CATEGORY_WEIGHTS = [3, 3, 2, 1, 1]

SALARY_RANGE = (8000, 12000)
ALLOWANCE_RANGE = (1000, 2000)  # 每月给妈妈的生活费


def _money(value: Any) -> Decimal:
    """float/int/str → 两位小数 Decimal（金额一律走 Decimal，禁止 float 累加）。"""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _rand_amount(rng: random.Random, lo: int, hi: int) -> Decimal:
    return _money(rng.uniform(lo, hi))


# --------------------------------------------------------------------------- #
#  连接（与 mock_bank/db.py 同源，独立一份避免循环导入）
# --------------------------------------------------------------------------- #
def _connect() -> pymysql.connections.Connection:
    # 收口到 db.get_conn（单一连接源）。
    # 注意：db.get_conn 是 @contextmanager，调用方用 `conn = _connect()` 后自行 close，
    # 不能简单 __enter__()——那样临时 contextmanager 会被 GC 提前执行 finally 关闭连接。
    # 这里把 cm 绑到 conn 上保活，直到调用方 close 后再随 conn 一起回收（finally 的二次
    # close 已被 db.get_conn 吞掉），行为与原来 pymysql.connect 完全一致。
    cm = db.get_conn()
    conn = cm.__enter__()
    conn._db_cm = cm  # type: ignore[attr-defined]
    return conn


# --------------------------------------------------------------------------- #
#  流水生成
# --------------------------------------------------------------------------- #
def generate_transactions(
    account_id: int,
    months: int = 1,
    *,
    cur: Any = None,
    seed: int | None = None,
    today: date | None = None,
) -> int:
    """为指定账户生成近 `months` 个月的初始流水并落库，返回写入条数。

    - 每月 30~50 笔（餐饮/购物/交通/娱乐/订阅 + 1 笔工资收入 + 1 笔给妈妈转账）
    - 日期回推并重排，**不会出现未来流水**（today 之后的日期会被夹到今天）
    - 传 `cur` 则并入调用方事务；不传则自己开一个事务
    - `seed` 给定后结果可复现（自测用）

    抽出这个函数是为了让"新用户 1 个月"和"演示用户 3 个月"共用同一套生成逻辑。
    """
    if months < 1:
        raise ValueError("months 必须 >= 1")
    rng = random.Random(seed)
    today = today or date.today()
    span_days = months * 30

    rows: list[tuple] = []
    for m in range(months):
        # 每月固定：1 笔工资（收入）+ 1 笔给妈妈的生活费（转账）
        salary_day = today - timedelta(days=m * 30 + 10)
        rows.append((
            account_id, min(salary_day, today),
            _rand_amount(rng, *SALARY_RANGE), "收入", "公司代发工资", "income",
        ))
        allowance_day = today - timedelta(days=m * 30 + 4)
        rows.append((
            account_id, min(allowance_day, today),
            _rand_amount(rng, *ALLOWANCE_RANGE), "转账", "妈妈", "expense",
        ))

        # 日常消费
        for _ in range(rng.randint(MIN_TXN_PER_MONTH, MAX_TXN_PER_MONTH)):
            category = rng.choices(_EXPENSE_CATEGORIES, weights=_CATEGORY_WEIGHTS, k=1)[0]
            merchant, lo, hi = rng.choice(_MERCHANT_POOL[category])
            offset = rng.randint(0, min(29, span_days))
            day = today - timedelta(days=m * 30 + offset)
            rows.append((
                account_id, min(day, today),
                _rand_amount(rng, lo, hi), category, merchant, "expense",
            ))

    rows.sort(key=lambda r: r[1])  # 按日期升序，读出来观感正常

    sql = (
        "INSERT INTO transactions (account_id, `date`, amount, category, merchant, type) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )

    if cur is not None:
        cur.executemany(sql, rows)
        return len(rows)

    conn = _connect()
    try:
        with conn.cursor() as c:
            c.executemany(sql, rows)
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  结构保护：新增列若未就位，注册初始化会直接失败（Unknown column 'source'）
# --------------------------------------------------------------------------- #
_SCHEMA_READY = False

# (表名, 列名, ALTER 语句) —— 与 sql/upgrade_subscription_v2.sql 保持一致，双保险
_REQUIRED_COLUMNS: list[tuple[str, str, str]] = [
    ("cards", "account_id",
     "ALTER TABLE cards ADD COLUMN account_id BIGINT NULL COMMENT '关联账户' AFTER user_id"),
    ("cards", "deleted_at",
     "ALTER TABLE cards ADD COLUMN deleted_at DATETIME NULL COMMENT '软删除时间'"),
    ("subscriptions", "source",
     "ALTER TABLE subscriptions ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'identified' "
     "COMMENT 'contracted=签约代扣 identified=流水识别'"),
    ("subscriptions", "merchant",
     "ALTER TABLE subscriptions ADD COLUMN merchant VARCHAR(100) NULL "
     "COMMENT '关联的流水商户名'"),
    ("subscriptions", "next_due",
     "ALTER TABLE subscriptions ADD COLUMN next_due DATE NULL COMMENT '下次扣款日'"),
    ("subscriptions", "confirmed",
     "ALTER TABLE subscriptions ADD COLUMN confirmed TINYINT NOT NULL DEFAULT 0 "
     "COMMENT 'identified 是否经用户确认'"),
]


def ensure_isolation_schema(force: bool = False) -> None:
    """幂等补齐多用户隔离所需的新增列（进程内只跑一次）。

    与 sql/upgrade_isolation.sql 内容一致 —— 那条是运维显式执行的标准路径，
    这条是兜底：避免"忘了跑脚本就注册"导致 provision 直接 500。
    """
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return

    conn = _connect()
    try:
        with conn.cursor() as cur:
            for table, column, ddl in _REQUIRED_COLUMNS:
                cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                    (config.DB_NAME, table, column),
                )
                if not cur.fetchone():
                    cur.execute(ddl)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    _SCHEMA_READY = True


# --------------------------------------------------------------------------- #
#  幂等判断
# --------------------------------------------------------------------------- #
def is_provisioned(user_id: Any, cur: Any = None) -> bool:
    """该用户是否已有账户记录（= 已初始化过）。"""
    sql = "SELECT COUNT(*) AS c FROM accounts WHERE user_id=%s"
    if cur is not None:
        cur.execute(sql, (user_id,))
        return int(cur.fetchone()["c"]) > 0
    conn = _connect()
    try:
        with conn.cursor() as c:
            c.execute(sql, (user_id,))
            return int(c.fetchone()["c"]) > 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  主入口
# --------------------------------------------------------------------------- #
def provision_new_user(
    user_id: Any,
    real_name: str = "",
    *,
    months: int = 1,
    seed: int | None = None,
) -> dict:
    """新用户注册成功后调用：创建一整套独立演示数据。

    返回：
        {"ok": True, "skipped": False, "user_id":.., "account_id":.., "transactions": n}
        {"ok": True, "skipped": True,  ...}   ← 已有账户记录，跳过（幂等）

    ★ 全程单事务：任一步异常整体回滚，绝不留下半初始化状态。
    ★ holdings 刻意留空 —— 新用户没买过理财，"引导购买"才是完整的体验闭环。
    """
    uid = int(user_id)
    rng = random.Random(seed)

    conn = _connect()
    try:
        with conn.cursor() as cur:
            # ---- 幂等闸门：已有账户记录直接返回，重复调用不产生重复账户 ----
            # ★★ 2026-09-21 安全提醒：这个闸门**必须只在「该 uid 的 users 行确实存在」时
            #   才认为是"重复调用"。否则一旦 MySQL 自增值被复用（旧用户只删了 users 行、
            #   业务数据留在库里），新注册的人会在这里被静默跳过初始化，
            #   从而**继承死账号的余额 / 卡 / 收款人 / 流水 / 订阅**。
            #   注册路径已在 `auth_register` 里先做残留清理（profile_db.count_user_assets /
            #   purge_user_assets），这里再兜一道：users 行不存在却有账户 → 判定脏数据。
            cur.execute("SELECT COUNT(*) AS c FROM accounts WHERE user_id=%s", (uid,))
            if int(cur.fetchone()["c"]) > 0:
                cur.execute("SELECT COUNT(*) AS c FROM users WHERE id=%s", (uid,))
                no_user_row = int(cur.fetchone()["c"]) == 0
                if no_user_row:
                    conn.rollback()
                    msg = (f"uid={uid} 存在资产记录但 users 表中没有该用户"
                           f"（典型的 uid 复用残留），拒绝静默跳过初始化；"
                           f"请先执行 scripts/purge_orphan_data.py 清理")
                    print(f"[error] {msg}")
                    return {"ok": False, "skipped": False, "user_id": uid, "error": msg}
                conn.rollback()
                return {"ok": True, "skipped": True, "user_id": uid, "reason": "已初始化，跳过"}

            # 用户名：没传真实姓名就用 users.name
            name = (real_name or "").strip()
            if not name:
                cur.execute("SELECT name FROM users WHERE id=%s", (uid,))
                row = cur.fetchone()
                name = (row or {}).get("name") or f"用户{uid}"

            # ---- 1) 账户：微众卡 20000.00 + 零钱罐 500.00 ----
            # ★ 卡片-账户一体化（2026-09-10）：账户必须有 Luhn 合法的完整卡号
            #   （转账收款靠 accounts.card_no 反查），主账户还要打 is_primary 标记
            #   （「归集到主账户」的目标 = is_primary，转账付款默认扣款账户也是它）。
            from utils import card_service
            main_card_no = card_service.gen_unique_card_no(cur)
            sub_card_no = card_service.gen_unique_card_no(cur)
            cur.execute(
                "INSERT INTO accounts (user_id, name, balance, card_no, account_type, is_primary) "
                "VALUES (%s, %s, %s, %s, 'debit', 1)",
                (uid, MAIN_ACCOUNT_NAME, MAIN_ACCOUNT_BALANCE, main_card_no),
            )
            main_account_id = int(cur.lastrowid)
            cur.execute(
                "INSERT INTO accounts (user_id, name, balance, card_no, account_type, is_primary) "
                "VALUES (%s, %s, %s, %s, 'debit', 0)",
                (uid, SUB_ACCOUNT_NAME, SUB_ACCOUNT_BALANCE, sub_card_no),
            )

            # ---- 2) 卡片：储蓄卡，挂主账户（卡与账户同号；卡号必须 Luhn 合法可反查）----
            tail = main_card_no[-4:]
            cur.execute(
                "INSERT INTO cards (user_id, account_id, card_no, card_no_full, card_name, card_type, "
                "tail, bank, credit_limit, available_limit, status) "
                "VALUES (%s, %s, %s, %s, %s, 'debit', %s, %s, 0, 0, 'normal')",
                (uid, main_account_id, card_service.mask_card_no(main_card_no), main_card_no,
                 CARD_NAME, tail, CARD_BANK),
            )
            card_id = int(cur.lastrowid)

            # ---- 3) 收款人：妈妈 + 同学小李（跨行）----
            # ★ 账户校验模型下，「仅凭姓名 + 自填尾号」的收款人转不了账（漏洞已封堵），
            #   所以种子收款人也必须给全三件套：bank_directory 外键（开户行）+
            #   Luhn 合法完整卡号 + 对方真实姓名。否则新用户注册完连一笔转账都发不出去。
            for nick, bank_name, real_name, bin4 in (
                ("妈妈", "招商银行", "李慧珍", "6225"),
                ("同学小李", "中国工商银行", "李强", "6222"),
            ):
                cur.execute("SELECT id, bank_name FROM bank_directory WHERE bank_name=%s",
                            (bank_name,))
                bk = cur.fetchone()
                if not bk:
                    continue
                pcard = card_service.gen_unique_card_no(cur, bin4 + "0000")
                cur.execute(
                    "INSERT INTO payees (user_id, nickname, real_name, bank, bank_id, card_no, "
                    "account_tail, is_frequent) VALUES (%s, %s, %s, %s, %s, %s, %s, 1)",
                    (uid, nick, real_name, bank_name, int(bk["id"]), pcard, pcard[-4:]),
                )

            # ---- 4) 流水：近 months 个月，30~50 笔/月 ----
            txn_count = generate_transactions(main_account_id, months, cur=cur, seed=seed)

            # ---- 5) 订阅：3 项（其中 1 项 last_used > 60 天，保留"闲置订阅"演示能力）----
            #     v2 语义：source='identified'（视为已确认的历史识别结果），
            #     merchant=服务名，next_due=下月初（避免注册当天就被惰性扣款）
            subs = [
                ("视频会员", Decimal("25.00"), "月付", 2),
                ("音乐会员", Decimal("15.00"), "月付", 5),
                ("网盘超级会员", Decimal("30.00"), "月付", 95),  # ★ 闲置
            ]
            sub_ids = []
            for service_name, fee, cycle, idle_days in subs:
                cur.execute(
                    "INSERT INTO subscriptions (user_id, service_name, fee, cycle, last_used, "
                    "status, source, merchant, next_due, confirmed) "
                    "VALUES (%s, %s, %s, %s, DATE_SUB(CURDATE(), INTERVAL %s DAY), "
                    "'active', 'identified', %s, "
                    "DATE_ADD(DATE_SUB(CURDATE(), INTERVAL DAYOFMONTH(CURDATE()) - 1 DAY), "
                    "INTERVAL 1 MONTH), 1)",
                    (uid, service_name, fee, cycle, idle_days, service_name),
                )
                sub_ids.append(int(cur.lastrowid))

            # ---- 6) holdings：留空（新用户没买过理财，引导购买才是体验闭环）----

            # ---- 7) 留痕 ----
            db.log_operation(
                uid,
                "provision_user",
                f"账户开户初始化：创建 {MAIN_ACCOUNT_NAME}/{SUB_ACCOUNT_NAME} 2 个账户、"
                f"1 张储蓄卡、2 位收款人、{txn_count} 笔流水、{len(sub_ids)} 项订阅",
                "success",
                cur=cur,
            )

        conn.commit()
        return {
            "ok": True,
            "skipped": False,
            "user_id": uid,
            "name": name,
            "account_id": main_account_id,
            "card_id": card_id,
            "subscription_ids": sub_ids,
            "transactions": txn_count,
            "holdings": 0,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def provision_user_by_username(username: str, *, months: int = 1) -> dict:
    """给已存在的老用户补初始化（scripts/provision_user.py 用）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, COALESCE(real_name, name) AS name FROM users WHERE username=%s",
                (username,),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "SELECT id, COALESCE(real_name, name) AS name FROM users WHERE name=%s",
                    (username,),
                )
                row = cur.fetchone()
        if not row:
            return {"ok": False, "error": f"用户 '{username}' 不存在"}
    finally:
        conn.close()

    result = provision_new_user(row["id"], row["name"], months=months)
    result["username"] = username
    return result
