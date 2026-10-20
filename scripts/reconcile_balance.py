"""reconcile_balance.py —— 账实一致性对账（增量）

不变量：账户 balance 的变动 = 该账户流水净额（income − expense）的变动。
  · 正常情况：差异 0（含卡密充值、转账、手续费、订阅、理财等所有走流水的变动）。
  · 若有人「绕过流水直接改余额」（如手工 UPDATE accounts SET balance=...），
    对账会报出差异（diff = Δbalance − Δnet）。

用法：
    python scripts/reconcile_balance.py            # 对账并推进基线（0 差异才推进）
    python scripts/reconcile_balance.py --dry-run  # 只报差异，不推进基线
    python scripts/reconcile_balance.py --reset    # 清空基线（下次重建，慎用）

首次运行对每个账户建立基线；已建立基线的账户做增量对账。
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_bank import db

TOL = Decimal("0.001")


def _accounts() -> list[dict]:
    return [dict(r) for r in db.fetch_all(
        "SELECT id, user_id, balance FROM accounts ORDER BY id")]


def _txn_net(account_id: int) -> tuple[Decimal, Decimal]:
    row = db.fetch_one(
        "SELECT "
        "COALESCE(SUM(CASE WHEN type='income' THEN amount ELSE 0 END),0) AS inc, "
        "COALESCE(SUM(CASE WHEN type='expense' THEN amount ELSE 0 END),0) AS exp "
        "FROM transactions WHERE account_id=%s", (account_id,))
    return Decimal(str(row["inc"])), Decimal(str(row["exp"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报差异，不推进基线")
    ap.add_argument("--reset", action="store_true", help="清空基线后重建")
    args = ap.parse_args()

    if args.reset:
        db.execute("DELETE FROM balance_reconcile")
        print("已清空对账基线（下次运行将重建）。")
        return 0

    baselines = {r["account_id"]: dict(r) for r in db.fetch_all(
        "SELECT * FROM balance_reconcile")}
    diffs: list[tuple] = []
    created = 0
    checked = 0

    for acct in _accounts():
        aid = int(acct["id"])
        bal = Decimal(str(acct["balance"]))
        inc, exp = _txn_net(aid)
        base = baselines.get(aid)
        if base is None:
            db.execute(
                "INSERT INTO balance_reconcile (account_id, user_id, snap_balance, "
                "snap_income, snap_expense) VALUES (%s,%s,%s,%s,%s)",
                (aid, int(acct["user_id"]), bal, inc, exp))
            created += 1
            continue
        checked += 1
        d_bal = bal - Decimal(str(base["snap_balance"]))
        d_net = (inc - Decimal(str(base["snap_income"]))) - \
                (exp - Decimal(str(base["snap_expense"])))
        diff = d_bal - d_net
        if abs(diff) > TOL:
            diffs.append((aid, int(acct["user_id"]), bal, d_bal, d_net, diff))
        elif not args.dry_run:
            db.execute(
                "UPDATE balance_reconcile SET snap_balance=%s, snap_income=%s, "
                "snap_expense=%s WHERE account_id=%s", (bal, inc, exp, aid))

    print(f"对账完成：新建基线 {created} 个，增量核对 {checked} 个账户。")
    if diffs:
        print(f"\n[差异] 发现 {len(diffs)} 个账户余额与流水不符（疑似绕过流水的余额改动）：")
        for aid, uid, bal, d_bal, d_net, diff in diffs:
            print(f"  account#{aid} (user#{uid})：余额 {bal}，"
                  f"Δ余额={d_bal}，Δ流水净额={d_net}，差异={diff}")
        return 2
    print("✓ 账实一致（0 差异）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
