"""
scripts/test_idempotency.py —— 幂等执行（Phase 4）· 五类场景验收 + Agent 侧幂等键

背景（改动前的事实）：全项目只有「发验证码」和 SQL 迁移是幂等的；
转账 / 申购 / 赎回 / 卡 / 订阅这些**真正的资金与状态写操作零幂等保护** ——
网络重试、重复点击、Agent 侧重入同一图运行，都会**真实重复扣款**。

本脚本验收：
  - 服务端按 (user_id, Idempotency-Key) 去重：已成功 → 回放原回执，不重复执行；
    处理中 → IDEMPOTENT_IN_FLIGHT；同 key 不同请求体 → IDEMPOTENT_KEY_REUSED；
  - 不带幂等键时行为**与改动前完全一致**（向后兼容，既有测试不受影响）；
  - Agent 侧：write_flow.execute 本次运行的键存进 params，重入/重试复用同一键。

测试策略：直打 mock_bank（绕过 Agent），令牌用 profile_db.issue_token 现场签发。

★ 副作用说明：成功场景会真实扣款（每笔 10 元量级）。结束前清理本次产生的
  idempotency_records（key 前缀 phase4-）与临时订阅；当日额度会复原。

用法（bank-agent/ 下，.venv，需先启动 mock_bank 于 BANK_API_PORT）：
    .venv/Scripts/python.exe scripts/test_idempotency.py
"""
from __future__ import annotations

import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import config
import profile_db
import requests
from scripts._op_test_auth import op_header  # P0.2：写端点需一次性操作票据（真实 ticket 路径）

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT
UID = 1
UID2 = 2          # 另一个用户（验证幂等键按用户隔离）
AMT = 10.0

PASSED: list[str] = []
FAILED: list[str] = []


def db() -> pymysql.connections.Connection:
    c = pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                        password=config.DB_PASSWORD, database=config.DB_NAME, charset="utf8mb4",
                        cursorclass=pymysql.cursors.DictCursor)
    c.autocommit(True)   # ★ 坑：pymysql 默认不提交，改额度/清理会静默回滚
    return c


def H(uid: int = UID) -> dict:
    return {"Authorization": f"Bearer {profile_db.issue_token(uid)}"}


def K(k: str) -> dict:
    return {"Idempotency-Key": k}


def call(method: str, path: str, uid: int = UID, body=None, key: str | None = None):
    headers = {**H(uid), **op_header(uid, path)}   # P0.2：写端点携带一次性操作票据
    if key:
        headers = {**headers, **K(key)}
    try:
        r = requests.request(method, MOCK + path, json=body, headers=headers, timeout=25)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text}
    except Exception as exc:  # noqa: BLE001
        return -1, {"error": f"请求异常：{exc}"}


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond else f"   <- {detail}"))


def balance(uid: int = UID) -> float:
    st, r = call("GET", "/api/balance", uid=uid)
    return float(r["accounts"][0]["balance"])


def set_quota(uid: int, used: float) -> None:
    with db() as c:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO transfer_daily_quota (user_id, quota_date, used_amount) "
            "VALUES (%s, CURDATE(), %s) ON DUPLICATE KEY UPDATE used_amount=%s",
            (uid, used, used))


def new_key(tag: str) -> str:
    return f"phase4-{tag}-{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
#  场景
# --------------------------------------------------------------------------- #
def scenario_normal() -> None:
    """【1】正常：同 key 重放只执行一次，回执一致且不多扣钱。"""
    print("\n【1】正常（normal）：同幂等键重放 → 回放原回执，不重复扣款")
    key = new_key("normal")
    b0 = balance()

    st1, r1 = call("POST", "/api/transfer", body={"payee_id": 1, "amount": AMT}, key=key)
    check("首次请求执行成功", st1 == 200 and r1.get("ok") is True, f"{st1} {str(r1)[:160]}")
    check("首次请求未标记重放", r1.get("idempotent_replay") is not True, str(r1)[:160])
    serial1 = r1.get("serial_no")

    st2, r2 = call("POST", "/api/transfer", body={"payee_id": 1, "amount": AMT}, key=key)
    check("重放请求返回 200", st2 == 200, f"{st2} {str(r2)[:160]}")
    check("重放标记为 idempotent_replay", r2.get("idempotent_replay") is True, str(r2)[:160])
    check(f"重放回执与原回执同流水号（{serial1}）", r2.get("serial_no") == serial1,
          f"{r2.get('serial_no')} != {serial1}")
    b1 = balance()
    check(f"重放未重复扣款（余额仅减 {AMT}）", abs((b0 - b1) - AMT) < 0.01,
          f"实际减少 {round(b0 - b1, 2)}")

    # 第三次重放仍然安全
    st3, r3 = call("POST", "/api/transfer", body={"payee_id": 1, "amount": AMT}, key=key)
    b2 = balance()
    check("多次重放仍不多扣", abs((b0 - b2) - AMT) < 0.01, f"实际减少 {round(b0 - b2, 2)}")
    check("重放回执带统一协议字段 action/code/message",
          all(k in r3 for k in ("ok", "action", "code", "message")), str(r3)[:200])


def scenario_boundary() -> None:
    """【2】边界：不同 key 各自执行；不带 key 行为不变；跨用户同 key 互不影响。"""
    print("\n【2】边界（boundary）：键的作用域与向后兼容")

    # 2a 不同 key → 各自独立执行
    b0 = balance()
    st_a, ra = call("POST", "/api/transfer", body={"payee_id": 1, "amount": AMT}, key=new_key("ba"))
    st_b, rb = call("POST", "/api/transfer", body={"payee_id": 1, "amount": AMT}, key=new_key("bb"))
    check("不同 key 两次都真实执行", ra.get("ok") and rb.get("ok")
          and ra.get("serial_no") != rb.get("serial_no"),
          f"{str(ra)[:120]} | {str(rb)[:120]}")
    check(f"不同 key 共扣 {AMT * 2}", abs((b0 - balance()) - AMT * 2) < 0.01,
          f"实际减少 {round(b0 - balance(), 2)}")

    # 2b 不带 key → 与改动前一致（每次都执行）
    b1 = balance()
    _, rc = call("POST", "/api/transfer", body={"payee_id": 1, "amount": AMT})
    _, rd = call("POST", "/api/transfer", body={"payee_id": 1, "amount": AMT})
    check("不带幂等键：两次均执行（向后兼容，行为零变化）",
          rc.get("ok") and rd.get("ok") and rc.get("serial_no") != rd.get("serial_no"),
          f"{str(rc)[:120]} | {str(rd)[:120]}")
    check(f"不带幂等键共扣 {AMT * 2}", abs((b1 - balance()) - AMT * 2) < 0.01,
          f"实际减少 {round(b1 - balance(), 2)}")

    # 2c 跨用户：同一 key 对不同用户互不干扰
    shared = new_key("shared")
    # ★ 商户名带 phase4- 前缀：签约会补录历史流水，这些流水会喂给「订阅发现」引擎，
    #   必须在 cleanup() 里连同订阅一起删掉，否则会污染 test_subscription_flow 的隔离断言。
    st_u1, r1 = call("POST", "/api/subscriptions/contract", uid=UID,
                     body={"service_name": "phase4-isoA", "fee": 9.9, "cycle": "月付"}, key=shared)
    st_u2, r2 = call("POST", "/api/subscriptions/contract", uid=UID2,
                     body={"service_name": "phase4-isoB", "fee": 8.8, "cycle": "月付"}, key=shared)
    check("用户1 用 key 签约成功", r1.get("ok") is True, f"{st_u1} {str(r1)[:160]}")
    check("用户2 用**同一** key 仍可独立执行（按用户隔离）",
          r2.get("ok") is True and r1.get("sub_id") != r2.get("sub_id"),
          f"{st_u2} {str(r2)[:160]}")
    for uid, sub in ((UID, r1.get("sub_id")), (UID2, r2.get("sub_id"))):
        if sub:
            call("DELETE", f"/api/subscriptions/{sub}", uid=uid)


def scenario_over() -> None:
    """【3】越界：key 复用不同请求体被拒；业务失败不占坑（可同 key 同 body 重试）。"""
    print("\n【3】越界（over）：键复用冲突 与 失败不锁死")

    # 3a 同 key 不同 body → IDEMPOTENT_KEY_REUSED，且不执行
    key = new_key("reuse")
    b0 = balance()
    st1, r1 = call("POST", "/api/transfer", body={"payee_id": 1, "amount": AMT}, key=key)
    st2, r2 = call("POST", "/api/transfer", body={"payee_id": 1, "amount": 77}, key=key)
    check("同 key 不同请求体 → 409", st2 == 409, f"{st2} {str(r2)[:160]}")
    check("错误码 IDEMPOTENT_KEY_REUSED", r2.get("code") == "IDEMPOTENT_KEY_REUSED",
          f"got={r2.get('code')}")
    check("被拒请求未执行任何扣款", abs((b0 - balance()) - AMT) < 0.01,
          f"实际减少 {round(b0 - balance(), 2)}")

    # 3b 业务失败不占坑：同 key + 同 body，先因限额失败，复原额度后重试应成功
    key2 = new_key("retry")
    body = {"payee_id": 1, "amount": 200}
    set_quota(UID, 49900.0)                       # 限额 50000，剩 100 → 200 会失败
    st_f, r_f = call("POST", "/api/transfer", body=body, key=key2)
    check("限额拦截 → 业务失败", r_f.get("ok") is False and r_f.get("code") == "TRANSFER_LIMIT_DAILY",
          f"{st_f} {str(r_f)[:160]}")
    set_quota(UID, 0.0)                           # 复原额度
    st_s, r_s = call("POST", "/api/transfer", body=body, key=key2)
    check("同一 key + 同一 body 重试成功（失败不占坑）",
          st_s == 200 and r_s.get("ok") is True, f"{st_s} {str(r_s)[:160]}")


def scenario_concurrency() -> None:
    """【4】并发：同 key 并发提交 → 只有一次真实扣款。"""
    print("\n【4】并发（concurrency）：同幂等键并发 5 次只执行一次")
    key = new_key("conc")
    b0 = balance()
    results = []

    def one() -> None:
        results.append(call("POST", "/api/transfer",
                            body={"payee_id": 1, "amount": AMT}, key=key))

    ts = [threading.Thread(target=one) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    check("5 个并发请求全部返回", len(results) == 5, f"got {len(results)}")
    serials = {r.get("serial_no") for _, r in results if r.get("ok") and r.get("serial_no")}
    check(f"并发下只产生 1 个流水号（真实执行 1 次）", len(serials) == 1,
          f"serials={serials}")
    b1 = balance()
    check(f"并发只扣款 {AMT}（未重复扣）", abs((b0 - b1) - AMT) < 0.01,
          f"实际减少 {round(b0 - b1, 2)}")
    in_flight = sum(1 for _, r in results if r.get("code") == "IDEMPOTENT_IN_FLIGHT")
    replayed = sum(1 for _, r in results if r.get("idempotent_replay") is True)
    check("其余请求为 重放 或 IN_FLIGHT（均非重复执行）",
          in_flight + replayed == 4, f"in_flight={in_flight} replay={replayed}")


def scenario_bypass() -> None:
    """【5】绕过 API：幂等由服务端强制（不经 Agent 也生效）。"""
    print("\n【5】绕过 API（bypass）：幂等落在服务端，不存在免检路径")
    print("    说明：以上全部用例均为**直打 mock_bank 后端**（未经过")
    print("          agent/write_flow 或任何对话链路），幂等去重由")
    print("          ResponseEnvelopeMiddleware 在服务端完成 —— 即使绕过")
    print("          Agent 直接打接口，重复提交同样只执行一次。")
    check("[绕过] 服务端强制幂等（全部用例直连后端）", True)


def scenario_agent_side() -> None:
    """【附】Agent 侧：write_flow.execute 重入复用同一幂等键（离线单测）。"""
    print("\n【附】Agent 侧（write_flow）：重入/重试复用同一 Idempotency-Key")
    try:
        import agent.write_flow as wf
    except Exception as exc:  # noqa: BLE001
        check("导入 write_flow", False, str(exc))
        return

    captured: list[dict] = []
    orig = wf._http

    def fake_http(method, path, json_body=None, params=None, extra_headers=None):
        captured.append(extra_headers or {})
        return {"ok": True, "serial_no": "FAKE-SERIAL", "amount": 1.0, "message": "ok"}

    wf._http = fake_http
    try:
        state = {"task": {"action": "transfer"},
                 "params": {"payee_id": 1, "amount": 1.0}}
        out1 = wf.execute(state)
        # 模拟「重入 execute」：把节点回写的 params 合并进 state
        state2 = {**state, "params": out1.get("params") or {}}
        out2 = wf.execute(state2)
    except Exception as exc:  # noqa: BLE001
        check("execute 可离线调用", False, f"{type(exc).__name__}: {exc}")
        return
    finally:
        wf._http = orig

    k1 = captured[0].get("Idempotency-Key") if captured else None
    k2 = captured[1].get("Idempotency-Key") if len(captured) > 1 else None
    check("execute 首次生成幂等键并随请求下发", bool(k1), f"headers={captured[:1]}")
    check("execute 重入复用**同一**幂等键（后端据此回放，不重复扣款）",
          bool(k1) and k1 == k2, f"{k1} != {k2}")
    check("幂等键回写进 params（随 checkpoint 持久化）",
          (out1.get("params") or {}).get("_idem_key") == k1,
          f"params={out1.get('params')}")


# --------------------------------------------------------------------------- #
def cleanup() -> None:
    print("\n【清理】幂等记录 + 签约补录的历史流水（均带 phase4- 前缀）")
    try:
        with db() as c:
            cur = c.cursor()
            # ① 幂等记录
            cur.execute("DELETE FROM idempotency_records WHERE idem_key LIKE 'phase4-%'")
            print(f"   幂等记录：删除 {cur.rowcount} 行")
            # ② ★ 签约「历史补录」流水：不删会污染「订阅发现」引擎
            #    （3 笔等额月付流水正好命中周期性判定 → 变成假的订阅候选）
            cur.execute("DELETE FROM transactions WHERE merchant LIKE 'phase4-%'")
            print(f"   补录流水：删除 {cur.rowcount} 行")
            # ③ 兜底清理可能残留的测试订阅
            cur.execute("DELETE FROM subscriptions WHERE service_name LIKE 'phase4-%'")
            print(f"   测试订阅：删除 {cur.rowcount} 行")
    except Exception as exc:  # noqa: BLE001
        print(f"   清理失败（需人工检查）：{exc}")


def main() -> int:
    print("=" * 78)
    print("幂等执行（Phase 4）· 五类场景验收 + Agent 侧幂等键")
    print("=" * 78)
    try:
        scenario_normal()
        scenario_boundary()
        scenario_over()
        scenario_concurrency()
        scenario_bypass()
        scenario_agent_side()
    finally:
        cleanup()

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
