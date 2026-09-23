"""
utils/settlement_engine.py —— 跨行清算 + 订阅补扣 双引擎（惰性触发，组 H 合并）

★ 设计原则（真实银行业务约束，改动前必读）
──────────────────────────────────────────────────────────────────────────
跨行转账本行无法实时校验对方账户（对方行的账，本行看不到）。
因此真实做法是：资金先扣款进入「在途（pending）」状态，交由清算网络处理，
清算结果只有两种 —— 到账（settled）或退票全额退回（returned）。

    ★ 账实始终一致：在途资金已离开可用余额，退票时原路全额退回，
      任何情况下都不会出现「钱扣了，既没到账也没退回」的悬空资金。

演示环境没有真实清算网络，故：
- 惰性触发（与订阅扣款引擎同款模式）：没有常驻定时器，访问任意数据接口
  （流水 / 余额 / 卡片 / 收件箱）时顺带把「到期的在途清算 / 到期订阅」处理掉。
- external_accounts 表模拟「他行可清算账户库」：
      在库  = 对方账户存在 → 清算成功（到账）；
      不在库 = 对方行退票   → 全额退回用户主账户。

订阅自动扣款引擎（原 deduction_engine，组 H 并入本文件）：没有定时器，靠读入口
顺带触发。到期未扣的订阅（status='active' AND next_due <= CURDATE()）在这里补扣：
    1. 查重守卫：同商户 + 同金额 + 同日期的流水已存在 → 只顺延不重扣；
    2. 选账户：按账户 id 顺序找第一个余额足够的账户；全都不够 → 跳过；
    3. 扣款落账 + 余额扣减 + next_due 顺延；
    4. 补扣上限：单次最多追 3 个周期；
    5. 取消闭环：引擎只处理 status='active'。

幂等与事务：全程单事务，任一失败整体回滚。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from mock_bank import db
from utils import inbox

# 清算延迟：生产为「创建 + 2 小时」，演示压缩为「创建 + 5 分钟」。
# 真正的到时时间在转账时写入 pending_settlements.expected_at（见 utils/transfer.py）。
# 业务常量单源见 rules.py（组 W）。
import rules  # C6：清算演示时长运行时从 system_config 读取

# --------------------------------------------------------------------------- #
#  跨行在途清算（settlement）
# --------------------------------------------------------------------------- #
# 逾期未处理、且尚未 done 的在途清算（行锁，防并发重复处理）
_DUE_SQL = (
    "SELECT id, txn_id, user_id, amount, target_card_no, target_bank_id, serial_no, created_at "
    "FROM pending_settlements "
    "WHERE status='pending' AND expected_at <= NOW() {user_filter} "
    "ORDER BY id ASC FOR UPDATE"
)

_EXT_EXISTS_SQL = "SELECT 1 FROM external_accounts WHERE card_no=%s LIMIT 1"

# 退款落点：用户主账户（is_primary 优先，其次 id 最小；排除信用账户）
_PRIMARY_ACCOUNT_SQL = (
    "SELECT id, name, balance FROM accounts WHERE user_id=%s "
    "AND COALESCE(account_type,'debit') <> 'credit' "
    "ORDER BY is_primary DESC, id ASC LIMIT 1 FOR UPDATE"
)

_BANK_NAME_SQL = "SELECT bank_name FROM bank_directory WHERE id=%s"


def _tail(card: str | None) -> str:
    return (card or "")[-4:]


def _masked_card(card: str | None) -> str:
    """6225****6688 形态的掩码（仅展示前 4 后 4，中间不打码全）。"""
    c = card or ""
    if len(c) < 8:
        return c or "—"
    return f"{c[:4]}****{c[-4:]}"


def _bank_name(cur, bank_id) -> str:
    if bank_id is None:
        return "他行"
    cur.execute(_BANK_NAME_SQL, (int(bank_id),))
    row = cur.fetchone() or {}
    return row.get("bank_name") or "他行"


def _settle_one(cur, row: dict) -> dict:
    """处理一笔到期在途清算：到账 或 退票全额退回。返回执行摘要。"""
    psid = int(row["id"])
    txn_id = int(row["txn_id"])
    uid = int(row["user_id"])
    amount = Decimal(str(row["amount"]))
    card = row.get("target_card_no")
    serial = row.get("serial_no")
    bank_name = _bank_name(cur, row.get("target_bank_id"))

    # ---- 清算判据：external_accounts 是否有该卡号（他行可清算账户库）----
    cur.execute(_EXT_EXISTS_SQL, (card,))
    exists = cur.fetchone() is not None

    if exists:
        # ================= 到账 =================
        cur.execute("UPDATE transactions SET settle_status='settled' WHERE id=%s", (txn_id,))
        cur.execute("UPDATE pending_settlements SET status='done' WHERE id=%s", (psid,))
        inbox.push_notification(
            uid, "transfer_receipt", "跨行转账已到账",
            f"您向{bank_name}尾号 {_tail(card)} 的跨行转账 ¥{float(amount):,.2f} "
            f"已完成清算并到账。流水号 {serial}。",
            meta={"amount": float(amount), "card_tail": _tail(card), "serial_no": serial,
                  "settle_status": "settled", "track": "cross_bank"},
            cur=cur,
        )
        db.log_operation(
            uid, "transfer_settled",
            {"settlement_id": psid, "txn_id": txn_id, "amount": float(amount),
             "serial_no": serial, "target_card_tail": _tail(card)},
            "success", cur=cur,
        )
        return {"settlement_id": psid, "txn_id": txn_id, "result": "settled",
                "amount": float(amount), "serial_no": serial}

    # ================= 退票（对方行退票）=================
    # 退款落点：用户主账户。找不到可退回账户属异常 —— 保持 pending 下次再试，
    # 绝不静默吞掉资金（账实一致优先于"处理完"）。
    cur.execute(_PRIMARY_ACCOUNT_SQL, (uid,))
    acct = cur.fetchone()
    if not acct:
        return {"settlement_id": psid, "txn_id": txn_id, "result": "skip_no_account",
                "amount": float(amount)}

    new_balance = Decimal(str(acct["balance"])) + amount
    cur.execute("UPDATE accounts SET balance=%s WHERE id=%s", (new_balance, acct["id"]))

    # 退款流水：类型=收入，分类=转账退回，remark 关联★原流水号（对账因果链）
    remark = f"跨行转账退票（原流水#{txn_id}" + (f" / {serial}" if serial else "") + "）"
    cur.execute(
        "INSERT INTO transactions (account_id, `date`, amount, category, merchant, "
        "type, remark) VALUES (%s, CURDATE(), %s, '转账退回', %s, 'income', %s)",
        (acct["id"], amount, f"{bank_name}退票", remark),
    )
    refund_txn_id = cur.lastrowid

    cur.execute("UPDATE transactions SET settle_status='returned' WHERE id=%s", (txn_id,))
    cur.execute("UPDATE pending_settlements SET status='done' WHERE id=%s", (psid,))

    # ★ 限额回补：退票 = 该笔转出当日"未真正离开"，额度返还（按转账所在自然日递减）。
    #   转账在提交日扣减计数器，退票若跨日也已不影响"当日"限额，递减对应自然日无害。
    try:
        cur.execute(
            "UPDATE transfer_daily_quota "
            "SET used_amount = GREATEST(used_amount - %s, 0.00) "
            "WHERE user_id=%s AND quota_date = DATE(%s)",
            (amount, uid, row.get("created_at")),
        )
    except Exception:  # noqa: BLE001  表/行不存在不影响退票本身
        pass

    # ★ 琥珀警示通知：明确"账户不存在已全额退回"，让用户立刻知道钱回来了
    inbox.push_notification(
        uid, "transfer_returned", "转账退票",
        f"转账退票：收款账户 {_masked_card(card)} 不存在，"
        f"¥{float(amount):,.2f} 已全额退回账户「{acct['name']}」。流水号 {serial}。",
        meta={"amount": float(amount), "card_tail": _tail(card), "serial_no": serial,
              "settle_status": "returned", "account_id": int(acct["id"]),
              "balance_after": float(new_balance), "track": "cross_bank"},
        cur=cur,
    )
    db.log_operation(
        uid, "transfer_returned",
        {"settlement_id": psid, "txn_id": txn_id, "refund_txn_id": refund_txn_id,
         "amount": float(amount), "serial_no": serial, "card_no": card},
        "success", cur=cur,
    )
    return {"settlement_id": psid, "txn_id": txn_id, "refund_txn_id": refund_txn_id,
            "result": "returned", "amount": float(amount), "serial_no": serial}


def process_due_settlements(user_id: int | None = None, cur: Any = None) -> dict:
    """到期在途清算入口。传 cur 复用调用方事务；不传则自开事务提交。

    user_id 为 None 时处理全部用户的到期清算（用于后台/脚本），
    否则只处理指定用户的（读接口按当前登录用户收敛）。
    """
    uid = int(user_id) if user_id is not None else None

    def _do(c) -> dict:
        if uid is None:
            sql, args = _DUE_SQL.replace("{user_filter}", ""), ()
        else:
            sql, args = _DUE_SQL.replace("{user_filter}", "AND user_id=%s"), (uid,)
        c.execute(sql, args)
        rows = list(c.fetchall())
        if not rows:
            return {"ok": True, "due_count": 0, "settled_count": 0,
                    "returned_count": 0, "details": []}
        details = [_settle_one(c, r) for r in rows]
        return {
            "ok": True,
            "due_count": len(rows),
            "settled_count": sum(1 for d in details if d["result"] == "settled"),
            "returned_count": sum(1 for d in details if d["result"] == "returned"),
            "details": details,
        }

    if cur is not None:
        return _do(cur)
    with db.get_conn() as conn:
        try:
            with conn.cursor() as c:
                result = _do(c)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def count_recent_returns(card_no: str, user_id: int | None = None, days: int = 1) -> int:
    """当日同一目标卡号的退票次数（用于转账前"近期有退票记录"预警）。

    ★ 只统计"已退票"的清算（原流水 settle_status='returned'），
      与到账的 done 区分开 —— 到账的清算不进退票计数。
    """
    if not card_no:
        return 0
    sql = (
        "SELECT COUNT(*) AS n FROM pending_settlements ps "
        "JOIN transactions t ON t.id = ps.txn_id "
        "WHERE ps.target_card_no=%s AND t.settle_status='returned' "
        "AND ps.created_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"
    )
    args: list[Any] = [card_no, int(days)]
    if user_id is not None:
        sql += " AND ps.user_id=%s"
        args.append(int(user_id))
    row = db.fetch_one(sql, tuple(args))
    return int((row or {}).get("n") or 0)


# --------------------------------------------------------------------------- #
#  订阅自动扣款（原 deduction_engine，组 H 并入）
# --------------------------------------------------------------------------- #
MAX_CATCHUP_CYCLES = 3   # 单次触发最多补扣周期数
CYCLE_DAYS = {"月付": 31, "年付": 366}   # 仅用于追扣上限的粗略护栏

_SUB_DUE_SQL = (
    "SELECT id, service_name, fee, cycle, merchant, next_due FROM subscriptions "
    "WHERE user_id=%s AND status='active' AND next_due IS NOT NULL "
    "AND next_due <= CURDATE() ORDER BY next_due ASC FOR UPDATE"
)

_SUB_ACCOUNTS_SQL = (
    "SELECT id, name, balance FROM accounts WHERE user_id=%s "
    "AND COALESCE(account_type,'debit') <> 'credit' ORDER BY id ASC FOR UPDATE"
)

_SUB_DUP_SQL = (
    "SELECT 1 FROM transactions t JOIN accounts a ON a.id=t.account_id "
    "WHERE a.user_id=%s AND t.merchant=%s AND t.amount=%s "
    "AND t.`date`=%s AND t.type='expense' LIMIT 1"
)


def _advance(d: date, cycle: str) -> date:
    """next_due 顺延一个周期：月付 +1 月（日溢出取月末），年付 +1 年。"""
    if cycle == "年付":
        try:
            return d.replace(year=d.year + 1)
        except ValueError:  # 2/29 → 2/28
            return d.replace(year=d.year + 1, day=28)
    y, m = divmod((d.year * 12 + d.month - 1) + 1, 12)
    m += 1
    last_day = (date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last_day))


def _charge_one(cur, uid: int, sub: dict, accounts: list[dict],
                today: date) -> dict:
    """对单个到期订阅补扣（可循环追扣），返回执行摘要。"""
    fee = Decimal(str(sub["fee"]))
    merchant = sub.get("merchant") or sub["service_name"]
    due = sub["next_due"] if isinstance(sub["next_due"], date) else today
    original_due = due
    charged, skipped = 0, 0
    total = Decimal("0")

    for _ in range(MAX_CATCHUP_CYCLES):
        if due > today:
            break  # 追平了

        # ---- 查重守卫：这笔已经扣过（入口重复触发 / 事务重放） ----
        cur.execute(_SUB_DUP_SQL, (uid, merchant, fee, due))
        if cur.fetchone():
            due = _advance(due, sub["cycle"])
            continue

        # ---- 选账户：第一个余额够的 ----
        account = next((a for a in accounts if Decimal(str(a["balance"])) >= fee), None)
        if account is None:
            skipped += 1
            break  # 余额不足：next_due 不动，留待下次触发重试

        cur.execute(
            "INSERT INTO transactions (account_id, `date`, amount, category, merchant, "
            "type, remark) VALUES (%s, %s, %s, '订阅', %s, 'expense', '订阅自动扣费')",
            (account["id"], due, fee, merchant),
        )
        cur.execute(
            "UPDATE accounts SET balance = balance - %s WHERE id=%s",
            (fee, account["id"]),
        )
        account["balance"] = Decimal(str(account["balance"])) - fee
        charged += 1
        total += fee
        due = _advance(due, sub["cycle"])

    # 顺延要持久化：真扣了当然要写；查重跳过后推算出的新周期也要写，
    # 否则 next_due 永远停在"已扣过的那天"，每次入口都白算一遍。
    # ★ 余额不足时 due 没动 → 不落库，下次触发重试。
    if due != original_due:
        if charged:
            cur.execute(
                "UPDATE subscriptions SET next_due=%s, last_used=%s WHERE id=%s",
                (due, today, sub["id"]),
            )
        else:
            cur.execute(
                "UPDATE subscriptions SET next_due=%s WHERE id=%s",
                (due, sub["id"]),
            )
    if charged:
        db.log_operation(
            uid, "subscription_deduct",
            {"sub_id": sub["id"], "service_name": sub["service_name"],
             "merchant": merchant, "charged": charged,
             "total": float(total), "next_due": due.isoformat()},
            "success", cur=cur,
        )

    return {
        "sub_id": sub["id"],
        "service_name": sub["service_name"],
        "charged": charged,
        "skipped_insufficient": bool(skipped),
        "total": float(total),
        "next_due": due.isoformat() if isinstance(due, date) else str(due),
    }


def run_due_deductions(user_id: int, cur: Any = None) -> dict:
    """到期补扣入口。传 cur 复用调用方事务；不传则自己开事务。

    返回：{"ok": True, "due_count": n, "charged_count": n,
           "details": [...], "total_amount": x.x}
    """
    uid = int(user_id)
    today = date.today()

    def _do(c) -> dict:
        c.execute(_SUB_DUE_SQL, (uid,))
        subs = list(c.fetchall())
        if not subs:
            return {"ok": True, "due_count": 0, "charged_count": 0,
                    "total_amount": 0.0, "details": []}
        c.execute(_SUB_ACCOUNTS_SQL, (uid,))
        accounts = list(c.fetchall())
        details = [_charge_one(c, uid, s, accounts, today) for s in subs]
        return {
            "ok": True,
            "due_count": len(subs),
            "charged_count": sum(d["charged"] for d in details),
            "total_amount": float(sum(Decimal(str(d["total"])) for d in details)),
            "details": details,
        }

    if cur is not None:
        return _do(cur)

    with db.get_conn() as conn:
        try:
            with conn.cursor() as c:
                result = _do(c)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def describe_deduction_result(result: dict) -> str:
    """把订阅扣款执行摘要转成一句话（Agent / 前端 toast 复用）。"""
    if not result or result.get("charged_count", 0) == 0:
        return "无到期订阅需要扣款"
    parts = []
    for d in result.get("details", []):
        if d["charged"]:
            parts.append(f"「{d['service_name']}」扣 {d['charged']} 期共 ¥{d['total']:.2f}")
        elif d.get("skipped_insufficient"):
            parts.append(f"「{d['service_name']}」余额不足已跳过")
    return "；".join(parts) if parts else "无到期订阅需要扣款"


# --------------------------------------------------------------------------- #
#  统一惰性入口：订阅到期补扣 + 跨行到期清算
# --------------------------------------------------------------------------- #
def process_housekeeping(user_id: int) -> dict:
    """统一惰性入口：订阅到期补扣 + 跨行到期清算。

    ★ 两引擎互相独立 try/except —— 任一失败不影响另一个，也不阻断读接口。
      （验收 #8：settlement 与订阅两引擎合并触发不互相干扰）
    """
    out: dict[str, Any] = {"deductions": None, "settlements": None}
    try:
        out["deductions"] = run_due_deductions(user_id)
    except Exception as exc:  # noqa: BLE001  扣款失败不阻断清算
        out["deductions_error"] = str(exc)
    try:
        out["settlements"] = process_due_settlements(user_id)
    except Exception as exc:  # noqa: BLE001  清算失败不影响读接口
        out["settlements_error"] = str(exc)
    return out


def describe_result(result: dict) -> str:
    """把清算摘要转成一句话（脚本 / 前端 toast 复用）。"""
    if not result or result.get("due_count", 0) == 0:
        return "无到期在途清算"
    parts = []
    for d in result.get("details", []):
        if d.get("result") == "settled":
            parts.append(f"流水#{d['txn_id']} 已到账 ¥{d['amount']:.2f}")
        elif d.get("result") == "returned":
            parts.append(f"流水#{d['txn_id']} 已退票退回 ¥{d['amount']:.2f}")
    return "；".join(parts) if parts else "无到期在途清算"


if __name__ == "__main__":  # 手动调试：python -m utils.settlement_engine [user_id]
    import sys

    target = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print(json.dumps(process_due_settlements(target), ensure_ascii=False, indent=2))
