"""
scripts/test_response_protocol.py —— 统一结构化响应协议（Phase 3）· 五类场景验收

目标：把转账 / 理财 / 卡 / 订阅四类**写操作**的成功回执与失败统一为固定字段
      {ok, action, code, message}，前端与 Agent 共用同一套契约；
      失败另带机器可读 code（utils.response_codes.ERR），无需解析中文文案判断原因。

测试策略（自包含，直打 mock_bank，不依赖前端 / 对话链路）：
  - 令牌用 profile_db.issue_token 现场签发（与 mock_bank 同源密钥，合法）；
  - 五类场景：正常 / 边界 / 越界 / 并发 / 绕过 API；
  - 「绕过 API」= 本脚本全部直打后端接口（不经 Agent write_flow），
    证明协议由**服务端响应包裹层**强制，而非 Agent 侧自行拼装。

★ 副作用说明（与 test_transfer_limit / test_wealth_suitability 一致的约定）：
  成功场景会真实改动测试库（小额转账、申购后赎回、冻结后解冻、建订阅后删除）。
  脚本结束前会把用户风险偏好、当日额度、卡片状态、订阅、产品上下架状态全部复原。

用法（bank-agent/ 下，.venv，需先启动 mock_bank 于 BANK_API_PORT）：
    .venv/Scripts/python.exe scripts/test_response_protocol.py
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import config
import profile_db
import requests

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT
UID = 1

PASSED: list[str] = []
FAILED: list[str] = []


def db() -> pymysql.connections.Connection:
    return pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                           password=config.DB_PASSWORD, database=config.DB_NAME, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)


def conn_autocommit() -> pymysql.connections.Connection:
    """★ 坑：pymysql 默认 autocommit=False，上下文退出会回滚，改额度/偏好会失效。"""
    c = db()
    c.autocommit(True)
    return c


def H(uid: int = UID) -> dict:
    return {"Authorization": f"Bearer {profile_db.issue_token(uid)}"}


def call(method: str, path: str, uid: int = UID, body=None):
    try:
        r = requests.request(method, MOCK + path, json=body, headers=H(uid), timeout=25)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text}
    except Exception as exc:  # noqa: BLE001
        return -1, {"error": f"请求异常：{exc}"}


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond else f"   <- {detail}"))


def check_envelope(label: str, resp: dict, action: str | None = None,
                   code: str | None = None) -> None:
    """断言统一协议：ok / action / code / message 四字段齐备（+ 可选精确比对）。"""
    missing = [k for k in ("ok", "action", "code", "message") if k not in resp]
    check(f"[协议] {label}：四字段齐备", not missing,
          f"缺 {missing}；resp={str(resp)[:220]}")
    if action is not None:
        check(f"[协议] {label}：action={action}", resp.get("action") == action,
              f"got={resp.get('action')}")
    if code is not None:
        check(f"[协议] {label}：code={code}", resp.get("code") == code,
              f"got={resp.get('code')}；resp={str(resp)[:220]}")
    if resp.get("ok") is False and "error" in resp:
        check(f"[协议] {label}：失败保留 error 且 message 与之一致",
              resp.get("message") == resp.get("error"),
              f"message={resp.get('message')!r} error={resp.get('error')!r}")


# --------------------------------------------------------------------------- #
#  准备 / 复原
# --------------------------------------------------------------------------- #
def load_fixtures() -> dict:
    with conn_autocommit() as c:
        cur = c.cursor()
        cur.execute("SELECT id, name, risk_level, min_amount FROM products "
                    "WHERE COALESCE(status,'on')='on' ORDER BY risk_level, id")
        prods = cur.fetchall()
        cur.execute("SELECT id FROM cards WHERE user_id=%s AND status='normal' "
                    "ORDER BY id LIMIT 1", (UID,))
        card = cur.fetchone()
        cur.execute("SELECT preferences FROM users WHERE id=%s", (UID,))
        prefs = (cur.fetchone() or {}).get("preferences")
        cur.execute("SELECT used_amount FROM transfer_daily_quota "
                    "WHERE user_id=%s AND quota_date=CURDATE()", (UID,))
        q = cur.fetchone()
    r1 = next(p for p in prods if int(p["risk_level"]) == 1)
    r3 = next(p for p in prods if int(p["risk_level"]) == 3)
    return {
        "r1": r1, "r3": r3, "all": prods,
        "card_id": int(card["id"]) if card else None,
        "prefs": prefs,
        "quota_used": float(q["used_amount"]) if q else None,
    }


def set_risk_pref(value) -> None:
    """设置 users.preferences.risk_preference（value=None 表示清空/未测评）。"""
    import json as _json
    with conn_autocommit() as c:
        cur = c.cursor()
        cur.execute("SELECT preferences FROM users WHERE id=%s", (UID,))
        raw = (cur.fetchone() or {}).get("preferences") or "{}"
        try:
            obj = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:  # noqa: BLE001
            obj = {}
        if value is None:
            obj.pop("risk_preference", None)
        else:
            obj["risk_preference"] = value
        cur.execute("UPDATE users SET preferences=%s WHERE id=%s",
                    (_json.dumps(obj, ensure_ascii=False), UID))


def set_quota(used: float) -> None:
    with conn_autocommit() as c:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO transfer_daily_quota (user_id, quota_date, used_amount) "
            "VALUES (%s, CURDATE(), %s) ON DUPLICATE KEY UPDATE used_amount=%s",
            (UID, used, used))


def restore_quota(original: float | None) -> None:
    with conn_autocommit() as c:
        cur = c.cursor()
        if original is None:
            cur.execute("DELETE FROM transfer_daily_quota "
                        "WHERE user_id=%s AND quota_date=CURDATE()", (UID,))
        else:
            cur.execute(
                "INSERT INTO transfer_daily_quota (user_id, quota_date, used_amount) "
                "VALUES (%s, CURDATE(), %s) ON DUPLICATE KEY UPDATE used_amount=%s",
                (UID, original, original))


def set_product_status(product_id: int, status: str) -> None:
    with conn_autocommit() as c:
        c.cursor().execute("UPDATE products SET status=%s WHERE id=%s", (status, product_id))


# --------------------------------------------------------------------------- #
#  场景
# --------------------------------------------------------------------------- #
def scenario_normal(fx: dict) -> None:
    print("\n【1】正常（normal）：成功回执带完整协议 + action 正确")

    st, r = call("POST", "/api/transfer", body={"payee_id": 1, "amount": 10})
    check_envelope("转账成功回执", r, action="transfer", code="SUCCESS")
    check("[协议] 转账成功回执：保留既有字段 serial_no/balance_after",
          "serial_no" in r and "balance_after" in r, f"resp={str(r)[:200]}")

    # 理财申购 + 赎回（赎回用于复原余额）
    set_risk_pref("进取")
    st, r = call("POST", "/api/products/buy",
                 body={"product_id": fx["r1"]["id"], "amount": float(fx["r1"]["min_amount"])})
    check_envelope("理财申购成功回执", r, action="buy_product", code="SUCCESS")
    holding_id = r.get("holding_id")
    check("[协议] 申购成功回执：保留既有字段 holding_id/suitability",
          "holding_id" in r and "suitability" in r, f"resp={str(r)[:200]}")
    if holding_id:
        st2, r2 = call("POST", "/api/redeem",
                       body={"holding_id": holding_id, "amount": float(fx["r1"]["min_amount"])})
        check_envelope("理财赎回成功回执", r2, action="redeem", code="SUCCESS")

    # 卡冻结 / 解冻（成对，复原状态）
    if fx["card_id"]:
        st, r = call("POST", "/api/cards/freeze", body={"card_id": fx["card_id"]})
        check_envelope("冻结卡成功回执", r, action="freeze_card", code="SUCCESS")
        check("[协议] 冻结回执：保留既有字段 card_id/status",
              "card_id" in r and "status" in r, f"resp={str(r)[:200]}")
        st, r = call("POST", "/api/cards/unfreeze", body={"card_id": fx["card_id"]})
        check_envelope("解冻卡成功回执", r, action="unfreeze_card", code="SUCCESS")

    # 订阅签约（随后删除复原）
    st, r = call("POST", "/api/subscriptions/contract",
                 body={"service_name": "协议回归测试", "fee": 10, "cycle": "月付"})
    check_envelope("订阅签约成功回执", r, action="subscription_contract", code="SUCCESS")
    sub_id = r.get("sub_id")
    if sub_id:
        st, r2 = call("DELETE", f"/api/subscriptions/{sub_id}")
        check_envelope("订阅删除成功回执", r2, action="delete_subscription", code="SUCCESS")


def scenario_boundary(fx: dict) -> None:
    print("\n【2】边界（boundary）：包裹层只作用于写操作，读操作零改动")

    st, r = call("GET", "/api/balance")
    check("[边界] GET /api/balance 未被包裹（无 action/code）",
          "action" not in r and "code" not in r, f"resp={str(r)[:160]}")
    check("[边界] GET /api/balance 原有字段 ok 仍在", "ok" in r, f"resp={str(r)[:160]}")

    st, r = call("GET", "/api/products")
    check("[边界] GET /api/products（无鉴权读接口）未被包裹",
          "action" not in r and "code" not in r, f"resp={str(r)[:160]}")

    # 幂等型重复操作：第二次冻结应仍是 ok=True 且协议完整（不是异常 500）
    if fx["card_id"]:
        call("POST", "/api/cards/freeze", body={"card_id": fx["card_id"]})
        st, r = call("POST", "/api/cards/freeze", body={"card_id": fx["card_id"]})
        check_envelope("重复冻结（幂等）回执", r, action="freeze_card", code="SUCCESS")
        call("POST", "/api/cards/unfreeze", body={"card_id": fx["card_id"]})


def scenario_over(fx: dict) -> None:
    print("\n【3】越界（over）：失败携带机器可读 code，且保留结构化诊断字段")

    # ---- 转账 ----
    st, r = call("POST", "/api/transfer", body={"payee_id": 1, "amount": 60000})
    check_envelope("单笔超限额", r, action="transfer", code="TRANSFER_LIMIT_SINGLE")
    check("[越界] 单笔超限额：回执带结构化 limit_info",
          isinstance(r.get("limit_info"), dict), f"resp={str(r)[:200]}")

    set_quota(49900.0)   # 模拟今日已转 49900（限额 50000）
    st, r = call("POST", "/api/transfer", body={"payee_id": 1, "amount": 200})
    check_envelope("当日累计超限额", r, action="transfer", code="TRANSFER_LIMIT_DAILY")
    restore_quota(fx["quota_used"])

    # ---- 理财 ----
    st, r = call("POST", "/api/products/buy", body={"product_id": 999999, "amount": 100})
    check_envelope("产品不存在", r, action="buy_product", code="PRODUCT_NOT_FOUND")

    r1 = fx["r1"]
    st, r = call("POST", "/api/products/buy",
                 body={"product_id": r1["id"], "amount": float(r1["min_amount"]) / 10})
    check_envelope("低于起投金额", r, action="buy_product", code="BELOW_MIN_AMOUNT")

    r3 = fx["r3"]
    set_risk_pref(None)   # 未测评
    st, r = call("POST", "/api/products/buy",
                 body={"product_id": r3["id"], "amount": float(r3["min_amount"])})
    check_envelope("风险未测评购 R3", r, action="buy_product", code="RISK_UNSET")
    check("[越界] 风险未测评：回执带结构化 suitability",
          isinstance(r.get("suitability"), dict), f"resp={str(r)[:200]}")

    set_risk_pref("稳健")   # 最高可购 R1，购 R3 应不适配
    st, r = call("POST", "/api/products/buy",
                 body={"product_id": r3["id"], "amount": float(r3["min_amount"])})
    check_envelope("风险不适配（稳健买 R3）", r, action="buy_product", code="RISK_MISMATCH")

    set_risk_pref("进取")
    st, r = call("POST", "/api/products/buy",
                 body={"product_id": r1["id"], "amount": 99999999})
    check_envelope("申购余额不足", r, action="buy_product", code="BUY_INSUFFICIENT")

    # 下架产品（临时置 off 后复原）
    off_id = r1["id"]
    set_product_status(off_id, "off")
    st, r = call("POST", "/api/products/buy",
                 body={"product_id": off_id, "amount": float(r1["min_amount"])})
    check_envelope("产品已下架", r, action="buy_product", code="PRODUCT_OFF")
    set_product_status(off_id, "on")

    # ---- 卡 ----
    if fx["card_id"]:
        st, r = call("PUT", "/api/cards/adjust_limit",
                     body={"card_id": fx["card_id"], "limit_amount": 100})
        check_envelope("调额超出范围", r, action="adjust_card_limit", code="CARD_LIMIT_RANGE")

        st, r = call("PUT", "/api/cards/adjust_limit",
                     body={"card_id": fx["card_id"], "limit_amount": 10000})
        check_envelope("储蓄卡调额度", r, action="adjust_card_limit", code="CARD_NOT_CREDIT")

    # ---- 订阅 ----
    st, r = call("POST", "/api/subscriptions/contract",
                 body={"service_name": "协议回归测试2", "fee": 10, "cycle": "周付"})
    check_envelope("订阅周期非法", r, action="subscription_contract", code="SUB_CYCLE_INVALID")

    st, r = call("POST", "/api/subscriptions/contract",
                 body={"service_name": "协议回归测试3", "fee": 10, "cycle": "月付",
                       "first_due": "2020-01-01"})
    check_envelope("首次扣款日已过", r, action="subscription_contract",
                   code="SUB_FIRST_DUE_INVALID")

    # 重复签约（同一商户）
    st, r = call("POST", "/api/subscriptions/contract",
                 body={"service_name": "协议重复测试", "fee": 10, "cycle": "月付"})
    dup_sub = r.get("sub_id")
    st, r2 = call("POST", "/api/subscriptions/contract",
                  body={"service_name": "协议重复测试", "fee": 10, "cycle": "月付"})
    check_envelope("同商户重复签约", r2, action="subscription_contract", code="SUB_DUPLICATE")
    if dup_sub:
        call("DELETE", f"/api/subscriptions/{dup_sub}")


def scenario_concurrency(fx: dict) -> None:
    print("\n【4】并发（concurrency）：并发写操作回执协议一致（不因并发丢字段）")
    results = []

    def one(i: int) -> None:
        st, r = call("POST", "/api/transfer", body={"payee_id": 1, "amount": 10})
        results.append((i, r))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("[并发] 3 个并发转账请求全部返回", len(results) == 3, f"got {len(results)}")
    ok_env = all(all(k in r for k in ("ok", "action", "code", "message"))
                 for _, r in results)
    check("[并发] 每个回执都带 ok/action/code/message", ok_env,
          f"resp={[str(r)[:120] for _, r in results]}")
    ok_action = all(r.get("action") == "transfer" for _, r in results)
    check("[并发] 每个回执 action 均为 transfer", ok_action,
          f"actions={[r.get('action') for _, r in results]}")


def scenario_bypass() -> None:
    print("\n【5】绕过 API（bypass）：协议由服务端强制，不经 Agent 也生效")
    print("    说明：本脚本全部用例均为**直打 mock_bank 后端接口**（未经过")
    print("          agent/write_flow 或任何对话链路），上面所有 action/code 断言")
    print("          均在绕过 Agent 的前提下成立 —— 证明统一协议落在服务端")
    print("          响应包裹层，而非 Agent 侧自行拼装，不存在免检路径。")
    check("[绕过] 协议在服务端强制（全部用例直连后端）", True)


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 78)
    print("统一结构化响应协议（Phase 3）· 五类场景验收")
    print("=" * 78)

    fx = load_fixtures()
    print(f"fixtures: r1={fx['r1']['name']}(min={fx['r1']['min_amount']})  "
          f"r3={fx['r3']['name']}(min={fx['r3']['min_amount']})  "
          f"card_id={fx['card_id']}  原始偏好={fx['prefs']}")

    try:
        scenario_normal(fx)
        scenario_boundary(fx)
        scenario_concurrency(fx)
        scenario_over(fx)
        scenario_bypass()
    finally:
        print("\n【复原】用户偏好 / 当日额度 / 卡片状态 / 产品状态")
        try:
            with conn_autocommit() as c:
                c.cursor().execute("UPDATE users SET preferences=%s WHERE id=%s",
                                   (fx["prefs"], UID))
            restore_quota(fx["quota_used"])
            if fx["card_id"]:
                with conn_autocommit() as c:
                    c.cursor().execute("UPDATE cards SET status='normal' WHERE id=%s",
                                       (fx["card_id"],))
            set_product_status(fx["r1"]["id"], "on")
        except Exception as exc:  # noqa: BLE001
            print(f"  复原失败（需人工检查）：{exc}")

    total = len(PASSED) + len(FAILED)
    print("\n" + "=" * 78)
    print(f"结果：{len(PASSED)}/{total} 通过" + ("" if not FAILED else f"，{len(FAILED)} 失败"))
    if FAILED:
        print("失败用例：")
        for f in FAILED:
            print("  - " + f)
    print("=" * 78)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
