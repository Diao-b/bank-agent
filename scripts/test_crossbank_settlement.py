"""
scripts/test_crossbank_settlement.py —— 跨行转账「在途 · 清算 · 退票」端到端验收

测试策略（自包含，不依赖前端/对话链路）：
  - 直接打 mock_bank /api/transfer（绕过 run_server 的 phone_guard 写闸门），
    用 profile_db.issue_token 现场签发令牌（与 mock_bank 同源密钥，合法）；
  - 余额 / 流水 / 收件箱 / 清算台账 直接读 bank_agent 库断言（更稳、更快）；
  - 惰性清算触发：把 pending_settlements.expected_at 改到过去，再访问
    GET /api/transactions —— 证明"访问任意数据接口即触发清算"（验收 #1）；
  - 「幽灵卡号」用测试脚本构造：直接向 payees 插一行"未走新增流程"的收款人
    （因此不会登记进 external_accounts），再按正常路径转账 → 必然退票。

注意：本脚本会真实改动测试库数据（与既有测试脚本一致，属预期副作用）。
用法（bank-agent/ 下，.venv）：
    .venv/Scripts/python.exe scripts/test_crossbank_settlement.py
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

from utils import card_validator, settlement_engine

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT

A = 1             # 张小明（付款方，主账户 id=1 / is_primary=1）
PAYEE_MOM = 1     # 妈妈·招行（已登记 external_accounts → 应到账）
PAYEE_INSITE = 3  # 李雷（站内 → 即时到账，不受影响）


# --------------------------------------------------------------------------- #
#  DB 工具
# --------------------------------------------------------------------------- #
def db() -> pymysql.connections.Connection:
    return pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                           password=config.DB_PASSWORD, database=config.DB_NAME, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)


def q1(sql: str, args: tuple = ()) -> dict:
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone() or {}


def exec_sql(sql: str, args: tuple = ()) -> int:
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            n = cur.rowcount
        c.commit()
    return n


def balance(uid: int) -> float:
    """可用余额合计（排除信用账户）：与 /api/transfer 与退款落点同口径。"""
    with db() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(balance),0) AS b FROM accounts "
                        "WHERE user_id=%s AND COALESCE(account_type,'debit') <> 'credit'", (uid,))
            return float((cur.fetchone() or {})["b"])


def account_balance(acc_id: int) -> float:
    return float(q1("SELECT balance FROM accounts WHERE id=%s", (acc_id,))["balance"])


def txn(account_id: int, **where) -> dict:
    sql = "SELECT * FROM transactions WHERE account_id=%s"
    args = [account_id]
    for k, v in where.items():
        sql += f" AND {k}=%s"
        args.append(v)
    sql += " ORDER BY id DESC LIMIT 1"
    return q1(sql, tuple(args))


def txn_count(uid: int, **where) -> int:
    sql = ("SELECT COUNT(*) AS n FROM transactions t JOIN accounts a ON a.id=t.account_id "
           "WHERE a.user_id=%s")
    args = [uid]
    for k, v in where.items():
        sql += f" AND t.{k}=%s"
        args.append(v)
    return int(q1(sql, tuple(args))["n"])


def inbox_count(uid: int, ntype: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s"
    args = [uid]
    if ntype:
        sql += " AND type=%s"
        args.append(ntype)
    return int(q1(sql, tuple(args))["n"])


def latest_notification(uid: int, ntype: str) -> dict:
    return q1("SELECT * FROM notifications WHERE user_id=%s AND type=%s ORDER BY id DESC LIMIT 1",
              (uid, ntype))


# --------------------------------------------------------------------------- #
#  HTTP 工具
# --------------------------------------------------------------------------- #
def token(uid: int) -> str:
    return profile_db.issue_token(uid)


def hdr(uid: int) -> dict:
    return {"Authorization": f"Bearer {token(uid)}"}


def transfer(uid: int, payee_id: int, amount: float) -> dict:
    h = hdr(uid)
    # P0.2：直连 8003 /api/transfer 必须携带一次性操作票据（场景=transfer），走真实 ticket 路径
    try:
        h["X-Op-Token"] = op_auth.issue_op_token(uid, "transfer")["token"]
    except Exception as exc:
        print(f"[warn] 操作票据签发失败（转账将被网关拒绝）: {exc}")
    r = requests.post(MOCK + "/api/transfer", json={"payee_id": payee_id, "amount": amount},
                      headers=h, timeout=10)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text}


def create_payee(uid: int, nickname: str, bank_id: int, card_no: str, real_name: str) -> dict:
    r = requests.post(MOCK + "/api/payees/create",
                      json={"payee_name": nickname, "bank_id": bank_id,
                            "card_no": card_no, "real_name": real_name},
                      headers=hdr(uid), timeout=10)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text}


def return_history(uid: int, payee_id: int) -> dict:
    r = requests.get(MOCK + "/api/settlements/return-history",
                     params={"payee_id": payee_id}, headers=hdr(uid), timeout=8)
    return r.json()


def trigger_lazy_settlement(uid: int) -> dict:
    """访问任意数据接口 → 触发惰性 housekeeping（跨行到期清算）。"""
    r = requests.get(MOCK + "/api/transactions", headers=hdr(uid), timeout=10)
    return r.json()


def force_due(txn_id: int) -> None:
    """把某笔在途清算的 expected_at 改到过去（免等 5 分钟，模拟"时间到了"）。"""
    exec_sql("UPDATE pending_settlements SET expected_at = DATE_SUB(NOW(), INTERVAL 1 MINUTE) "
             "WHERE txn_id=%s AND status='pending'", (txn_id,))


def pending_row(txn_id: int) -> dict:
    return q1("SELECT * FROM pending_settlements WHERE txn_id=%s", (txn_id,))


def ext_has(card_no: str) -> bool:
    return bool(q1("SELECT 1 AS x FROM external_accounts WHERE card_no=%s", (card_no,)))


# --------------------------------------------------------------------------- #
#  测试辅助：造一个「幽灵卡号」收款人（未走新增流程 → 不登记 external_accounts）
# --------------------------------------------------------------------------- #
def ensure_ghost_payee() -> tuple[int, str]:
    """返回 (payee_id, card_no)。重复运行幂等（同卡号复用）。"""
    card = card_validator.generate_card_no("6225", seq=9182736)   # 招行 BIN，Luhn 合法
    assert card_validator.luhn_check(card) and card_validator.bank_from_card(card) == "CMBC"
    row = q1("SELECT id FROM payees WHERE user_id=%s AND card_no=%s", (A, card))
    if row:
        # 保证它绝不在可清算库里（模拟"未核验账户"）
        exec_sql("DELETE FROM external_accounts WHERE card_no=%s", (card,))
        return int(row["id"]), card
    exec_sql(
        "INSERT INTO payees (user_id, nickname, real_name, bank, bank_id, card_no, "
        "account_tail, linked_user_id, account_id, is_frequent) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, NULL, 0)",
        (A, "幽灵卡号（验收测试）", "错误户名", "招商银行", 6, card, card[-4:]),
    )
    exec_sql("DELETE FROM external_accounts WHERE card_no=%s", (card,))
    pid = int(q1("SELECT id FROM payees WHERE user_id=%s AND card_no=%s", (A, card))["id"])
    return pid, card


RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 68)
    print(" 跨行转账「在途 · 清算 · 退票」· 验收自测")
    print(f" mock_bank={MOCK}")
    print("=" * 68)

    # ===================================================================== #
    #  验收 1：向已登记外部账户（妈妈·招行）转账 → 在途 → 触发清算 → 到账
    # ===================================================================== #
    print("\n[验收1] 跨行到账闭环：A → 妈妈(招行·已登记) ¥500")
    bal0 = balance(A)
    ib_receipt0 = inbox_count(A, "transfer_receipt")
    refund_base = txn_count(A, category="转账退回")   # ★ 相对基线：脚本可重复运行
    sc, sj = transfer(A, PAYEE_MOM, 500.0)
    record("跨行转账返回 ok + 轨道 offsite", sc == 200 and sj.get("ok") is True
           and sj.get("track") == "offsite", f"status={sc} track={sj.get('track')}")
    record("响应标记 settle_status='pending'", sj.get("settle_status") == "pending",
           f"settle_status={sj.get('settle_status')}")

    bal1 = balance(A)
    record("在途期余额 -500（资金已离开可用余额）", abs(bal1 - (bal0 - 500)) < 0.01,
           f"{bal0}→{bal1}")

    t_sender = txn(1, category="转账", type="expense")
    record("原流水 settle_status='pending'（在途）", t_sender.get("settle_status") == "pending",
           f"txn#{t_sender.get('id')} settle={t_sender.get('settle_status')}")
    prow = pending_row(int(t_sender["id"]))
    record("已建 pending_settlements 在途台账",
           bool(prow) and prow.get("status") == "pending", json.dumps(prow, default=str)[:120])

    nt = latest_notification(A, "transfer_receipt")
    record("受理通知（不写成功回执）",
           "受理" in (nt.get("title") or "") and "清算中" in (nt.get("content") or ""),
           f"title={nt.get('title')}")
    record("受理通知数 +1", inbox_count(A, "transfer_receipt") == ib_receipt0 + 1)

    # 触发清算
    force_due(int(t_sender["id"]))
    trigger_lazy_settlement(A)
    t_sender2 = txn(1, category="转账", type="expense")
    record("★ 触发后原流水 settle_status='settled'（到账）",
           t_sender2.get("settle_status") == "settled", f"settle={t_sender2.get('settle_status')}")
    record("在途台账置 done", pending_row(int(t_sender["id"])).get("status") == "done")
    nt2 = latest_notification(A, "transfer_receipt")
    record("到账通知（含流水号）",
           "到账" in (nt2.get("title") or "") and str(sj.get("serial_no")) in (nt2.get("content") or ""),
           f"title={nt2.get('title')}")
    record("到账不产生退款流水", txn_count(A, category="转账退回") == refund_base)
    record("到账后余额不回滚（钱已到对方）", abs(balance(A) - bal1) < 0.01)

    # ===================================================================== #
    #  验收 2 + 3：幽灵卡号 → 退票全额退回 + 金额守恒
    # ===================================================================== #
    print("\n[验收2/3] 退票闭环：幽灵卡号（未登记）¥500 → 全额退回")
    ghost_id, ghost_card = ensure_ghost_payee()
    record("幽灵卡号 Luhn 合法但不在可清算库",
           card_validator.luhn_check(ghost_card) and not ext_has(ghost_card),
           f"card={ghost_card}")

    bal_g0 = balance(A)
    refund0 = txn_count(A, category="转账退回")
    scg, sjg = transfer(A, ghost_id, 500.0)
    record("幽灵卡号转账受理（在途，不立即失败）",
           scg == 200 and sjg.get("ok") is True and sjg.get("settle_status") == "pending",
           f"track={sjg.get('track')}")
    bal_g1 = balance(A)
    record("在途期余额 -500（X → X-500）", abs(bal_g1 - (bal_g0 - 500)) < 0.01,
           f"{bal_g0}→{bal_g1}")
    g_tx = txn(1, category="转账", type="expense")
    record("原流水在途 pending", g_tx.get("settle_status") == "pending")

    force_due(int(g_tx["id"]))
    trigger_lazy_settlement(A)

    g_tx2 = txn(1, category="转账", type="expense")
    bal_g2 = balance(A)
    record("★ 原流水 settle_status='returned'（退票）",
           g_tx2.get("settle_status") == "returned", f"settle={g_tx2.get('settle_status')}")
    record("★ 全额退回（X-500 → X，分毫恢复）", abs(bal_g2 - bal_g0) < 0.01,
           f"{bal_g1}→{bal_g2} (原 {bal_g0})")
    record("退款流水 +1", txn_count(A, category="转账退回") == refund0 + 1)
    refund = txn(1, category="转账退回", type="income")
    record("退款流水：收入方向 + 全额",
           refund.get("type") == "income" and abs(float(refund.get("amount", 0)) - 500) < 0.01,
           f"amount={refund.get('amount')} type={refund.get('type')}")
    record("★ 退款流水关联原流水号（因果链）",
           f"#{g_tx['id']}" in (refund.get("remark") or ""), f"remark={refund.get('remark')}")
    ret_nt = latest_notification(A, "transfer_returned")
    record("★ 退票通知（账户不存在已全额退回）",
           ret_nt.get("type") == "transfer_returned"
           and "退票" in (ret_nt.get("title") or "")
           and "全额退回" in (ret_nt.get("content") or ""), f"title={ret_nt.get('title')}")
    log_n = int(q1("SELECT COUNT(*) AS n FROM operation_logs WHERE user_id=%s "
                   "AND action='transfer_returned' AND result='success'", (A,))["n"])
    record("退票留痕 operation_logs.transfer_returned ≥1", log_n >= 1, f"count={log_n}")

    # 账单口径自洽：支出先 +500，退票后收入 +500
    exp_n = txn_count(A, category="转账", type="expense")
    inc_ret = txn_count(A, category="转账退回", type="income")
    record("账单自洽：支出笔数含在途/退票原笔", exp_n >= 1)
    record("账单自洽：退票以收入体现（本月支出自动修正）", inc_ret >= 1)

    # ===================================================================== #
    #  验收 4：收款人簿内跨行账户（新增流程创建）→ 正常到账
    # ===================================================================== #
    print("\n[验收4] 新增流程创建的跨行账户 → 登记可清算 → 到账")
    fresh_card = card_validator.generate_card_no("6222", seq=5550001)  # 工行 BIN
    # 确保不在库里（幂等：先清理上次运行残留）
    exec_sql("DELETE FROM external_accounts WHERE card_no=%s", (fresh_card,))
    exec_sql("DELETE FROM payees WHERE user_id=%s AND card_no=%s", (A, fresh_card))
    scp, sjp = create_payee(A, "验收新收款人", 1, fresh_card, "李四")
    record("新增跨行收款人成功", scp == 200 and sjp.get("ok") is True
           and sjp.get("track") == "cross_bank", f"track={sjp.get('track')}")
    record("★ 新增即登记 external_accounts（可清算）", ext_has(fresh_card))
    new_pid = int((sjp.get("payee") or {}).get("id") or 0)
    bal_n0 = balance(A)
    scn, sjn = transfer(A, new_pid, 300.0)
    record("向新登记账户转账受理", scn == 200 and sjn.get("ok") is True)
    n_tx = txn(1, category="转账", type="expense")
    force_due(int(n_tx["id"]))
    trigger_lazy_settlement(A)
    record("★ 收款人簿内跨行账户 → 清算到账",
           txn(1, category="转账", type="expense").get("settle_status") == "settled")
    record("新登记账户到账无退款（余额净 -300）",
           abs(balance(A) - (bal_n0 - 300)) < 0.01, f"{bal_n0}→{balance(A)}")

    # ===================================================================== #
    #  验收 5：同一幽灵卡号连续退票 ≥2 次 → 再转时收到提醒
    # ===================================================================== #
    print("\n[验收5] 退票记录预警：同卡号当日退票 ≥2 次 → 转账前提醒")
    # 第 1 次已在上文完成；再补 1 次 → 计数 ≥2
    scg2, sjg2 = transfer(A, ghost_id, 200.0)
    g2_tx = txn(1, category="转账", type="expense")
    force_due(int(g2_tx["id"]))
    trigger_lazy_settlement(A)
    rh = return_history(A, ghost_id)
    record("★ 退票记录查询 warn=true（当日 ≥2 次）",
           rh.get("warn") is True and int(rh.get("return_count", 0)) >= 2,
           json.dumps(rh, ensure_ascii=False))
    record("退票记录文案含「核对」", "核对" in (rh.get("message") or ""))
    rh_ok = return_history(A, PAYEE_MOM)
    record("已到账账户无退票记录（warn=false）", rh_ok.get("warn") is False,
           f"count={rh_ok.get('return_count')}")

    # ===================================================================== #
    #  验收 6：用户问"我的转账到账了吗" → AI 可查 settle_status
    # ===================================================================== #
    print("\n[验收6] 对话可查清算状态")
    from agent.tools import execute_tool
    from utils.security import set_current_user_id, set_current_auth_token
    # ★ 工具经 auth_headers() 读"当前请求上下文"里的登录态转发给 8003。
    #   独立脚本没有请求中间件，须显式注入；否则 BANK_ALLOW_ANON=0 下 8003 以 401 拒绝。
    set_current_user_id(A)
    set_current_auth_token(token(A))
    tool_payload = execute_tool("get_transactions", {"start": "2026-01-01", "end": "2026-12-31"})
    txs = tool_payload.get("transactions") or []
    has_field = any("settle_status" in t for t in txs)
    record("★ 流水工具透出 settle_status（AI 据此如实回答）", has_field,
           f"样例行数={len(txs)}")
    prompt_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "agent", "prompts.py"), encoding="utf-8").read()
    record("read_agent 提示词含跨行清算口径",
           "settle_status" in prompt_src and "退票" in prompt_src)

    # ===================================================================== #
    #  验收 7：回归 —— 站内转账即时到账不受影响
    # ===================================================================== #
    print("\n[验收7] 回归：站内转账仍即时到账")
    sc_i, sj_i = transfer(A, PAYEE_INSITE, 100.0)
    record("站内转账 ok + track insite + settle_status 为空",
           sc_i == 200 and sj_i.get("ok") is True and sj_i.get("track") == "insite"
           and sj_i.get("settle_status") is None,
           f"track={sj_i.get('track')} settle={sj_i.get('settle_status')}")
    insite_tx = txn(1, category="转账", type="expense")
    record("站内流水 settle_status 为 NULL（无需清算）", insite_tx.get("settle_status") is None)

    # ===================================================================== #
    #  验收 8：两引擎合并触发互不干扰 + 幂等（不重复退票）
    # ===================================================================== #
    print("\n[验收8] housekeeping 合并触发 + 幂等")
    hk = settlement_engine.process_housekeeping(A)
    record("housekeeping 同时返回订阅与清算两路结果",
           "deductions" in hk and "settlements" in hk,
           f"keys={list(hk.keys())}")
    record("两引擎无互相污染报错",
           "deductions_error" not in hk and "settlements_error" not in hk,
           json.dumps({k: v for k, v in hk.items() if k.endswith('_error')}, ensure_ascii=False))
    refund_before = txn_count(A, category="转账退回")
    trigger_lazy_settlement(A)   # 再触发一次（此时无到期待处理）
    trigger_lazy_settlement(A)
    record("★ 重复触发不重复退票（幂等）", txn_count(A, category="转账退回") == refund_before,
           f"{refund_before}→{txn_count(A, category='转账退回')}")
    record("无到期待处理时 due_count=0",
           settlement_engine.process_due_settlements(A).get("due_count") == 0)

    # ===================================================================== #
    print("\n" + "=" * 68)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f" 结果：{passed}/{total} 通过")
    fails = [n for n, ok, _ in RESULTS if not ok]
    if fails:
        print(" 未通过：")
        for f in fails:
            print("   -", f)
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
