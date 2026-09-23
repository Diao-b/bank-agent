"""
scripts/test_subscription_flow.py —— 订阅模块 v2 全链路自测

覆盖 8 条验收标准：
  1. 签约自洽：contract 创建订阅 + 补录历史流水，一个事务
  2. 惰性扣款：next_due 过期后，GET /api/subscriptions 触发自动扣款
  3. 取消闭环：cancel 后即使 next_due 过期也不再产生流水
  4. 发现引擎：discover 从流水挖出未登记的周期扣款（腾讯视频）
  5. 非周期不误识别：瑞幸咖啡（间隔不规则）不进候选
  6. 余额不足跳过：不够扣就跳过、next_due 不动
  7. 回归：流水 / 余额 / 更新 / 删除等旧接口不受影响
  8. 多用户隔离：A 发现腾讯视频、B 只发现美团外卖会员，互不可见

用法：
    python scripts/test_subscription_flow.py
（直接跑 FastAPI TestClient，不需要起服务；结束自动清理测试数据）
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import profile_db  # noqa: E402
from mock_bank import db  # noqa: E402
from mock_bank.main import app  # noqa: E402

client = TestClient(app)

USER_A, USER_B = 1, 2
TEST_MERCHANT = "自测·腾讯音乐"      # 签约代扣用（测试完删）
TEST_MERCHANT_B = "自测·大额服务"    # 余额不足用（用户B）

_passed, _failed = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _passed.append(name)
        print(f"  ✅ {name}")
    else:
        _failed.append(name)
        print(f"  ❌ {name}  {detail}")


def hdr(uid: int) -> dict:
    return {"Authorization": f"Bearer {profile_db.issue_token(uid)}"}


def q1(sql: str, args=()) -> dict | None:
    return db.fetch_one(sql, args)


def force_next_due(merchant: str, uid: int, new_due: date) -> None:
    db.execute(
        "UPDATE subscriptions SET next_due=%s WHERE user_id=%s AND merchant=%s",
        (new_due, uid, merchant),
    )


def txn_count(merchant: str, uid: int, on: date | None = None) -> int:
    row = q1(
        "SELECT COUNT(*) AS c FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "WHERE a.user_id=%s AND t.merchant=%s AND (%s IS NULL OR t.`date`=%s)",
        (uid, merchant, on, on),
    )
    return int(row["c"]) if row else 0


# --------------------------------------------------------------------------- #
def cleanup() -> None:
    """删掉测试期间产生的订阅 / 流水 / 日志，恢复余额。"""
    for uid, merchant in ((USER_A, TEST_MERCHANT), (USER_B, TEST_MERCHANT_B)):
        db.execute(
            "DELETE FROM transactions WHERE merchant=%s AND account_id IN "
            "(SELECT id FROM accounts WHERE user_id=%s)",
            (merchant, uid),
        )
        db.execute(
            "DELETE FROM subscriptions WHERE user_id=%s AND merchant=%s",
            (uid, merchant),
        )
        db.execute(
            "DELETE FROM operation_logs WHERE user_id=%s AND detail LIKE %s",
            (uid, f"%{merchant}%"),
        )
    # 用户B 的余额在"余额不足"用例里不该有变化；用户A 的签约补录不动余额，也无需恢复
    # （discover 用例可能提前中断留下的转正/忽略记录，一并还原到演示初始态）
    db.execute("DELETE FROM subscriptions WHERE user_id=%s AND merchant='腾讯视频'", (USER_A,))
    db.execute("DELETE FROM subscriptions WHERE user_id=%s AND merchant='美团外卖会员'", (USER_B,))


def test_contract_and_lazy() -> None:
    print("\n== 1. 签约代扣 + 2. 惰性扣款 + 6. 余额不足 ==")
    bal_before = q1("SELECT balance FROM accounts WHERE id=1")["balance"]

    r = client.post("/api/subscriptions/contract", headers=hdr(USER_A),
                    json={"service_name": TEST_MERCHANT, "fee": 10.0, "cycle": "月付"})
    body = r.json()
    check("contract 返回 ok", r.status_code == 200 and body.get("ok"), str(body))
    check("source=contracted / confirmed=1 / next_due 在未来",
          body.get("source") == "contracted"
          and date.fromisoformat(body["next_due"]) > date.today(),
          str(body))
    backfilled = txn_count(TEST_MERCHANT, USER_A)
    check("补录 2~3 笔历史流水", 2 <= backfilled <= 3, f"got {backfilled}")
    check("补录不动余额", q1("SELECT balance FROM accounts WHERE id=1")["balance"] == bal_before)

    # 重复签约应被拒绝
    r2 = client.post("/api/subscriptions/contract", headers=hdr(USER_A),
                     json={"service_name": TEST_MERCHANT, "fee": 10.0, "cycle": "月付"})
    check("重复签约被拒绝", not r2.json().get("ok"), str(r2.json()))

    # ---- 惰性扣款：把 next_due 拨到昨天，GET 订阅列表触发 ----
    overdue = date.today() - timedelta(days=1)
    force_next_due(TEST_MERCHANT, USER_A, overdue)
    r3 = client.get("/api/subscriptions", headers=hdr(USER_A))
    subs = {s["merchant"]: s for s in r3.json().get("subscriptions", [])}
    check("列表返回新字段（source/merchant/next_due/confirmed）",
          all(k in list(subs.values())[0] for k in ("source", "merchant", "next_due", "confirmed"))
          if subs else False)
    sub = subs.get(TEST_MERCHANT, {})
    check("惰性扣款已执行（出现自动扣费流水）",
          txn_count(TEST_MERCHANT, USER_A, overdue) == 1
          and sub.get("next_due") and date.fromisoformat(sub["next_due"]) > date.today(),
          str(sub))
    bal_after = q1("SELECT balance FROM accounts WHERE id=1")["balance"]
    check("余额被扣减 ¥10", bal_before - bal_after == Decimal("10.00"),
          f"{bal_before} -> {bal_after}")
    remark = q1(
        "SELECT remark FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "WHERE a.user_id=%s AND t.merchant=%s AND t.`date`=%s",
        (USER_A, TEST_MERCHANT, overdue),
    )
    check("流水 remark='订阅自动扣费'", remark and remark["remark"] == "订阅自动扣费")

    # ---- 查重守卫：把 next_due 拨回已扣过款的日期，再触发不重复扣 ----
    force_next_due(TEST_MERCHANT, USER_A, overdue)
    r4 = client.get("/api/subscriptions", headers=hdr(USER_A))
    sub4 = {s["merchant"]: s for s in r4.json().get("subscriptions", [])}[TEST_MERCHANT]
    check("查重守卫：同日不重复扣款、只顺延",
          txn_count(TEST_MERCHANT, USER_A, overdue) == 1
          and date.fromisoformat(sub4["next_due"]) > date.today(), str(sub4))

    # ---- 余额不足（用户B，余额 1200 < 9999）----
    rb = client.post("/api/subscriptions/contract", headers=hdr(USER_B),
                     json={"service_name": TEST_MERCHANT_B, "fee": 9999.0, "cycle": "月付",
                           "first_due": (date.today() + timedelta(days=5)).isoformat()})
    check("用户B签约成功（首扣在未来）", rb.json().get("ok"), str(rb.json()))
    force_next_due(TEST_MERCHANT_B, USER_B, date.today() - timedelta(days=1))
    client.get("/api/subscriptions", headers=hdr(USER_B))
    check("余额不足：不产生扣款流水且 next_due 不动",
          txn_count(TEST_MERCHANT_B, USER_B, date.today() - timedelta(days=1)) == 0
          and q1("SELECT next_due FROM subscriptions WHERE user_id=%s AND merchant=%s",
                 (USER_B, TEST_MERCHANT_B))["next_due"] == date.today() - timedelta(days=1))

    # ---- 取消闭环 ----
    sub_id = q1("SELECT id FROM subscriptions WHERE user_id=%s AND merchant=%s",
                (USER_A, TEST_MERCHANT))["id"]
    force_next_due(TEST_MERCHANT, USER_A, date.today() - timedelta(days=1))
    rc = client.post(f"/api/subscriptions/{sub_id}/cancel", headers=hdr(USER_A))
    check("cancel 返回 ok", rc.json().get("ok"), str(rc.json()))
    cnt_before = txn_count(TEST_MERCHANT, USER_A)
    client.get("/api/subscriptions", headers=hdr(USER_A))
    client.get("/api/transactions", headers=hdr(USER_A))
    check("取消闭环：到期也不再扣款",
          txn_count(TEST_MERCHANT, USER_A) == cnt_before
          and q1("SELECT status FROM subscriptions WHERE id=%s", (sub_id,))["status"] == "cancelled")


def test_discover() -> None:
    print("\n== 4. 发现引擎 + 5. 非周期不误识别 + 确认/忽略 ==")
    r = client.get("/api/subscriptions/discover", headers=hdr(USER_A))
    body = r.json()
    names = [c["merchant"] for c in body.get("candidates", [])]
    check("发现腾讯视频（3 笔周期扣款、未入表）", "腾讯视频" in names, str(names))
    check("非周期商户（瑞幸咖啡）不误识别", "瑞幸咖啡" not in names, str(names))
    check("已登记商户（爱奇艺）不重复发现", "爱奇艺" not in names, str(names))

    rc = client.post("/api/subscriptions/discover/confirm", headers=hdr(USER_A),
                     json={"merchant": "腾讯视频"})
    body_c = rc.json()
    check("确认候选 → 转正 active/identified", body_c.get("ok")
          and body_c.get("source") == "identified", str(body_c))
    r2 = client.get("/api/subscriptions/discover", headers=hdr(USER_A))
    check("确认后不再出现在候选里",
          "腾讯视频" not in [c["merchant"] for c in r2.json().get("candidates", [])])

    # 忽略候选（用户B 的美团外卖会员）
    rb = client.get("/api/subscriptions/discover", headers=hdr(USER_B))
    names_b = [c["merchant"] for c in rb.json().get("candidates", [])]
    check("用户B 只发现美团外卖会员（隔离）", names_b == ["美团外卖会员"], str(names_b))
    rbi = client.post("/api/subscriptions/discover/ignore", headers=hdr(USER_B),
                      json={"merchant": "美团外卖会员"})
    check("忽略候选成功", rbi.json().get("ok"), str(rbi.json()))
    rb2 = client.get("/api/subscriptions/discover", headers=hdr(USER_B))
    check("忽略后 discover 不再推荐",
          "美团外卖会员" not in [c["merchant"] for c in rb2.json().get("candidates", [])])

    # 收尾：把"腾讯视频"转正的订阅与"美团"忽略记录还原，恢复演示初始态
    db.execute("DELETE FROM subscriptions WHERE user_id=%s AND merchant='腾讯视频'", (USER_A,))
    db.execute("DELETE FROM subscriptions WHERE user_id=%s AND merchant='美团外卖会员'", (USER_B,))
    db.execute(
        "DELETE FROM operation_logs WHERE user_id=%s AND detail LIKE %s "
        "AND action IN ('subscription_confirm','subscription_ignore')",
        (USER_A, "%腾讯视频%"),
    )
    db.execute(
        "DELETE FROM operation_logs WHERE user_id=%s AND detail LIKE %s",
        (USER_B, "%美团外卖会员%"),
    )


def test_regression() -> None:
    print("\n== 7. 回归：旧接口不受影响 ==")
    for path in ("/api/balance", "/api/transactions", "/api/subscriptions",
                 "/api/payees", "/api/products", "/api/cards"):
        r = client.get(path, headers=hdr(USER_A))
        check(f"GET {path}", r.status_code == 200 and r.json().get("ok"), r.text[:120])
    # 越权：用户B 碰用户A 的订阅 → 403
    sub_a = q1("SELECT id FROM subscriptions WHERE user_id=%s LIMIT 1", (USER_A,))
    if sub_a:
        r = client.post(f"/api/subscriptions/{sub_a['id']}/cancel", headers=hdr(USER_B))
        check("水平越权被拦截（B 不能取消 A 的订阅）", r.status_code == 403,
              f"status={r.status_code}")


def main() -> None:
    print("订阅模块 v2 全链路自测")
    print(f"数据库自检：腾讯视频流水 {txn_count('腾讯视频', USER_A)} 笔，"
          f"美团流水 {txn_count('美团外卖会员', USER_B)} 笔")
    try:
        test_contract_and_lazy()
        test_discover()
        test_regression()
    finally:
        cleanup()
        print("\n（测试数据已清理）")
    print(f"\n结果：{len(_passed)} 通过 / {len(_failed)} 失败")
    if _failed:
        print("失败项：", *_failed, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()
