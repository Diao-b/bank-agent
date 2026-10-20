"""card_keys.py —— 卡密充值（模块 B，资金入口）核心逻辑

★ 归属分离（铁律完好）：管理员只生成「卡密（预付凭证）」——零余额操作；
  入账由用户主动提交卡密触发，走独立资金入口。任何路径都不允许管理员直接改余额。

★ 安全底线（逐条落地，详见 docs/card_key_design.md + sql/upgrade_card_key.sql）：
  a) 绝不存明文：库里只存 code_hash = SHA-256(pepper + code)；明文仅生成时一次性返回。
  b) 不可枚举：Crockford-base32 16 位（80bit 熵），禁自增/时间戳/短码。
  c) 原子核销：UPDATE ... WHERE code_hash=? AND status='unused' → rowcount==1 判定。
  d) 幂等：复用 idempotency_records（客户端带 Idempotency-Key），与入账同事务。
  e) 面额服务端权威：只取 card_keys.amount，忽略一切客户端金额。
  f) 面额生成即冻结：amount 一经生成不可改。
  g) 一次性+可作废：后台作废未使用卡密；已使用不可作废。
  h) 账实一致：入账同事务写 transactions(income/充值) + notifications。
  i) 审计：生成/作废/导出/核销全写 admin_logs（含 old→new）。
  j) 限流防爆破：核销按 user+IP 限频。
  k) 上限：单卡面额/单笔/单日充值上限走 rules.RULE_SPECS。

★ 模块 A 联动：核销命中 R-CHARGE（异常充值）由 routes/card_key.py 在成功后触发
  risk 引擎（不在本文件，避免循环依赖）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from decimal import Decimal, InvalidOperation
from datetime import datetime

from mock_bank import db
import rules
import config
from utils import idempotency
from utils import inbox

# Crockford base32（剔除 I L O U，避免 1/0 视觉混淆）—— 16 位 = 80bit 熵
_C32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _pepper() -> str:
    """卡密哈希盐：由服务端密钥派生（绝不随卡密下发）。"""
    return "cardkey::" + (config.AUTH_SECRET or "dev-pepper")


def hash_code(code: str) -> str:
    """SHA-256(pepper + code)。库里只存这个。"""
    return hashlib.sha256((_pepper() + code).encode("utf-8")).hexdigest()


def _gen_code() -> str:
    """生成 16 位 Crockford-base32 卡密（80bit 熵，不可枚举）。"""
    num = int.from_bytes(secrets.token_bytes(10), "big")
    chars = []
    for _ in range(16):
        num, rem = divmod(num, 32)
        chars.append(_C32[rem])
    return "".join(reversed(chars))


def _norm_code(code: str) -> str:
    """用户输入规范化：去空格、转大写（Crockford 不区分大小写）。"""
    return (code or "").strip().upper()


def _audit(admin_id: int, action: str, target_type: str, target_id, detail: dict) -> None:
    """管理端留痕（admin_logs）。生成/作废在 util 内统一留痕（单一来源，任何调用者都被审计）。"""
    db.execute(
        "INSERT INTO admin_logs (admin_id, action, target_type, target_id, detail) "
        "VALUES (%s,%s,%s,%s,%s)",
        (int(admin_id), action, target_type, str(target_id),
         json.dumps(detail, ensure_ascii=False, default=str)))


# --------------------------------------------------------------------------- #
#  生成批次（管理员：super_admin 专属，零余额操作）
# --------------------------------------------------------------------------- #
def generate_batch(amount: Decimal | str, count: int, created_by: int,
                   remark: str = "", batch_no: str | None = None) -> dict:
    """生成一批同额卡密。返回 {batch_no, amount, count, codes: [明文...]}。

    ★ 明文仅此一次返回，库里只落 code_hash + code_prefix（前4位人工核对）。
    """
    amount = Decimal(str(amount))
    amax = Decimal(str(rules.get_rule("card_key_amount_max")))
    if amount < Decimal("1") or amount > amax:
        raise ValueError(f"卡密面额需在 1 ~ {amax} 元之间")
    if not isinstance(count, int) or count < 1 or count > 1000:
        raise ValueError("单次生成数量需在 1 ~ 1000 之间")

    if not batch_no:
        batch_no = "CK" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(3).upper()

    codes = [_gen_code() for _ in range(count)]
    rows = [(hash_code(c), c[:4], amount, batch_no, int(created_by),
             (remark or "")[:255]) for c in codes]

    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO card_key_batches (batch_no, amount, count, created_by, remark) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (batch_no, amount, count, int(created_by), (remark or "")[:255]))
                cur.executemany(
                    "INSERT INTO card_keys (code_hash, code_prefix, amount, batch_no, "
                    "created_by, remark) VALUES (%s,%s,%s,%s,%s,%s)",
                    rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    # ★ 审计：只记元数据，绝不记明文
    _audit(created_by, "cardkey_generate", "card_batch", batch_no,
           {"count": count, "amount": str(amount), "remark": remark})

    return {"batch_no": batch_no, "amount": str(amount),
            "count": count, "codes": codes, "remark": remark}


# --------------------------------------------------------------------------- #
#  作废（管理员：super_admin 专属；仅 unused 可作废）
# --------------------------------------------------------------------------- #
def void_card(card_id: int, admin_id: int, reason: str) -> dict:
    """作废一张未使用卡密。返回 {ok, old_status}。已 used/void/expired 不可作废。"""
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status, amount, batch_no FROM card_keys WHERE id=%s FOR UPDATE",
                    (int(card_id),))
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return {"ok": False, "error": "卡密不存在"}
                if row["status"] != "unused":
                    conn.rollback()
                    return {"ok": False, "error": f"状态为 {row['status']}，只有 unused 可作废"}
                cur.execute(
                    "UPDATE card_keys SET status='void' WHERE id=%s AND status='unused'",
                    (int(card_id),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    _audit(admin_id, "cardkey_void", "card_key", card_id,
           {"reason": reason, "old_status": "unused", "amount": str(row["amount"]),
            "batch_no": row["batch_no"]})
    return {"ok": True, "old_status": "unused", "amount": str(row["amount"]),
            "batch_no": row["batch_no"]}


# --------------------------------------------------------------------------- #
#  列表（脱敏，管理员可见）
# --------------------------------------------------------------------------- #
def list_batches() -> list[dict]:
    rows = db.fetch_all(
        "SELECT b.batch_no, b.amount, b.count, b.created_by, b.created_at, b.remark, "
        "COUNT(k.id) AS total, "
        "SUM(CASE WHEN k.status='used' THEN 1 ELSE 0 END) AS redeemed "
        "FROM card_key_batches b LEFT JOIN card_keys k ON k.batch_no=b.batch_no "
        "GROUP BY b.batch_no ORDER BY b.created_at DESC")
    return [dict(r) for r in rows]


def list_cards(batch_no: str) -> list[dict]:
    """批次内卡密列表（脱敏：只给前4位 + 状态 + 面额，绝不给明文）。"""
    rows = db.fetch_all(
        "SELECT id, code_prefix, amount, status, created_at, used_at, used_by "
        "FROM card_keys WHERE batch_no=%s ORDER BY id", (batch_no,))
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
#  导出（管理员：理由 + 留痕；只含脱敏元数据，明文永不二次出现）
# --------------------------------------------------------------------------- #
def export_batch_meta(batch_no: str) -> list[dict]:
    """导出批次卡密元数据（前缀 + 面额 + 状态 + 使用时间），用于对账/盘点。

    ★ 安全：明文只在生成时返回一次，此后库里只有 code_hash，故导出不可能含明文。
    """
    return list_cards(batch_no)


# --------------------------------------------------------------------------- #
#  原子核销（用户侧：幂等 + 限流 + 上限 + 账实一致）
# --------------------------------------------------------------------------- #
def _throttle_ok(uid: int, ip: str) -> bool:
    """核销限流：user+IP+分钟桶；超过阈值拒绝。返回 True=放行。"""
    limit = int(rules.get_rule("card_key_redeem_rate_limit"))
    bucket = datetime.now().replace(second=0, microsecond=0)
    try:
        db.execute(
            "INSERT INTO card_redeem_throttle (user_id, ip, bucket_min, cnt) "
            "VALUES (%s,%s,%s,1) ON DUPLICATE KEY UPDATE cnt=cnt+1",
            (int(uid), (ip or "0.0.0.0")[:64], bucket))
        row = db.fetch_one(
            "SELECT cnt FROM card_redeem_throttle "
            "WHERE user_id=%s AND ip=%s AND bucket_min=%s",
            (int(uid), (ip or "0.0.0.0")[:64], bucket))
        cnt = int((row or {}).get("cnt") or 0)
        return cnt <= limit
    except Exception:  # noqa: BLE001 限流表异常不阻断业务
        return True


def _daily_redeemed(uid: int) -> Decimal:
    row = db.fetch_one(
        "SELECT COALESCE(SUM(amount),0) AS s FROM card_keys "
        "WHERE used_by=%s AND status='used' AND DATE(used_at)=CURDATE()", (int(uid),))
    return Decimal(str((row or {}).get("s") or 0))


def redeem(code: str, uid: int, account_id: int | None, idem_key: str,
           request_hash: str, client_ip: str) -> dict:
    """原子核销：成功则入账并返回 {amount, balance, txn_id}；失败抛异常/返回错误 dict。

    事务边界：幂等占位 + 卡密置 used + 账户加额 + 流水 + 通知 + 幂等终态
    全部在同一事务内，要么全成、要么全回滚。
    """
    code = _norm_code(code)
    if len(code) != 16:
        return {"ok": False, "error": "卡密格式不正确"}
    code_hash = hash_code(code)

    # ---- 限额 / 限流（在事务外快速拦截；限流计数独立提交，失败也计入）----
    single_max = Decimal(str(rules.get_rule("card_key_single_recharge_max")))
    daily_max = Decimal(str(rules.get_rule("card_key_daily_recharge_max")))
    if not _throttle_ok(uid, client_ip):
        return {"ok": False, "error": "核销过于频繁，请稍后再试", "code": "rate_limited"}

    # ---- 幂等：先查已落终态（同 key 成功 → 回放）----
    prior = idempotency.lookup(uid, idem_key)
    if prior and prior.get("status") == "done":
        return {"ok": True, "replayed": True,
                **(idempotency.parse_payload(prior.get("response_json")) or {})}

    payload: dict | None = None
    try:
        with db.get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    # 幂等占位（并发抢锁；撞车 → 让上层 409）
                    if not idempotency.claim(cur, uid, idem_key, "card_redeem", request_hash):
                        conn.rollback()
                        return {"ok": False, "error": "同请求正在处理中，请稍候重试",
                                "code": "conflict"}

                    # 锁卡（结构化匹配，杜绝"凭字符串猜卡"）
                    cur.execute(
                        "SELECT id, amount, status, batch_no FROM card_keys "
                        "WHERE code_hash=%s FOR UPDATE", (code_hash,))
                    card = cur.fetchone()
                    if not card or card["status"] != "unused":
                        conn.rollback()
                        return {"ok": False, "error": "卡密无效、已使用或已作废",
                                "code": "invalid"}

                    amount = Decimal(str(card["amount"]))
                    # 单笔上限
                    if amount > single_max:
                        conn.rollback()
                        return {"ok": False, "error": f"该卡面额超出单笔充值上限 {single_max} 元",
                                "code": "over_single"}
                    # 单日上限
                    if _daily_redeemed(uid) + amount > daily_max:
                        conn.rollback()
                        return {"ok": False, "error": f"今日充值已接近上限 {daily_max} 元",
                                "code": "over_daily"}

                    # 入账账户：必须属于该用户；未指定则取默认第一张
                    if account_id:
                        acct = db.fetch_one(
                            "SELECT id, balance FROM accounts WHERE id=%s AND user_id=%s",
                            (int(account_id), int(uid)))
                        if not acct:
                            conn.rollback()
                            return {"ok": False, "error": "入账账户不存在或不属于你",
                                    "code": "bad_account"}
                    else:
                        acct = db.fetch_one(
                            "SELECT id, balance FROM accounts WHERE user_id=%s ORDER BY id LIMIT 1",
                            (int(uid),))
                        if not acct:
                            conn.rollback()
                            return {"ok": False, "error": "你还没有可入账的账户",
                                    "code": "no_account"}

                    # 置 used（再校验一次 status，防止 TOCTOU）
                    cur.execute(
                        "UPDATE card_keys SET status='used', used_by=%s, used_at=NOW(), "
                        "used_account_id=%s WHERE id=%s AND status='unused'",
                        (int(uid), int(acct["id"]), int(card["id"])))
                    if cur.rowcount != 1:
                        conn.rollback()
                        return {"ok": False, "error": "卡密已被他人核销", "code": "concurrent"}

                    # 加额（SQL 原子自增）
                    cur.execute(
                        "UPDATE accounts SET balance = balance + %s WHERE id=%s",
                        (amount, int(acct["id"])))
                    new_balance = Decimal(str(acct["balance"])) + amount

                    # 流水（income / 充值 / 卡密充值；payee_id 留空，与转账分流）
                    cur.execute(
                        "INSERT INTO transactions (account_id, `date`, amount, category, "
                        "merchant, type, remark) VALUES (%s, CURDATE(), %s, %s, %s, %s, %s)",
                        (int(acct["id"]), amount, "充值", "卡密充值", "income",
                         card["batch_no"]))
                    txn_id = cur.lastrowid

                    # 通知（同事务）
                    inbox.push_notification(
                        int(uid), "recharge", "卡密充值成功",
                        f"您使用卡密成功充值 ¥{float(amount):,.2f}，已存入账户（尾号 "
                        f"{str(acct['id'])}），当前余额 ¥{float(new_balance):,.2f}。",
                        meta={"amount": float(amount), "txn_id": txn_id,
                              "account_id": int(acct["id"])},
                        cur=cur)

                    # 幂等终态（与业务同事务）
                    payload = {"ok": True, "amount": float(amount),
                               "balance": float(new_balance), "txn_id": txn_id,
                               "account_id": int(acct["id"]),
                               "batch_no": card["batch_no"]}
                    idempotency.finalize(cur, uid, idem_key, payload)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception:  # noqa: BLE001
        # 业务失败 → 删占位允许重试（另开连接，避免污染主事务；避免 IN_FLIGHT 卡死）
        try:
            with db.get_conn() as c2:
                with c2.cursor() as c2c:
                    idempotency.finalize(c2c, uid, idem_key, None)
                c2.commit()
        except Exception:  # noqa: BLE001
            pass
        raise
    return payload


# --------------------------------------------------------------------------- #
#  用户侧：本人核销历史
# --------------------------------------------------------------------------- #
def my_redeems(uid: int) -> list[dict]:
    rows = db.fetch_all(
        "SELECT id, amount, batch_no, used_at, used_account_id FROM card_keys "
        "WHERE used_by=%s AND status='used' ORDER BY used_at DESC", (int(uid),))
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
#  演示固定卡密（card_key_demo_enabled=1 时确保存在；生产必须关）
# --------------------------------------------------------------------------- #
_DEMO_CODE = "DEMODEMODEMODEMO"  # 16 位 Crockford 占位（仅演示）


def ensure_demo_card() -> None:
    if int(rules.get_rule("card_key_demo_enabled")) != 1:
        return
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM card_keys WHERE code_hash=%s", (hash_code(_DEMO_CODE),))
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO card_keys (code_hash, code_prefix, amount, batch_no, "
                        "created_by, remark) VALUES (%s,%s,%s,%s,%s,%s)",
                        (hash_code(_DEMO_CODE), _DEMO_CODE[:4], Decimal("100"),
                         "CKDEMO", None, "演示固定卡密"))
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
