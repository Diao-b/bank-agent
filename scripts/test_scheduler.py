"""
scripts/test_scheduler.py —— P1.2 独立调度器 · 验收自测

背景：订阅到期补扣 / 跨行到期清算原先只靠「访问读接口」惰性触发 —— 用户不访问就不推进。
本脚本验证新增的 `utils/scheduler.py` 能在**没有任何 HTTP 读请求**的情况下推进到期业务：

  1. 跨行到期清算：受理一笔在途转账 → 拨快到过去 → 仅调 `scheduler.run_once()`（不调任何读接口）
     → 在途流水自动 settle（到账）；
  2. 订阅到期补扣：签约一笔订阅 → next_due 拨到昨天 → 仅调 `run_once()` → 出现自动扣费流水；
  3. 幂等：连续两轮 run_once 不重复扣款 / 不重复清算；
  4. 后台线程：以 1s 间隔 start() 后，状态显示 running 且轮次自增（无人访问也在跑），随后 stop()；
  5. 只读状态接口：`GET /api/housekeeping`（run_server:8178）暴露调度器状态。

用法（bank-agent/ 下，.venv，需先启动 mock_bank 于 BANK_API_PORT）：
    .venv/Scripts/python.exe scripts/test_scheduler.py
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import config
import profile_db
import requests
from scripts._op_test_auth import op_header
from utils import scheduler

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT
PROXY = "http://127.0.0.1:8178"
UID = 1
TEST_MERCHANT = "自测·调度器订阅"
AMOUNT = 10.0

RESULTS: list[tuple[str, bool, str]] = []


def db() -> pymysql.connections.Connection:
    c = pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                        password=config.DB_PASSWORD, database=config.DB_NAME,
                        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    c.autocommit(True)
    return c


def q1(sql: str, args: tuple = ()) -> dict:
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone() or {}


def exec_sql(sql: str, args: tuple = ()) -> int:
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return cur.rowcount


def hdr(uid: int, path: str) -> dict:
    h = {"Authorization": f"Bearer {profile_db.issue_token(uid)}"}
    h.update(op_header(uid, path))
    return h


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _crossbank_payee() -> int | None:
    row = q1("SELECT p.id FROM payees p JOIN external_accounts e ON e.card_no=p.card_no "
             "WHERE p.user_id=%s AND p.linked_user_id IS NULL ORDER BY p.id LIMIT 1", (UID,))
    return int(row["id"]) if row else None


def _acct_id() -> int:
    return int(q1("SELECT id FROM accounts WHERE user_id=%s AND COALESCE(account_type,'debit')<>'credit' "
                  "ORDER BY id ASC LIMIT 1", (UID,))["id"])


def _latest_txn(acct: int, category: str, ttype: str) -> dict:
    return q1("SELECT id, settle_status, amount FROM transactions WHERE account_id=%s "
              "AND category=%s AND type=%s ORDER BY id DESC LIMIT 1", (acct, category, ttype))


def _txn_count(merchant: str) -> int:
    return int(q1("SELECT COUNT(*) AS n FROM transactions t JOIN accounts a ON a.id=t.account_id "
                  "WHERE a.user_id=%s AND t.merchant=%s", (UID, merchant))["n"])


def main() -> int:
    print("=" * 66)
    print(" P1.2 独立调度器 · 验收自测")
    print(f" mock_bank={MOCK}  UID={UID}  interval={config.HOUSEKEEPING_INTERVAL_SECONDS}s")
    print("=" * 66)

    try:
        requests.get(MOCK + "/api/products", timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ mock_bank 不可达：{exc}")
        return 2

    # ---------------- 1) 跨行到期清算：仅靠调度器推进 ----------------
    print("\n[1] 跨行到期清算：受理在途 → 拨快到期 → 只跑 run_once()（不调读接口）")
    pid = _crossbank_payee()
    if pid is None:
        record("存在可清算的跨行收款人", False, "库内无已登记跨行收款人（请先 seed）")
    else:
        acct = _acct_id()
        r = requests.post(MOCK + "/api/transfer", headers=hdr(UID, "/api/transfer"),
                          json={"payee_id": pid, "amount": AMOUNT}, timeout=10)
        body = r.json()
        record("跨行转账受理（在途）", r.status_code == 200 and body.get("ok")
               and body.get("track") == "offsite", f"track={body.get('track')}")
        txn = _latest_txn(acct, "转账", "expense")
        tid = int(txn["id"])
        record("原流水在途 pending", txn.get("settle_status") == "pending")
        exec_sql("UPDATE pending_settlements SET expected_at=DATE_SUB(NOW(), INTERVAL 1 MINUTE) "
                 "WHERE txn_id=%s AND status='pending'", (tid,))
        # ★ 关键：只调调度器，不访问任何读接口
        summ = scheduler.run_once()
        after = q1("SELECT settle_status FROM transactions WHERE id=%s", (tid,))
        record("★ 仅调度器推进 → 在途流水自动到账",
               after.get("settle_status") == "settled", f"settle={after.get('settle_status')} "
               f"run_once={summ}")
        nt = q1("SELECT type, title FROM notifications WHERE user_id=%s AND type='transfer_receipt' "
                "ORDER BY id DESC LIMIT 1", (UID,))
        record("到账通知已落收件箱", bool(nt), f"{nt.get('title')}")

    # ---------------- 2) 订阅到期补扣：仅靠调度器推进 ----------------
    print("\n[2] 订阅到期补扣：签约 → next_due 拨昨天 → 只跑 run_once()")
    rc = requests.post(MOCK + "/api/subscriptions/contract", headers=hdr(UID, "/api/subscriptions/contract"),
                       json={"service_name": TEST_MERCHANT, "fee": AMOUNT, "cycle": "月付"}, timeout=10)
    record("签约成功", rc.status_code == 200 and rc.json().get("ok"), str(rc.json())[:100])
    before = _txn_count(TEST_MERCHANT)
    exec_sql("UPDATE subscriptions SET next_due=%s WHERE user_id=%s AND merchant=%s",
             (date.today() - timedelta(days=1), UID, TEST_MERCHANT))
    summ2 = scheduler.run_once()
    after_n = _txn_count(TEST_MERCHANT)
    record("★ 仅调度器推进 → 出现自动扣费流水", after_n > before,
           f"{before}→{after_n} run_once={summ2}")

    # ---------------- 3) 幂等：连续两轮不重复 ----------------
    print("\n[3] 幂等：再来一轮 run_once 不重复扣款 / 不重复清算")
    n_before = _txn_count(TEST_MERCHANT)
    settled_before = int(q1("SELECT COUNT(*) AS n FROM transactions t JOIN accounts a ON a.id=t.account_id "
                            "WHERE a.user_id=%s AND t.settle_status='settled'", (UID,))["n"])
    scheduler.run_once()
    record("订阅不重复扣款", _txn_count(TEST_MERCHANT) == n_before)
    record("已清算流水不重复清算",
           int(q1("SELECT COUNT(*) AS n FROM transactions t JOIN accounts a ON a.id=t.account_id "
                  "WHERE a.user_id=%s AND t.settle_status='settled'", (UID,))["n"]) == settled_before)

    # ---------------- 4) 后台线程：无人访问也在跑 ----------------
    print("\n[4] 后台线程：1s 间隔启动后轮次自增")
    config.HOUSEKEEPING_INTERVAL_SECONDS = 1
    started = scheduler.start()
    runs0 = scheduler.status()["runs"]
    time.sleep(2.6)
    st = scheduler.status()
    scheduler.stop()
    record("调度器 start() 成功", started or st["running"], f"running={st['running']}")
    record("★ 线程自动执行（runs 自增）", st["runs"] > runs0, f"{runs0}→{st['runs']}")
    record("状态含最近一轮汇总",
           st.get("last_run_at") is not None and st.get("last_duration_ms") is not None,
           f"last_run={st.get('last_run_at')} dur={st.get('last_duration_ms')}ms")

    # ---------------- 5) 只读状态接口（8178）----------------
    print("\n[5] 只读状态接口 GET /api/housekeeping")
    try:
        rs = requests.get(PROXY + "/api/housekeeping", timeout=5)
        ok = rs.status_code == 200 and rs.json().get("ok") is True and "interval_seconds" in rs.json()
        record("run_server /api/housekeeping 可达", ok, str(rs.json())[:120])
    except Exception as exc:  # noqa: BLE001
        record("run_server /api/housekeeping 可达", False, f"8178 未启动？{exc}")

    # ---------------- 收尾 ----------------
    with db() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE merchant=%s AND account_id IN "
                        "(SELECT id FROM accounts WHERE user_id=%s)", (TEST_MERCHANT, UID))
            cur.execute("DELETE FROM subscriptions WHERE user_id=%s AND merchant=%s", (UID, TEST_MERCHANT))
            cur.execute("DELETE FROM operation_logs WHERE user_id=%s AND detail LIKE %s",
                        (UID, f"%{TEST_MERCHANT}%"))
    print("\n（测试订阅与流水已清理；跨行到账属预期副作用）")

    print("\n" + "=" * 66)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f" 结果：{passed}/{total} 通过")
    for n, ok, _ in RESULTS:
        if not ok:
            print("   -", n)
    print("=" * 66)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
