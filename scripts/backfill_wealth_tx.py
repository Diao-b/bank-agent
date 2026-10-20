# -*- coding: utf-8 -*-
"""scripts/backfill_wealth_tx.py —— 补录「理财申购/赎回」缺失的流水

背景（2026-10-11 修 P0）：`utils/wealth.py` 的 buy_product / redeem 此前只动余额 + 持仓
+ 操作留痕，**不写 transactions** → 历史上发生过的理财买卖在「账单分析 / 流水明细 /
分类占比」里全部查无此笔。修好代码后，**存量缺口需要本脚本回填**。

做法（幂等 + 可核对）：
  1. 从 operation_logs 取 buy_product / redeem 且 result=success 的记录，detail 里有
     serial_no / amount / product_id / created_at（买入另需账户：按业务口径取
     该用户名下 id 最小的非信用账户，与代码一致）；
  2. 对每条 serial_no，检查 transactions 里是否已有 remark 含该号的行 → 有则跳过；
  3. 缺则补一条：category=理财，买入 type=expense / 赎回 type=income，date 用当时日期，
     remark 与原业务一致的「申购理财（serial）」/「赎回理财（serial）」。

用法：
    .venv/Scripts/python.exe -u scripts/backfill_wealth_tx.py 9            # 预演（不改库）
    .venv/Scripts/python.exe -u scripts/backfill_wealth_tx.py 9 --apply    # 实际写入
    .venv/Scripts/python.exe -u scripts/backfill_wealth_tx.py --all --apply
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mock_bank import db  # noqa: E402


def _loads(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    return raw if isinstance(raw, dict) else {}


def primary_account(cur, uid: int) -> int | None:
    """买入扣款账户：用户名下 id 最小的非信用账户（与 wealth.buy_product 同口径）。"""
    cur.execute(
        "SELECT id FROM accounts WHERE user_id=%s AND COALESCE(account_type,'debit') <> 'credit' "
        "ORDER BY id ASC LIMIT 1", (uid,))
    row = cur.fetchone()
    return int(row["id"]) if row else None


def plan_for_user(cur, uid: int) -> list[dict]:
    """列出该用户需要补录的流水（不改库）。"""
    cur.execute("SELECT id, name FROM products")
    prod_name = {int(r["id"]): r["name"] for r in cur.fetchall()}
    # 赎回留痕 detail 里没有 product_id，靠 holding_id 反查产品名（持仓可能已被删 → 回落"理财"）
    cur.execute("SELECT id, product_id FROM holdings")
    hold_prod = {int(r["id"]): int(r["product_id"]) for r in cur.fetchall()}
    cur.execute(
        "SELECT id, action, detail, created_at FROM operation_logs "
        "WHERE user_id=%s AND action IN ('buy_product','redeem') AND result='success' "
        "ORDER BY id", (uid,))
    todo = []
    for r in cur.fetchall():
        d = _loads(r["detail"])
        serial = d.get("serial_no")
        amount = d.get("amount")
        if not serial or amount is None:
            continue
        # 幂等：已存在 remark 含该流水号的行 → 跳过
        cur.execute("SELECT id FROM transactions WHERE remark LIKE %s LIMIT 1", ("%" + str(serial) + "%",))
        if cur.fetchone():
            continue
        kind = "申购" if r["action"] == "buy_product" else "赎回"
        if r["action"] == "buy_product":
            acc = primary_account(cur, uid)
        else:
            # 赎回优先用留痕里的 account_id；10-11（回款到原扣款账户）之前的旧记录
            # detail 里没有 account_id → 回落用户主账户，与 utils/wealth.redeem 的
            # 「原扣款账户缺失/失效则回落」兜底口径一致。
            acc = d.get("account_id") or primary_account(cur, uid)
        pid = d.get("product_id")
        if pid is None and d.get("holding_id") is not None:
            pid = hold_prod.get(int(d["holding_id"]))
        pname = prod_name.get(int(pid)) if pid is not None else None
        todo.append({
            "log_id": int(r["id"]), "serial": serial, "amount": float(amount),
            "date": str(r["created_at"])[:10], "account_id": int(acc) if acc else None,
            "merchant": pname or "理财", "type": "expense" if kind == "申购" else "income",
            "remark": f"{kind}理财（{serial}）",
        })
    return todo


def apply_rows(cur, rows: list[dict]) -> int:
    n = 0
    for x in rows:
        if not x["account_id"]:
            print("  [skip] 无可用账户 log#%s" % x["log_id"])
            continue
        cur.execute(
            "INSERT INTO transactions (account_id, `date`, amount, category, merchant, type, remark) "
            "VALUES (%s, %s, %s, '理财', %s, %s, %s)",
            (x["account_id"], x["date"], str(x["amount"]), x["merchant"], x["type"], x["remark"]))
        print("  [+ ] 补录 %s acc=%s %s ¥%.2f %s | %s"
              % (x["date"], x["account_id"], x["merchant"], x["amount"], x["type"], x["remark"]))
        n += 1
    return n


def main() -> int:
    args = [a for a in sys.argv[1:]]
    do_apply = "--apply" in args
    all_users = "--all" in args
    uids = [int(a) for a in args if a.isdigit()]
    if not all_users and not uids:
        print(__doc__)
        return 2

    total_plan = total_done = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            if all_users:
                cur.execute("SELECT id FROM users ORDER BY id")
                uids = [int(r["id"]) for r in cur.fetchall()]
            for uid in uids:
                rows = plan_for_user(cur, uid)
                if not rows:
                    print("uid=%s：无缺口（已是最新账）" % uid)
                    continue
                print("uid=%s：发现 %d 条待补流水" % (uid, len(rows)))
                for x in rows:
                    print("  [?] %s acc=%s %s ¥%.2f %s | %s"
                          % (x["date"], x["account_id"], x["merchant"], x["amount"], x["type"], x["remark"]))
                total_plan += len(rows)
                if do_apply:
                    total_done += apply_rows(cur, rows)
        if do_apply:
            conn.commit()
    print("\n%s：计划 %d 条，已写入 %d 条" % ("APPLY" if do_apply else "DRY-RUN", total_plan, total_done))
    if not do_apply and total_plan:
        print("（预演模式，未改库；确认无误后加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
