"""运维脚本：修复「演示账户被测试残留持仓抽干」的问题。

背景
----
`scripts/test_wealth_suitability.py` 的买入会把钱从用户主力账户（如 user1 的
「微众卡（主账户）」）划到 `holdings`，但该用例并不保证每次买入都赎回。
反复运行后，可用余额被一点点抽干，最终 `test_insite_transfer` 因
「余额不足」失败（表现是站内/跨行转账全部返回 ok=false）。

本脚本把符合"测试残留"特征的持仓**走真实赎回 API** 赎回 —— 本金按 P0.3 的
口径退回用户名下 id 最小的非信用账户。**资金总量不变**，只是把"躺在持仓里的
演示余额"还原成"可用余额"。

特征（刻意保守，宁少不多）
--------------------------
  * user_id = --uid（默认 1）
  * status  = 'holding'
  * id      >= --min-id（默认 11）
      ★ holdings.id 1..10 是 `sql/init.sql` 的种子持仓，**绝不触碰**。
  * amount  ∈ (100, 1000)   ← 适当性用例只买这两种面额

用法
----
    python scripts/repair_demo_state.py                # 只列清单（dry-run，默认）
    python scripts/repair_demo_state.py --apply        # 确认后真正赎回

前置：mock_bank 在 8003 运行。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql  # noqa: E402
import requests  # noqa: E402

import config  # noqa: E402
import profile_db  # noqa: E402
from utils import op_auth  # noqa: E402

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT
SEED_MAX_ID = 10                      # 种子持仓 id 上界（绝不动）
TEST_AMOUNTS = ("100.00", "1000.00")  # 适当性用例的买入面额


def conn():
    cfg = dict(config.db_config())
    cfg["cursorclass"] = pymysql.cursors.DictCursor
    return pymysql.connect(**cfg)


def residue(uid: int, min_id: int) -> list[dict]:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id, product_id, amount, status FROM holdings "
                "WHERE user_id=%s AND status='holding' AND id>=%s AND amount IN %s "
                "ORDER BY id",
                (uid, max(min_id, SEED_MAX_ID + 1), TEST_AMOUNTS),
            )
            return list(cur.fetchall())


def balances(uid: int) -> list[dict]:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT id, name, account_type, balance FROM accounts "
                "WHERE user_id=%s ORDER BY id", (uid,))
            return list(cur.fetchall())


def redeem(uid: int, holding_id: int, amount: str) -> tuple[bool, str]:
    tok = profile_db.issue_token(uid)
    op = op_auth.issue_op_token(uid, "redeem", purpose="repair-demo-state")
    if not op.get("ok"):
        return False, f"签票失败：{op.get('error')}"
    try:
        r = requests.post(
            MOCK + "/api/redeem",
            headers={"Authorization": "Bearer " + tok, "X-Op-Token": op["token"]},
            json={"holding_id": holding_id, "amount": float(amount)},
            timeout=15,
        )
        body = r.json() if r.content else {}
    except Exception as exc:  # noqa: BLE001
        return False, f"请求异常：{exc}"
    ok = bool(body.get("ok"))
    return ok, body.get("message") or body.get("error") or f"HTTP {r.status_code}"


def main() -> int:
    ap = argparse.ArgumentParser(description="还原被测试残留持仓抽干的演示账户余额")
    ap.add_argument("--uid", type=int, default=1, help="用户 id（默认 1 = user1 演示账号）")
    ap.add_argument("--min-id", type=int, default=SEED_MAX_ID + 1,
                    help=f"持仓 id 下界（默认 {SEED_MAX_ID + 1}，种子持仓 1..{SEED_MAX_ID} 永不触碰）")
    ap.add_argument("--apply", action="store_true", help="真正执行赎回（默认只 dry-run）")
    args = ap.parse_args()

    print(f"目标：uid={args.uid}  持仓 id>={max(args.min_id, SEED_MAX_ID + 1)}  "
          f"面额 {TEST_AMOUNTS}  status=holding")
    print("\n--- 修复前账户余额 ---")
    for b in balances(args.uid):
        print(f"  #{b['id']:<4} {b['name']:<18} {b['account_type']:<8} {b['balance']}")

    rows = residue(args.uid, args.min_id)
    total = sum(float(r["amount"]) for r in rows)
    print(f"\n--- 命中测试残留持仓：{len(rows)} 笔，合计 ¥{total:.2f} ---")
    for r in rows:
        print(f"  holding #{r['id']:<5} product={r['product_id']:<4} {float(r['amount']):>10.2f}")

    if not rows:
        print("\n无残留，无需修复。")
        return 0
    if not args.apply:
        print(f"\n[dry-run] 未做任何变更。确认后执行："
              f"\n  .venv/Scripts/python.exe scripts/repair_demo_state.py --uid {args.uid} --apply")
        return 0

    print("\n--- 走真实赎回 API 赎回 ---")
    ok_n = 0
    for r in rows:
        ok, msg = redeem(args.uid, int(r["id"]), r["amount"])
        print(f"  #{r['id']:<5} {'OK ' if ok else 'FAIL'} {msg[:90]}")
        ok_n += 1 if ok else 0

    print(f"\n--- 修复后账户余额（成功赎回 {ok_n}/{len(rows)}）---")
    for b in balances(args.uid):
        print(f"  #{b['id']:<4} {b['name']:<18} {b['account_type']:<8} {b['balance']}")
    return 0 if ok_n == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
