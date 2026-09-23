"""
scripts/test_redeem_loop.py —— P0.3 赎回资金闭环 · 验收自测

背景（P0.3 缺陷）：`POST /api/redeem` 修复前只减持仓、**不回款** —— 赎回后资金凭空
消失（前端「资金将按规则回到您的账户」承诺落空、余额明细对不上）。本脚本逐条验收：

  1. 买入：余额 -X，持仓 +1，回执含 balance_after；
  2. 全额赎回：余额 +X（分毫回到买入前），持仓置 redeemed，回执含 balance_after；
  3. ★ 资金守恒：buy(X) + redeem(X) 后余额与初始**完全一致**（闭环）；
  4. 部分赎回：余额 +部分，持仓余额 = 原持仓 - 部分；
  5. 入账口径与买入一致（用户名下 id 最小的非信用账户）；operation_logs 留痕。

测试策略（与既有脚本一致）：直连 mock_bank:8003，登录态 + 一次性操作票据（P0.2）
走真实 ticket 路径；余额断言直接读库（更稳）。结束清理测试持仓，余额自然回到初始。

用法（bank-agent/ 下，.venv，需先启动 mock_bank 于 BANK_API_PORT）：
    .venv/Scripts/python.exe scripts/test_redeem_loop.py
"""
from __future__ import annotations

import os
import sys
import json
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import config
import profile_db
import requests
from scripts._op_test_auth import op_header  # P0.2：写端点需一次性操作票据

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT

UID = 1            # 张小明
PRODUCT_R1 = 1     # 活期+（起购 100，R1，任何风险偏好均可买）

RESULTS: list[tuple[str, bool, str]] = []


def db() -> pymysql.connections.Connection:
    return pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                           password=config.DB_PASSWORD, database=config.DB_NAME, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)


def q1(sql: str, args: tuple = ()) -> dict:
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone() or {}


def avail_balance(uid: int) -> float:
    """可用余额（口径与买入/赎回一致：id 最小的非信用账户）。"""
    row = q1("SELECT balance FROM accounts WHERE user_id=%s "
             "AND COALESCE(account_type,'debit') <> 'credit' ORDER BY id ASC LIMIT 1", (uid,))
    return float(row.get("balance") or 0)


def holding_row(hid: int) -> dict:
    return q1("SELECT id, amount, status FROM holdings WHERE id=%s", (hid,))


def redeem_log_count(uid: int) -> int:
    return int(q1("SELECT COUNT(*) AS n FROM operation_logs WHERE user_id=%s "
                  "AND action='redeem' AND result='success'", (uid,))["n"])


def _h(uid: int, path: str) -> dict:
    h = {"Authorization": f"Bearer {profile_db.issue_token(uid)}"}
    h.update(op_header(uid, path))
    return h


def buy(uid: int, product_id: int, amount: float) -> tuple[int, dict]:
    r = requests.post(MOCK + "/api/products/buy", headers=_h(uid, "/api/products/buy"),
                      json={"product_id": product_id, "amount": amount}, timeout=15)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {"raw": r.text}


def redeem(uid: int, holding_id: int, amount: float) -> tuple[int, dict]:
    r = requests.post(MOCK + "/api/redeem", headers=_h(uid, "/api/redeem"),
                      json={"holding_id": holding_id, "amount": amount}, timeout=15)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {"raw": r.text}


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _cleanup_buy(uid: int, hid: int) -> None:
    """删测试持仓（余额已由赎回还原；持仓行属测试产物）。"""
    with db() as c:
        c.autocommit(True)
        with c.cursor() as cur:
            cur.execute("DELETE FROM holdings WHERE id=%s AND user_id=%s", (hid, uid))


def main() -> int:
    print("=" * 64)
    print(" P0.3 赎回资金闭环 · 验收自测")
    print(f" mock_bank={MOCK}  UID={UID}")
    print("=" * 64)

    # 连通性
    try:
        requests.get(MOCK + "/api/products", timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ mock_bank 不可达：{exc}\n  请先启动：uvicorn mock_bank.main:app --port "
              f"{config.BANK_API_PORT}")
        return 2

    p = q1("SELECT id, name, min_amount, status FROM products WHERE id=%s", (PRODUCT_R1,))
    assert p, f"产品 {PRODUCT_R1} 不存在"
    buy_amt = max(100.0, float(p["min_amount"]))

    # ---- 1) 买入 ----
    print(f"\n[1] 买入「{p['name']}」¥{buy_amt:.0f}")
    bal0 = avail_balance(UID)
    sc, sj = buy(UID, PRODUCT_R1, buy_amt)
    record("买入返回 ok", sc == 200 and sj.get("ok") is True, f"status={sc} {str(sj)[:80]}")
    hid = sj.get("holding_id")
    record("回执含 holding_id", bool(hid), f"holding_id={hid}")
    record("回执 balance_after 与库一致",
           sj.get("balance_after") is not None and abs(float(sj["balance_after"]) - (bal0 - buy_amt)) < 0.01,
           f"{sj.get('balance_after')} vs {bal0 - buy_amt:.2f}")
    record("买入后余额 -X", abs(avail_balance(UID) - (bal0 - buy_amt)) < 0.01,
           f"{bal0:.2f}→{avail_balance(UID):.2f}")
    record("持仓 holding/金额正确",
           holding_row(int(hid)).get("status") == "holding"
           and abs(float(holding_row(int(hid)).get("amount", 0)) - buy_amt) < 0.01)

    # ---- 2) 全额赎回（★闭环核心）----
    print(f"\n[2] 全额赎回持仓 #{hid} ¥{buy_amt:.0f}")
    log0 = redeem_log_count(UID)
    sc2, sj2 = redeem(UID, int(hid), buy_amt)
    record("赎回返回 ok", sc2 == 200 and sj2.get("ok") is True, f"status={sc2} {str(sj2)[:80]}")
    record("★ 赎回资金回到账户（余额 +X，分毫复原）",
           abs(avail_balance(UID) - bal0) < 0.01,
           f"{avail_balance(UID):.2f} vs 初始 {bal0:.2f}")
    record("回执 balance_after == 初始余额",
           sj2.get("balance_after") is not None and abs(float(sj2["balance_after"]) - bal0) < 0.01,
           f"{sj2.get('balance_after')}")
    record("持仓置 redeemed", holding_row(int(hid)).get("status") == "redeemed")
    record("★ 资金守恒（buy+redeem 净变动为 0）", abs(avail_balance(UID) - bal0) < 0.01)
    record("operation_logs 增加 redeem/success 留痕", redeem_log_count(UID) == log0 + 1)

    # ---- 3) 部分赎回 ----
    print("\n[3] 部分赎回：买 ¥200，赎 ¥80")
    sc3, sj3 = buy(UID, PRODUCT_R1, 200.0)
    hid3 = int(sj3.get("holding_id"))
    pre = avail_balance(UID)
    sc4, sj4 = redeem(UID, hid3, 80.0)
    record("部分赎回返回 ok", sc4 == 200 and sj4.get("ok") is True, f"status={sc4}")
    record("余额 +80（仅部分）", abs(avail_balance(UID) - (pre + 80)) < 0.01,
           f"{pre:.2f}→{avail_balance(UID):.2f}")
    record("持仓余额 = 200-80 = 120",
           holding_row(hid3).get("status") == "holding"
           and abs(float(holding_row(hid3).get("amount", 0)) - 120) < 0.01,
           f"amount={holding_row(hid3).get('amount')}")

    # ---- 4) 并发赎回同一持仓：至多 1 笔到账（holdings 行锁防双退）----
    print("\n[4] 并发赎回同一持仓（FOR UPDATE 行锁防双退）")
    pre5 = avail_balance(UID)
    sc5, sj5 = buy(UID, PRODUCT_R1, 300.0)
    hid5 = int(sj5.get("holding_id"))
    res: list[tuple[int, dict]] = []
    lk = threading.Lock()

    def worker() -> None:
        r = redeem(UID, hid5, 300.0)
        with lk:
            res.append(r)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    oks = sum(1 for stt, ss in res if stt == 200 and ss.get("ok"))
    record("并发·至多 1 笔赎回成功", oks == 1, f"success={oks}")
    record("并发·只入账一次（余额恰好回到买入前，非 +600）",
           abs(avail_balance(UID) - pre5) < 0.01,
           f"{pre5:.2f}→{avail_balance(UID):.2f}（买入后应为 {pre5 - 300:.2f}）")

    # ---- 收尾：把剩余赎回掉并删测试持仓，余额回到初始 ----
    redeem(UID, hid3, 120.0)
    _cleanup_buy(UID, int(hid))
    _cleanup_buy(UID, hid3)
    _cleanup_buy(UID, hid5)
    record("收尾后余额回到初始（净副作用 0）", abs(avail_balance(UID) - bal0) < 0.01,
           f"{avail_balance(UID):.2f} vs {bal0:.2f}")

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f" 结果：{passed}/{total} 通过")
    fails = [n for n, ok, _ in RESULTS if not ok]
    if fails:
        print(" 未通过：")
        for f in fails:
            print("   -", f)
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
