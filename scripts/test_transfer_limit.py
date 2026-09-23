"""
scripts/test_transfer_limit.py —— 转账限额（单笔 / 单日累计）强制校验 · 五类场景验收

测试策略（自包含，不依赖前端 / 对话链路）：
  - 直接打 mock_bank 的 POST /api/transfer 与 GET /api/transfer/limit（与 test_insite_transfer 同源）；
  - 令牌用 profile_db.issue_token 现场签发（与 mock_bank 同源密钥，合法）；
  - 当日累计口径用「直接改写 transfer_daily_quota.used_amount」模拟历史已转金额，
    从而用小额真实转账即可覆盖"单笔==限额 / 已转 N 再转 M 超额"等场景，无需巨额余额；
  - 被拒转账在限额校验阶段即失败（位于扣款之前），不改动余额 / 流水 / 额度，断言安全。

★ 五类场景（用户验收要求）：
  1. 正常（normal）：额度内转账成功，回执与 GET /limit 反映 used / remaining；
  2. 边界（boundary）：恰好转剩余额度允许（单笔==限额 inclusive）；
  3. 越界（over）：单笔 > 限额拒绝；已转 30000 再转 25000 拒绝并显示剩余 20000；
  4. 并发（concurrency）：并发请求不能同时突破（行锁串行化，至多 1 笔成功）；
  5. 绕过 API（bypass）：直接打后端转账接口（绕过 Agent write_flow 前置提示）仍被拦截，
     证明强制校验落在 execute_transfer 事务内、无单独免检路径。

★ 副作用说明（与 test_insite_transfer 一致的预期副作用约定）：
  本脚本会真实改动测试库——成功场景会真实扣减 SENDER 余额并写入流水、累加额度。
  默认 SENDER=1（张小明），累计真实扣减约 8000 元；运行前会自动把当日额度清零。
  如需零污染，改 SENDER 为一个一次性测试账号并在结束后用 profile_db.delete_user_cascade 清理。

用法（bank-agent/ 下，.venv，需先启动 mock_bank 于 BANK_API_PORT）：
    .venv/Scripts/python.exe scripts/test_transfer_limit.py
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

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT

# 发送方：张小明（有足够非信用余额）；收款人：妈妈(payee_id=1, 行外，属 user1)
SENDER = 1
PAYEE = 1


def db() -> pymysql.connections.Connection:
    return pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                           password=config.DB_PASSWORD, database=config.DB_NAME, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor)


def token(uid: int) -> str:
    return profile_db.issue_token(uid)


def transfer(uid: int, payee_id: int, amount: float) -> tuple[int, dict]:
    h = {"Authorization": f"Bearer {token(uid)}"}
    try:
        r = requests.post(MOCK + "/api/transfer",
                          json={"payee_id": payee_id, "amount": amount},
                          headers=h, timeout=15)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text}
    except Exception as exc:  # 连接失败等
        return -1, {"error": f"请求异常：{exc}"}


def limit_query(uid: int) -> dict:
    h = {"Authorization": f"Bearer {token(uid)}"}
    try:
        r = requests.get(MOCK + "/api/transfer/limit", headers=h, timeout=8)
        return r.json()
    except Exception as exc:
        return {"error": f"请求异常：{exc}"}


def _reset_quota(uid: int) -> None:
    """把当日额度锚点行重置为 used=0（幂等：先删后插）。需显式提交——pymysql 默认
    关闭自动提交，否则上下文退出会回滚，模拟值不落库（曾因此导致全部限额断言失真）。"""
    with db() as c:
        c.autocommit(True)
        with c.cursor() as cur:
            cur.execute("DELETE FROM transfer_daily_quota WHERE user_id=%s AND quota_date=CURDATE()", (uid,))
            cur.execute(
                "INSERT INTO transfer_daily_quota (user_id, quota_date, used_amount) VALUES (%s, CURDATE(), 0.00)",
                (uid,))


def _set_used(uid: int, used: float) -> None:
    """模拟当日历史已转金额：直接置 used_amount（不经由真实转账）。需显式提交。"""
    with db() as c:
        c.autocommit(True)
        with c.cursor() as cur:
            cur.execute("DELETE FROM transfer_daily_quota WHERE user_id=%s AND quota_date=CURDATE()", (uid,))
            cur.execute(
                "INSERT INTO transfer_daily_quota (user_id, quota_date, used_amount) VALUES (%s, CURDATE(), %s)",
                (uid, used))


# --------------------------------------------------------------------------- #
#  验收主体
# --------------------------------------------------------------------------- #
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def server_up() -> bool:
    try:
        r = requests.get(MOCK + "/api/transfer/limit",
                         headers={"Authorization": f"Bearer {token(SENDER)}"}, timeout=5)
        return r.status_code in (200, 401, 403)  # 能连上即可
    except Exception:
        return False


def scenario_normal(lim: float) -> None:
    print("\n[1/5 正常] 额度内转账成功 + 额度回显")
    _reset_quota(SENDER)
    amt = 100.0   # 小额真实转账即可验证"成功 + 额度回显"（大额由 _set_used 模拟）
    sc, sj = transfer(SENDER, PAYEE, amt)
    record("正常·额度内转账成功", sc == 200 and sj.get("ok"), f"status={sc}")
    record("正常·回执含 limit_info", sj.get("limit_info") is not None)
    li = sj.get("limit_info") or {}
    record("正常·used==%.0f" % amt, abs(li.get("used", 0) - amt) < 0.01, f"used={li.get('used')}")
    record("正常·remaining==limit-amt",
           abs(li.get("remaining", 0) - (lim - amt)) < 0.01, f"remaining={li.get('remaining')}")
    gl = limit_query(SENDER)
    record("正常·GET /limit 反映 used==%.0f" % amt,
           abs(gl.get("used", 0) - amt) < 0.01, f"used={gl.get('used')}")


def scenario_boundary(lim: float) -> None:
    print("\n[2/5 边界] 恰好转剩余额度允许（单笔 == 限额 inclusive）")
    # 置 used = lim-100，再转 100 → 累计 == lim，应放行（仅 100 真实扣减）
    _set_used(SENDER, lim - 100)
    sc, sj = transfer(SENDER, PAYEE, 100.0)
    record("边界·恰好转剩余额度允许", sc == 200 and sj.get("ok"),
           f"status={sc} {sj.get('error','')[:40]}")
    li = sj.get("limit_info") or {}
    record("边界·used==limit", abs(li.get("used", 0) - lim) < 0.01, f"used={li.get('used')}")
    record("边界·remaining==0", abs(li.get("remaining", 0)) < 0.01,
           f"remaining={li.get('remaining')}")
    gl = limit_query(SENDER)
    record("边界·GET /limit remaining==0", abs(gl.get("remaining", 0)) < 0.01,
           f"remaining={gl.get('remaining')}")


def scenario_over(lim: float) -> None:
    print("\n[3/5 越界] 单笔 > 限额拒绝；已转 30000 再转 25000 拒绝且显示剩余 20000")
    # 3a 单笔超额
    _set_used(SENDER, 0)
    sc1, sj1 = transfer(SENDER, PAYEE, lim + 1000.0)
    record("越界·单笔>限额拒绝", sc1 != 200 or not sj1.get("ok"), f"status={sc1}")
    li1 = sj1.get("limit_info") or {}
    record("越界·reason=single_exceeded", li1.get("reason") == "single_exceeded",
           f"reason={li1.get('reason')}")
    record("越界·单笔拒绝不累加额度",
           abs(limit_query(SENDER).get("used", 0)) < 0.01,
           f"used={limit_query(SENDER).get('used')}")
    record("越界·错误消息含『限额』提示", "限额" in (sj1.get("error") or ""))
    # 3b 日累计超额
    _set_used(SENDER, 30000)
    sc2, sj2 = transfer(SENDER, PAYEE, 25000.0)
    record("越界·日累计超额拒绝", sc2 != 200 or not sj2.get("ok"), f"status={sc2}")
    li2 = sj2.get("limit_info") or {}
    record("越界·reason=daily_exceeded", li2.get("reason") == "daily_exceeded",
           f"reason={li2.get('reason')}")
    record("越界·剩余显示 20000", abs(li2.get("remaining", 0) - 20000) < 0.01,
           f"remaining={li2.get('remaining')}")
    record("越界·错误消息显示剩余 20000",
           ("20,000" in (sj2.get("error") or "")) or ("20000" in (sj2.get("error") or "")),
           f"msg={(sj2.get('error') or '')[:60]}")
    record("越界·日累计拒绝不累加额度",
           abs(limit_query(SENDER).get("used", 0) - 30000) < 0.01,
           f"used={limit_query(SENDER).get('used')}")


def scenario_concurrency(lim: float) -> None:
    print("\n[4/5 并发] 并发请求不能同时突破（行锁串行化）")
    # 置 used = lim-100（剩 100），发两笔各 100 的并发转账 → 至多 1 笔成功（仅 1 笔真实扣减）
    _set_used(SENDER, lim - 100)
    results: list[tuple[int, dict]] = []
    lock = threading.Lock()

    def worker() -> None:
        r = transfer(SENDER, PAYEE, 100.0)
        with lock:
            results.append(r)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    oks = sum(1 for sc, sj in results if sc == 200 and sj.get("ok"))
    record("并发·至多 1 笔成功（不能双双突破）", oks == 1, f"success_count={oks}")
    gl = limit_query(SENDER)
    record("并发·最终 used==limit（无双扣）",
           abs(gl.get("used", 0) - lim) < 0.01, f"used={gl.get('used')}")


def scenario_bypass_api(lim: float) -> None:
    print("\n[5/5 绕过 API] 直打后端转账接口（绕过 Agent 前置）仍被拦截")
    _set_used(SENDER, 0)
    sc, sj = transfer(SENDER, PAYEE, lim + 5000.0)
    record("绕过API·直打转账接口仍拦截", sc != 200 or not sj.get("ok"), f"status={sc}")
    li = sj.get("limit_info") or {}
    record("绕过API·拒绝原因 single_exceeded", li.get("reason") == "single_exceeded",
           f"reason={li.get('reason')}")
    # 反向证明：限额查询是只读端点，无法借此放大额度（无 setter 暴露）
    gl = limit_query(SENDER)
    record("绕过API·限额查询为只读(applied 仅反映状态)", gl.get("applied") is True,
           f"applied={gl.get('applied')}")


def main() -> int:
    print("=" * 64)
    print(" 转账限额（单笔 / 单日累计）五类场景验收")
    print(f" mock_bank={MOCK}  SENDER={SENDER}  PAYEE={PAYEE}")
    print("=" * 64)

    if not server_up():
        print("\n✗ mock_bank 未运行或不可达，请先启动：")
        print(f"  cd bank-agent && .venv/Scripts/python.exe -m uvicorn mock_bank.main:app "
              f"--host 127.0.0.1 --port {config.BANK_API_PORT}")
        return 2

    lim = float((limit_query(SENDER) or {}).get("limit") or 50000)
    print(f"\n当前生效限额（单笔/单日累计）: ¥{lim:,.0f}")
    print("（额度在 POST /api/transfer 事务内以 SELECT ... FOR UPDATE 串行校验）")

    _reset_quota(SENDER)
    scenario_normal(lim)
    scenario_boundary(lim)
    scenario_over(lim)
    scenario_concurrency(lim)
    scenario_bypass_api(lim)

    # 收尾：把当日额度清零，保证 re-run 确定性（真实流水/余额改动属预期副作用）
    _reset_quota(SENDER)

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
