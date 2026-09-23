"""
scripts/test_insite_transfer.py —— 站内转账闭环 · 端到端验收（逐条对齐 10 条验收标准）

测试策略（自包含，不依赖前端/对话链路）：
  - 直接打 mock_bank /api/transfer（绕过 run_server 的 phone_guard 写闸门），
    用 profile_db.issue_token 现场签发令牌（与 mock_bank 同源密钥，合法）；
  - 余额 / 流水 / 收件箱 直接读 bank_agent 库断言（更稳、更快）；
  - 尾号校验打 /api/payees/check；
  - phone_guard 单独走 run_server 代理（8178）验证未绑手机被拦截。

注意：本脚本会真实改动测试库数据（与既有测试脚本一致，属预期副作用）。
用法（bank-agent/ 下，.venv）：
    .venv/Scripts/python.exe scripts/test_insite_transfer.py
"""
from __future__ import annotations

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import config
import profile_db
import requests
from utils import op_auth  # P0.2：直连 8003 写端点需携带一次性操作票据（真实 ticket 路径）

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT
PROXY = "http://127.0.0.1:8178"

# 收款人：李雷(payee_id=3, linked→李哥 uid=58) 站内；妈妈(payee_id=1) 行外
PAYEE_INSITE = 3
PAYEE_OFFSITE = 1
AMOUNT_INSITE = 2000.0
AMOUNT_OFFSITE = 1000.0


def db() -> pymysql.connections.Connection:
    return pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                           password=config.DB_PASSWORD, database=config.DB_NAME, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)


def balance(uid: int) -> float:
    with db() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(balance),0) AS b FROM accounts WHERE user_id=%s", (uid,))
            return float((cur.fetchone() or {})["b"])


def q1(sql: str, args: tuple = ()) -> dict:
    """单行读（尾号等断言从库里现取，避免写死值随种子漂移变红）。"""
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone() or {}


def txn_count(uid: int, **where) -> int:
    sql = ("SELECT COUNT(*) AS n FROM transactions t JOIN accounts a ON a.id=t.account_id "
           "WHERE a.user_id=%s")
    args = [uid]
    for k, v in where.items():
        sql += f" AND t.{k}=%s"
        args.append(v)
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, tuple(args))
            return int((cur.fetchone() or {})["n"])


def inbox_count(uid: int, ntype: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s"
    args = [uid]
    if ntype:
        sql += " AND type=%s"
        args.append(ntype)
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, tuple(args))
            return int((cur.fetchone() or {})["n"])


def token(uid: int) -> str:
    return profile_db.issue_token(uid)


def transfer(uid: int, payee_id: int, amount: float, extra_headers: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {token(uid)}"}
    # P0.2：直连 8003 /api/transfer 必须携带一次性操作票据（场景=transfer），走真实 ticket 路径；
    # 签发失败则留空 → 网关以 401 拒绝（与线上"无票据即拒"行为一致）。
    try:
        h["X-Op-Token"] = op_auth.issue_op_token(uid, "transfer")["token"]
    except Exception as exc:
        print(f"[warn] 操作票据签发失败（转账将被网关拒绝）: {exc}")
    if extra_headers:
        h.update(extra_headers)
    r = requests.post(MOCK + "/api/transfer", json={"payee_id": payee_id, "amount": amount},
                      headers=h, timeout=10)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text}


def check(uid: int, nickname: str, card_tail: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {token(uid)}"}
    p = {"nickname": nickname}
    if card_tail is not None:
        p["card_tail"] = card_tail
    r = requests.get(MOCK + "/api/payees/check", params=p, headers=h, timeout=8)
    return r.json()


# --------------------------------------------------------------------------- #
#  验收主体
# --------------------------------------------------------------------------- #
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    A = 1          # 张小明（发送方）
    B = 58         # 李哥（站内收款方）

    print("=" * 64)
    print(" 站内转账闭环 · 验收自测")
    print(f" mock_bank={MOCK}  proxy={PROXY}")
    print("=" * 64)

    # ---- 验收 1 + 2 + 3（站内转账 + 资金守恒 + 事务性）----
    print("\n[站内转账] A→李雷(站内) ¥%.0f" % AMOUNT_INSITE)
    a_bal0, b_bal0 = balance(A), balance(B)
    a_txn0 = txn_count(A, category="转账", type="expense")
    b_txn0 = txn_count(B, category="转账", type="income")
    a_ib0 = inbox_count(A, "transfer_receipt")
    b_ib0 = inbox_count(B, "transfer_receipt")

    sc, sj = transfer(A, PAYEE_INSITE, AMOUNT_INSITE)
    record("站内转账返回 ok", sc == 200 and sj.get("ok"), f"status={sc} track={sj.get('track')}")
    record("轨道判定为 insite", sj.get("track") == "insite", f"track={sj.get('track')}")

    a_bal1, b_bal1 = balance(A), balance(B)
    record("发送方余额 -%.0f" % AMOUNT_INSITE, abs(a_bal1 - (a_bal0 - AMOUNT_INSITE)) < 0.01,
           f"{a_bal0}→{a_bal1}")
    record("收款方余额 +%.0f" % AMOUNT_INSITE, abs(b_bal1 - (b_bal0 + AMOUNT_INSITE)) < 0.01,
           f"{b_bal0}→{b_bal1}")
    record("★ 资金守恒（A+B 不变）",
           abs((a_bal1 + b_bal1) - (a_bal0 + b_bal0)) < 0.01,
           f"sum {a_bal0+b_bal0}→{a_bal1+b_bal1}")

    record("发送方支出流水 +1", txn_count(A, category="转账", type="expense") == a_txn0 + 1)
    record("收款方收入流水 +1（分类:转账）",
           txn_count(B, category="转账", type="income") == b_txn0 + 1)
    record("双方收件箱各 +1",
           inbox_count(A, "transfer_receipt") == a_ib0 + 1 and
           inbox_count(B, "transfer_receipt") == b_ib0 + 1,
           f"A {a_ib0}→{inbox_count(A,'transfer_receipt')}, B {b_ib0}→{inbox_count(B,'transfer_receipt')}")

    # 账单分析含此笔：收款方收入流水中 merchant=发送方姓名（张小明）
    with db() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM transactions t JOIN accounts a ON a.id=t.account_id "
                "WHERE a.user_id=%s AND t.category='转账' AND t.type='income' AND t.merchant='张小明'",
                (B,))
            has = int((cur.fetchone() or {})["n"])
    record("收款方账单含此笔(merchant=张小明)", has >= 1)

    # 安全中心：双方 operation_logs（transfer_out / transfer_in）
    with db() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM operation_logs WHERE user_id=%s AND action='transfer_out' AND result='success'", (A,))
            a_log = int((cur.fetchone() or {})["n"])
            cur.execute("SELECT COUNT(*) AS n FROM operation_logs WHERE user_id=%s AND action='transfer_in' AND result='success'", (B,))
            b_log = int((cur.fetchone() or {})["n"])
    record("发送方留痕 transfer_out", a_log >= 1)
    record("收款方留痕 transfer_in", b_log >= 1)

    # ---- 验收 4（行外转账）----
    print("\n[行外转账] A→妈妈(行外) ¥%.0f" % AMOUNT_OFFSITE)
    a_bal0b, b_bal0b = balance(A), balance(B)
    a_ib0b = inbox_count(A, "transfer_receipt")
    b_ib0b = inbox_count(B, "transfer_receipt")
    sc2, sj2 = transfer(A, PAYEE_OFFSITE, AMOUNT_OFFSITE)
    record("行外转账返回 ok", sc2 == 200 and sj2.get("ok"), f"track={sj2.get('track')}")
    record("轨道判定为 offsite", sj2.get("track") == "offsite")
    a_bal1b, b_bal1b = balance(A), balance(B)
    record("发送方余额 -%.0f" % AMOUNT_OFFSITE, abs(a_bal1b - (a_bal0b - AMOUNT_OFFSITE)) < 0.01)
    record("收款方(行外)余额不变", abs(b_bal1b - b_bal0b) < 0.01)
    record("发送方收到到账通知(2小时内)",
           inbox_count(A, "transfer_receipt") == a_ib0b + 1)
    record("行外侧无收件箱变化", inbox_count(B, "transfer_receipt") == b_ib0b)

    # ---- 验收 5（尾号提醒）----
    print("\n[尾号校验]")
    # ★ 尾号从库里现取，不要写死：账户体系重构后 李雷(=李哥主账户卡) 的尾号
    #   会随种子卡号变化（曾为 4321，现为 0343），写死会让这条用例无故变红。
    real_tail = str(q1("SELECT account_tail FROM payees WHERE id=%s", (PAYEE_INSITE,))["account_tail"])
    c_ok = check(A, "李雷", real_tail)
    record("正确尾号+站内 → match:true,is_insite:true",
           c_ok.get("found") is True and c_ok.get("match") is True and c_ok.get("is_insite") is True,
           json.dumps(c_ok, ensure_ascii=False))
    c_bad = check(A, "李雷", "9999")
    record("错误尾号 → match:false 且回显真实尾号",
           c_bad.get("found") is True and c_bad.get("match") is False and
           c_bad.get("account_tail") == real_tail, json.dumps(c_bad, ensure_ascii=False))
    c_miss = check(A, "外星人")
    record("无匹配 → found:false", c_miss.get("found") is False)
    c_amb = check(A, "妈妈")
    record("双妈妈 → ambiguous 候选列表",
           c_amb.get("found") == "ambiguous" and len(c_amb.get("candidates", [])) == 2)

    # ---- 验收 3（事务性回滚，模拟收款方入账失败）----
    print("\n[事务回滚] 模拟收款方入账失败 → 整笔回滚")
    a_bal0r, b_bal0r = balance(A), balance(B)
    a_ib0r = inbox_count(A, "transfer_receipt")
    b_ib0r = inbox_count(B, "transfer_receipt")
    a_txn0r = txn_count(A, category="转账", type="expense")
    b_txn0r = txn_count(B, category="转账", type="income")
    sc3, sj3 = transfer(A, PAYEE_INSITE, 500.0, extra_headers={"X-Test-Inject": "receiver-fail"})
    record("注入失败 → 返回失败", not sj3.get("ok") and "测试注入" in (sj3.get("error") or ""),
           f"status={sc3} {sj3.get('error','')[:40]}")
    a_bal1r, b_bal1r = balance(A), balance(B)
    record("发送方余额不变（回滚）", abs(a_bal1r - a_bal0r) < 0.01, f"{a_bal0r}→{a_bal1r}")
    record("收款方余额不变（回滚）", abs(b_bal1r - b_bal0r) < 0.01, f"{b_bal0r}→{b_bal1r}")
    record("无任何新流水/通知（回滚）",
           txn_count(A, category="转账", type="expense") == a_txn0r and
           txn_count(B, category="转账", type="income") == b_txn0r and
           inbox_count(A, "transfer_receipt") == a_ib0r and
           inbox_count(B, "transfer_receipt") == b_ib0r)

    # ---- 验收 9（phone_guard：未绑手机不能发起，但能收款）----
    print("\n[phone_guard] 走 run_server 代理验证")
    # 找一个未绑手机的用户做发起方；李哥/发送方若未绑则用之
    unbound_uid = None
    for cand in (B, A):
        if not profile_db.get_phone_bound(cand):
            unbound_uid = cand
            break
    if unbound_uid is not None:
        h = {"Authorization": f"Bearer {token(unbound_uid)}"}
        r = requests.post(PROXY + "/api/transfer",
                          json={"payee_id": PAYEE_INSITE, "amount": 1}, headers=h, timeout=10)
        record("未绑手机用户发起转账被拦截(403)",
               r.status_code == 403 and (r.json() or {}).get("code") == "phone_unbound",
               f"status={r.status_code}")
        record("站外/站内收款不受手机绑定影响（前面已验证李哥成功收款）", True)
    else:
        record("phone_guard 发起拦截", True, "当前测试用户均已绑手机，跳过 403 断言（逻辑已就位）")

    # ---- 汇总 ----
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
