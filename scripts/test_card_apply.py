"""
scripts/test_card_apply.py —— 卡片-账户一体化 · 验收自测（直连 mock_bank:8003）

覆盖需求里的 6 项脚本验收：
  #1 申请储蓄卡（新账户）→ cards+1、accounts+1、卡号 Luhn 合法且全局唯一、
     新账户 balance=0，且「卡号 = 账户号」（转账反查靠它）
  #2 B 用户向新卡卡号转账 500 → 新卡账户余额 500、B 扣款、双方通知 + 双流水
  #3 新卡归集 500 到主账户 → 主账户 +500、新卡归零、双流水 remark=内部划转、
     operation_logs 留痕 + 双方通知；归集后主账户可正常对外转账
  #4 申请第 4 张储蓄卡 → 拒绝并提示现有卡数
  #5 挂靠已有账户 → 校归属（别人的账户必须拒绝）+ 同一账户最多 2 张卡
  #6 信用卡申请 → 信用账户复用（每人一个）、额度 ¥5,000 展示、信用卡不参与收款

测试策略（自包含，不依赖前端 / 对话链路）：
  - 直接打 mock_bank（绕过 run_server 的 phone_guard 写闸门），
    用 profile_db.issue_token 现场签发令牌（与 mock_bank 同源密钥，合法）；
  - 断言直接读 bank_agent 库，更稳更快；
  - 使用**专用测试用户**（cardtest_a / cardtest_e），每次运行前清理其业务数据，
    不污染演示账号（DEFAULT_USER_ID=1 / 李哥 uid=58）。

注意：本脚本会改动测试库数据（与其他测试脚本一致，属预期副作用），
      但只作用于 cardtest_* 这两个专用用户。
用法（bank-agent/ 下，.venv）：
    .venv/Scripts/python.exe scripts/test_card_apply.py
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
from utils import card_validator
from scripts._op_test_auth import op_header  # P0.2：写端点需一次性操作票据（真实 ticket 路径）

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT

USER_A = "cardtest_a"     # 办卡主体（已初始化演示数据）
USER_E = "cardtest_e"     # 挂靠 / 归属 / 单账户上限用例主体（不初始化，无账户）
PWD = "cardtest123"

LOCAL_BANK_ID = 19        # 微众银行（本行）

results: list[tuple[str, bool]] = []


# --------------------------------------------------------------------------- #
#  基础设施
# --------------------------------------------------------------------------- #
def db():
    return pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                           password=config.DB_PASSWORD, database=config.DB_NAME,
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


def q1(sql: str, args: tuple = ()) -> dict:
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone() or {}


def qall(sql: str, args: tuple = ()) -> list[dict]:
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall() or [])


def token(uid: int) -> str:
    return profile_db.issue_token(uid)


def http(method: str, path: str, uid: int | None = None, body: dict | None = None,
         headers: dict | None = None):
    h = dict(headers or {})
    if uid is not None:
        h["Authorization"] = f"Bearer {token(uid)}"
    h.update(op_header(uid, path))   # P0.2：写端点携带一次性操作票据
    r = requests.request(method, MOCK + path, json=body, headers=h, timeout=15)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text}


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not ok else ""))
    return ok


def banner(n, name: str) -> None:
    print(f"\n=== #{n} {name} ===")


# --------------------------------------------------------------------------- #
#  测试用户准备（每次运行重置其业务数据，保证可重复执行）
# --------------------------------------------------------------------------- #
def ensure_user(username: str) -> int:
    """取（或创建）测试用户 id。"""
    row = q1("SELECT id FROM users WHERE username=%s", (username,))
    if row:
        return int(row["id"])
    created = profile_db.create_user(username, PWD)
    if not created:
        return int(q1("SELECT id FROM users WHERE username=%s", (username,))["id"])
    return int(created["user_id"])


def reset_user(uid: int) -> None:
    """清空该测试用户的业务数据（账户 / 卡 / 收款人 / 流水 / 通知 / 留痕），保证幂等可重跑。

    ★ 只作用于 cardtest_* 专用测试用户，绝不碰演示账号。
    """
    with db() as c:
        with c.cursor() as cur:
            cur.execute("SELECT id FROM accounts WHERE user_id=%s", (uid,))
            acc_ids = [int(r["id"]) for r in (cur.fetchall() or [])]
            if acc_ids:
                fmt = ",".join(["%s"] * len(acc_ids))
                cur.execute(f"DELETE FROM transactions WHERE account_id IN ({fmt})", tuple(acc_ids))
            cur.execute("DELETE FROM cards WHERE user_id=%s", (uid,))
            cur.execute("DELETE FROM payees WHERE user_id=%s", (uid,))
            cur.execute("DELETE FROM notifications WHERE user_id=%s", (uid,))
            cur.execute("DELETE FROM operation_logs WHERE user_id=%s", (uid,))
            cur.execute("DELETE FROM accounts WHERE user_id=%s", (uid,))
        c.commit()


def setup_users() -> tuple[int, int]:
    from utils import user_provision
    uid_a = ensure_user(USER_A)
    uid_e = ensure_user(USER_E)
    reset_user(uid_a)
    reset_user(uid_e)
    # A：标准演示沙箱（1 张微众储蓄卡 + 微众卡/零钱罐两个账户）
    res = user_provision.provision_new_user(uid_a, "办卡测试A")
    assert res.get("ok"), res
    print(f"  测试用户 A uid={uid_a}（已初始化演示数据）；E uid={uid_e}（无账户，用于挂靠用例）")
    return uid_a, uid_e


# --------------------------------------------------------------------------- #
def main() -> int:
    print("[test_card_apply] 卡片-账户一体化 · 验收自测")
    uid_a, uid_e = setup_users()

    # ======================================================================= #
    banner(1, "申请储蓄卡（默认开新账户）：cards+1 / accounts+1 / 卡号 Luhn 合法且唯一")
    cards0 = qall("SELECT * FROM cards WHERE user_id=%s AND status<>'deleted'", (uid_a,))
    accs0 = qall("SELECT * FROM accounts WHERE user_id=%s", (uid_a,))
    check("初始：1 张储蓄卡 / 2 个账户", len(cards0) == 1 and len(accs0) == 2,
          f"cards={len(cards0)} accounts={len(accs0)}")

    st, d1 = http("POST", "/api/cards/apply", uid_a, {"name": "出差专用卡", "type": "debit"})
    check("申请成功", st == 200 and d1.get("ok") is True, json.dumps(d1, ensure_ascii=False)[:200])
    if not d1.get("ok"):
        return finish()
    new_card_no = d1.get("card_no") or ""
    new_acc_id = int(d1.get("account_id"))
    cards1 = qall("SELECT * FROM cards WHERE user_id=%s AND status<>'deleted'", (uid_a,))
    accs1 = qall("SELECT * FROM accounts WHERE user_id=%s", (uid_a,))
    check("cards +1", len(cards1) == len(cards0) + 1, f"{len(cards0)} -> {len(cards1)}")
    check("accounts +1（开新账户）", len(accs1) == len(accs0) + 1, f"{len(accs0)} -> {len(accs1)}")
    check("返回完整卡号 16 位", len(new_card_no) == 16, new_card_no)
    check("卡号 Luhn 合法", card_validator.luhn_check(new_card_no), new_card_no)
    check("卡号 4-4-4-4 一次性展示", d1.get("card_no_grouped", "").replace(" ", "") == new_card_no,
          str(d1.get("card_no_grouped")))
    new_acc = q1("SELECT * FROM accounts WHERE id=%s", (new_acc_id,))
    check("新账户 balance=0.00", float(new_acc.get("balance") or 0) == 0.0, str(new_acc.get("balance")))
    check("新账户 card_no = 新卡卡号（转账反查靠它）", new_acc.get("card_no") == new_card_no,
          f"{new_acc.get('card_no')} vs {new_card_no}")
    check("新账户非主账户", int(new_acc.get("is_primary") or 0) == 0)
    check("卡与账户关联", int(q1("SELECT account_id FROM cards WHERE id=%s",
                                (d1["card_id"],)).get("account_id")) == new_acc_id)
    check("状态为激活态 normal", q1("SELECT status FROM cards WHERE id=%s",
                                   (d1["card_id"],)).get("status") == "normal")
    # 唯一性：accounts.card_no ∪ cards.card_no_full 内不得重复
    dup = q1("SELECT (SELECT COUNT(*) FROM accounts WHERE card_no=%s) AS a, "
             "(SELECT COUNT(*) FROM cards WHERE card_no_full=%s) AS c",
             (new_card_no, new_card_no))
    check("卡号全局唯一（accounts 1 条 / cards 1 条）",
          int(dup["a"]) == 1 and int(dup["c"]) == 1, str(dup))
    st, lim = http("GET", "/api/cards", uid_a)
    check("列表返回持卡上限统计", lim.get("limits", {}).get("debit", {}).get("used") == 2
          and lim.get("limits", {}).get("debit", {}).get("max") == 3, json.dumps(lim.get("limits")))
    listed = next((c for c in lim.get("cards", []) if c.get("id") == d1["card_id"]), None)
    check("列表带关联账户名 + 余额", listed and listed.get("account_name")
          and float(listed.get("account_balance") or 0) == 0.0, json.dumps(listed, ensure_ascii=False)[:160])
    check("列表不泄露完整卡号", listed is not None and not listed.get("card_no_full")
          and listed.get("card_no_masked", "").count("*") >= 4, str(listed and listed.get("card_no_masked")))
    st, logrow = http("GET", "/api/operation_logs?limit=50", uid_a)
    applied = [l for l in logrow.get("logs", []) if l.get("action") == "card_apply"]
    check("card_apply 留痕（含卡面名+类型+尾号）",
          applied and d1["tail"] in str(applied[0].get("detail")), str(applied[:1]))

    # ======================================================================= #
    banner(2, "B 用户向新卡卡号转账 500 → 新卡账户余额 500 + B 扣款 + 双方通知")
    # 付款方另起一个独立测试用户（不跟 A 自己的钱混在一起）
    from utils import user_provision
    uid_b = ensure_user("cardtest_b")
    reset_user(uid_b)
    user_provision.provision_new_user(uid_b, "办卡测试B")
    b_accs = qall("SELECT id, name, balance, is_primary FROM accounts WHERE user_id=%s ORDER BY id", (uid_b,))
    b_main = b_accs[0]
    b_before = float(b_main["balance"])

    a_inbox_before = int(q1("SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s",
                            (uid_a,))["n"])
    # B 把 A 的新卡加进收款人簿（★ 卡号反查必须命中 cards.card_no_full → 新账户）
    st, pc = http("POST", "/api/payees/create", uid_b,
                  {"payee_name": "A的新卡", "bank_id": LOCAL_BANK_ID, "card_no": new_card_no})
    check("B 新增收款人成功（站内）", pc.get("ok") is True and pc.get("track") == "insite",
          json.dumps(pc, ensure_ascii=False)[:200])
    check("★ 卡号反查命中新卡挂靠的账户（不是对方 id 最小账户）",
          int((pc.get("payee") or {}).get("account_id") or 0) == new_acc_id,
          str((pc.get("payee") or {}).get("account_id")))
    payee_id = int((pc.get("payee") or {}).get("id"))
    st, tr = http("POST", "/api/transfer", uid_b, {"payee_id": payee_id, "amount": 500})
    check("转账成功", tr.get("ok") is True and tr.get("track") == "insite",
          json.dumps(tr, ensure_ascii=False)[:200])
    check("新卡账户余额 500", float(q1("SELECT balance FROM accounts WHERE id=%s",
                                      (new_acc_id,))["balance"]) == 500.0)
    b_after = float(q1("SELECT balance FROM accounts WHERE id=%s", (b_main["id"],))["balance"])
    check("B 主账户扣款 500", abs((b_before - b_after) - 500.0) < 0.001, f"{b_before}->{b_after}")
    inc = qall("SELECT * FROM transactions WHERE account_id=%s AND type='income'", (new_acc_id,))
    check("新卡账户收入流水 1 条", len(inc) == 1 and float(inc[0]["amount"]) == 500.0, str(inc[:1]))
    a_inbox_after = int(q1("SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s",
                           (uid_a,))["n"])
    a_receipts = qall("SELECT * FROM notifications WHERE user_id=%s AND type='transfer_receipt' "
                      "ORDER BY id DESC LIMIT 1", (uid_a,))
    check("A 收到「收到转账」通知", a_inbox_after == a_inbox_before + 1 and bool(a_receipts),
          f"{a_inbox_before}->{a_inbox_after}")
    check("通知文案点明入账账户名",
          a_receipts and (new_acc["name"] in str(a_receipts[0].get("content"))),
          str(a_receipts[:1])[:200])
    check("B 侧「转账成功」通知存在",
          int(q1("SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s AND type='transfer_receipt'",
                 (uid_b,))["n"]) >= 1)
    check("新卡账户卡片页余额可见（GET /api/cards 该卡 account_balance=500）",
          float(next(c for c in http("GET", "/api/cards", uid_a)[1]["cards"]
                     if c["id"] == d1["card_id"])["account_balance"]) == 500.0)

    # ======================================================================= #
    banner(3, "新卡归集 500 到主账户（本人账户间划转，免验证码仅密码）")
    main_acc = q1("SELECT id, name, balance FROM accounts WHERE user_id=%s AND is_primary=1", (uid_a,))
    main_before = float(main_acc["balance"])
    inbox_before = int(q1("SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s", (uid_a,))["n"])
    logs_before = int(q1("SELECT COUNT(*) AS n FROM operation_logs WHERE user_id=%s "
                         "AND action='internal_transfer'", (uid_a,))["n"])
    st, it = http("POST", "/api/accounts/transfer-internal", uid_a,
                  {"from_account_id": new_acc_id, "to_account_id": int(main_acc["id"]), "amount": 500})
    check("归集成功", it.get("ok") is True, json.dumps(it, ensure_ascii=False)[:200])
    check("主账户 +500", abs(float(q1("SELECT balance FROM accounts WHERE id=%s",
                                     (main_acc["id"],))["balance"]) - (main_before + 500)) < 0.001)
    check("新卡归零", float(q1("SELECT balance FROM accounts WHERE id=%s",
                              (new_acc_id,))["balance"]) == 0.0)
    itx = qall("SELECT * FROM transactions WHERE account_id IN (%s,%s) AND remark='内部划转' "
               "ORDER BY id", (new_acc_id, main_acc["id"]))
    check("双流水 2 条（remark=内部划转）", len(itx) == 2, str(len(itx)))
    check("分支出/收入两侧", {t["type"] for t in itx} == {"expense", "income"}, str(itx))
    logs_after = int(q1("SELECT COUNT(*) AS n FROM operation_logs WHERE user_id=%s "
                        "AND action='internal_transfer'", (uid_a,))["n"])
    check("operation_logs 留痕 internal_transfer +1", logs_after == logs_before + 1)
    inbox_after = int(q1("SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s", (uid_a,))["n"])
    its = qall("SELECT * FROM notifications WHERE user_id=%s AND type='internal_transfer'", (uid_a,))
    check("双方（转出/转入）通知 2 条", inbox_after == inbox_before + 2 and len(its) == 2,
          f"{inbox_before}->{inbox_after} rows={len(its)}")
    check("通知文案含「归集」与金额", its and all("归集" in str(x.get("title")) + str(x.get("content"))
                                                for x in its))
    # 归集后可正常对外转账（钱真正能用了）
    st, pc2 = http("POST", "/api/payees/create", uid_a,
                   {"payee_name": "测试跨行", "bank_id": 6, "real_name": "测试收款人",
                    "card_no": card_validator.generate_card_no("62250000", 4242)})
    if pc2.get("ok"):
        st, tr2 = http("POST", "/api/transfer", uid_a, {"payee_id": int(pc2["payee"]["id"]), "amount": 100})
        check("归集后主账户可正常对外转账", tr2.get("ok") is True,
              json.dumps(tr2, ensure_ascii=False)[:160])

    # 归集的负向用例：余额不足 / 同账户 / 越权
    st, e1 = http("POST", "/api/accounts/transfer-internal", uid_a,
                  {"from_account_id": new_acc_id, "to_account_id": int(main_acc["id"]), "amount": 99999})
    check("余额不足被拒", e1.get("ok") is False and "余额不足" in (e1.get("error") or ""),
          str(e1.get("error")))
    st, e2 = http("POST", "/api/accounts/transfer-internal", uid_a,
                  {"from_account_id": int(main_acc["id"]), "to_account_id": int(main_acc["id"]),
                   "amount": 1})
    check("同账户转账被拒", e2.get("ok") is False, str(e2.get("error")))
    st, e3 = http("POST", "/api/accounts/transfer-internal", uid_e,
                  {"from_account_id": int(main_acc["id"]), "to_account_id": int(main_acc["id"]) + 1,
                   "amount": 1})
    check("★ 越权（用别人的 account_id）被拒", e3.get("ok") is False, str(e3.get("error")))

    # ======================================================================= #
    banner(4, "申请第 4 张储蓄卡 → 拒绝并提示现有卡数")
    st, d3 = http("POST", "/api/cards/apply", uid_a, {"name": "第三张储蓄卡", "type": "debit"})
    check("第 3 张储蓄卡申请成功", d3.get("ok") is True, json.dumps(d3, ensure_ascii=False)[:160])
    st, d4 = http("POST", "/api/cards/apply", uid_a, {"name": "第四张储蓄卡", "type": "debit"})
    check("第 4 张被拒", d4.get("ok") is False, json.dumps(d4, ensure_ascii=False)[:200])
    check("提示储蓄卡上限", "储蓄卡上限" in (d4.get("error") or ""), str(d4.get("error")))
    check("提示现有卡数（3 张储蓄卡）", "3" in (d4.get("error") or ""), str(d4.get("error")))
    check("被拒后落 failed 留痕",
          int(q1("SELECT COUNT(*) AS n FROM operation_logs WHERE user_id=%s "
                 "AND action='card_apply' AND result='failed'", (uid_a,))["n"]) >= 1)

    # ======================================================================= #
    banner(5, "挂靠已有账户：归属校验 + 单账户最多 2 张卡")
    # E 无账户 → 先开一张（默认开新账户）
    st, e_card = http("POST", "/api/cards/apply", uid_e, {"name": "E的第一张卡", "type": "debit"})
    check("E 无账户也能开卡（自动开新账户）", e_card.get("ok") is True,
          json.dumps(e_card, ensure_ascii=False)[:160])
    e_acc = int(e_card.get("account_id"))
    # 5a 挂靠自己的账户
    st, link1 = http("POST", "/api/cards/apply", uid_e,
                     {"name": "E的挂靠卡1", "type": "debit", "account_id": e_acc})
    check("挂靠成功（该账户第 2 张卡）", link1.get("ok") is True,
          json.dumps(link1, ensure_ascii=False)[:200])
    check("挂靠卡有独立卡号且 Luhn 合法",
          link1.get("card_no") and card_validator.luhn_check(link1["card_no"])
          and link1["card_no"] != e_card.get("card_no"), str(link1.get("card_no")))
    check("挂靠卡账号 ≠ 账户号（卡号独立）",
          link1.get("card_no") != q1("SELECT card_no FROM accounts WHERE id=%s",
                                     (e_acc,)).get("card_no"))
    # 同一账户余额在两卡上同步显示
    st, cl = http("GET", "/api/cards", uid_e)
    same_acc = [c for c in cl.get("cards", []) if int(c.get("account_id") or 0) == e_acc]
    check("同一账户两张卡余额同步显示", len(same_acc) == 2
          and len({float(c.get("account_balance") or 0) for c in same_acc}) == 1,
          json.dumps([{k: c.get(k) for k in ("id", "account_balance")} for c in same_acc]))
    check("账户已挂 2 张卡 → can_bind_more=False",
          next(a for a in http("GET", "/api/accounts", uid_e)[1]["accounts"]
               if int(a["id"]) == e_acc)["can_bind_more"] is False)
    # 5b 单账户超限
    st, link2 = http("POST", "/api/cards/apply", uid_e,
                     {"name": "E的挂靠卡2", "type": "debit", "account_id": e_acc})
    check("★ 同一账户第 3 张卡被拒", link2.get("ok") is False, json.dumps(link2, ensure_ascii=False)[:200])
    check("提示单账户上限 2 张", "2 张卡" in (link2.get("error") or ""), str(link2.get("error")))
    # 5c 归属校验：挂靠别人的账户
    st, link3 = http("POST", "/api/cards/apply", uid_e,
                     {"name": "挂靠别人的账户", "type": "debit", "account_id": int(main_acc["id"])})
    check("★ 挂靠他人账户被拒（归属校验）", link3.get("ok") is False
          and "不属于当前用户" in (link3.get("error") or ""), str(link3.get("error")))

    # ======================================================================= #
    banner(6, "信用卡申请：信用账户复用（每人一个）+ 额度 ¥5,000 + 不参与收款")
    st, c1 = http("POST", "/api/cards/apply", uid_a, {"name": "A的信用卡", "type": "credit"})
    check("信用卡申请成功", c1.get("ok") is True, json.dumps(c1, ensure_ascii=False)[:200])
    check("额度 ¥5,000 / 可用 ¥5,000",
          float(c1.get("credit_limit") or 0) == 5000.0 and float(c1.get("available_limit") or 0) == 5000.0,
          f"{c1.get('credit_limit')} / {c1.get('available_limit')}")
    check("挂在信用账户下（account_type=credit）",
          q1("SELECT account_type FROM accounts WHERE id=%s",
             (int(c1["account_id"]),)).get("account_type") == "credit")
    cc_acc = int(c1["account_id"])
    # 5d 储蓄卡不能挂靠信用账户（用 E 验证：E 只有 2 张储蓄卡，不会先撞上储蓄卡上限）
    st, e_credit = http("POST", "/api/cards/apply", uid_e, {"name": "E的信用卡", "type": "credit"})
    check("E 信用卡申请成功（用于账户类型校验）", e_credit.get("ok") is True,
          json.dumps(e_credit, ensure_ascii=False)[:160])
    st, link4 = http("POST", "/api/cards/apply", uid_e,
                     {"name": "挂到信用账户的储蓄卡", "type": "debit",
                      "account_id": int(e_credit["account_id"])})
    check("★ 储蓄卡不能挂靠信用账户", link4.get("ok") is False
          and "信用账户" in (link4.get("error") or ""), str(link4.get("error")))
    st, c2 = http("POST", "/api/cards/apply", uid_a, {"name": "A的第二张信用卡", "type": "credit"})
    check("第 2 张信用卡申请成功", c2.get("ok") is True, json.dumps(c2, ensure_ascii=False)[:160])
    check("★ 复用同一个信用账户（每人一个）", int(c2.get("account_id") or 0) == cc_acc,
          f"{c2.get('account_id')} vs {cc_acc}")
    check("信用账户全库仅 1 个",
          int(q1("SELECT COUNT(*) AS n FROM accounts WHERE user_id=%s AND account_type='credit'",
                 (uid_a,))["n"]) == 1)
    st, c3 = http("POST", "/api/cards/apply", uid_a, {"name": "A的第三张信用卡", "type": "credit"})
    check("第 3 张信用卡被拒（上限 2 张）", c3.get("ok") is False and "信用卡上限" in (c3.get("error") or ""),
          str(c3.get("error")))
    st, cl2 = http("GET", "/api/cards", uid_a)
    credit_cards = [c for c in cl2.get("cards", []) if c.get("card_type") == "credit"]
    check("信用卡列表带额度/已用/进度", len(credit_cards) == 2
          and all("usage_ratio" in c and c.get("is_demo_only") is True for c in credit_cards),
          json.dumps(credit_cards[:1], ensure_ascii=False)[:200])
    check("列表限额统计 credit 2/2", cl2.get("limits", {}).get("credit", {}).get("used") == 2
          and cl2.get("limits", {}).get("credit", {}).get("max") == 2)
    # 信用卡卡号不能作为收款账户
    st, cc_payee = http("POST", "/api/payees/create", uid_b,
                        {"payee_name": "信用卡收款", "bank_id": LOCAL_BANK_ID,
                         "card_no": c1.get("card_no")})
    check("★ 信用卡卡号不能作为收款账户", cc_payee.get("ok") is False
          and "信用卡" in (cc_payee.get("error") or ""), str(cc_payee.get("error")))
    # 信用账户余额不计入总资产（顶栏口径）
    st, bal = http("GET", "/api/balance", uid_a)
    debit_sum = sum(float(a["balance"]) for a in bal.get("accounts", []))
    check("信用账户不计入总资产（额度不是存款）",
          abs(float(bal.get("total_balance") or 0) - debit_sum) < 0.001
          and all(a.get("account_type") != "credit" for a in bal.get("accounts", [])),
          json.dumps(bal, ensure_ascii=False)[:200])

    return finish()


def finish() -> int:
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("\n" + "=" * 60)
    print(f"  {passed}/{total} PASSED" + ("  ALL GREEN" if passed == total else "  *** FAILED ***"))
    print("=" * 60)
    if passed != total:
        for name, ok in results:
            if not ok:
                print(f"  FAIL: {name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
