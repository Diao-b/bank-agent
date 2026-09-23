"""
scripts/test_runtime_state.py —— P1.4 进程内状态治理 · 验收自测

背景：原先两处安全/正确性相关的状态是**进程内**的，多 worker 下会不一致：
  ① 登录失败计数 run_server._LOGIN_FAILS（进程内 dict）→ N worker 放宽 N 倍；
  ② 支付密码"已通过"标记 paypass._PASSED（进程内 dict）→ verify 与 resume 落到不同进程就"丢"。
两者已迁到 DB（login_attempts / paypass_passed，迁移 upgrade_runtime_state.sql）。

覆盖：
  1. 登录锁定写进 DB：本进程 record_failure ×5 → 另一个进程（run_server:8178）同账号登录被 429
     —— 证明锁定是**跨进程**生效的；
  2. 成功登录清空计数（DB 行消失）；
  3. 支付密码"已通过"标记落 DB、可跨进程读到、**一次性消费**、TTL 过期即失效。

用法（bank-agent/ 下，.venv，需 MySQL；[1][2] 需 run_server:8178）：
    .venv/Scripts/python.exe scripts/test_runtime_state.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config
from mock_bank import db
from utils import login_guard, paypass

PROXY = "http://127.0.0.1:8178"
STAMP = str(int(time.time()))[-6:]
PROBE_USER = f"rt_probe_{STAMP}"

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _cleanup() -> None:
    db.execute("DELETE FROM login_attempts WHERE username LIKE %s", ("rt\\_probe\\_%",))
    db.execute("DELETE FROM paypass_passed WHERE user_id >= %s", (990000,))


def main() -> int:
    print("=" * 66)
    print(" P1.4 进程内状态治理 · 验收自测")
    print("=" * 66)
    _cleanup()

    # ---------------- 1) 登录锁定：DB 记录 + 跨进程生效 ----------------
    print(f"\n[1] 登录锁定（DB）：{PROBE_USER} 连续失败")
    maxf, locks = rules_max(), rules_lock()
    locked = False
    for i in range(maxf):
        n, locked = login_guard.record_failure(PROBE_USER, maxf, locks)
    record(f"累计失败 {maxf} 次后触发锁定", locked, f"n={n} locked={locked}")
    remain = login_guard.locked_remaining_minutes(PROBE_USER)
    record("剩余锁定分钟可读（DB）", remain is not None, f"remain={remain}")
    # ★ 关键：由「另一个进程」= run_server(8178) 判定 → 证明跨进程
    try:
        r = requests.post(PROXY + "/api/auth/login",
                          json={"username": PROBE_USER, "password": "whatever123"}, timeout=5)
        record("★ 另一进程（8178）同账号登录被 429（锁定跨进程）", r.status_code == 429,
               f"status={r.status_code} {str(r.json())[:80]}")
    except Exception as exc:  # noqa: BLE001
        record("★ 另一进程（8178）同账号登录被 429（锁定跨进程）", False, f"8178 未启动？{exc}")

    # ---------------- 2) 成功登录清空计数 ----------------
    print("\n[2] 成功登录清空计数（用真实账号 user1）")
    try:
        requests.post(PROXY + "/api/auth/login",
                      json={"username": "user1", "password": "wrong-guess"}, timeout=5)
        n_before = int(db.fetch_one(
            "SELECT fail_count AS n FROM login_attempts WHERE username=%s", ("user1",))["n"])
        r_ok = requests.post(PROXY + "/api/auth/login",
                             json={"username": "user1", "password": "123456"}, timeout=5)
        row_after = db.fetch_one("SELECT fail_count AS n FROM login_attempts WHERE username=%s",
                                 ("user1",))
        record("登录成功后计数清零（DB 行删除）",
               r_ok.status_code == 200 and r_ok.json().get("ok") and row_after is None,
               f"login={r_ok.status_code} n_before={n_before} row_after={row_after}")
    except Exception as exc:  # noqa: BLE001
        record("登录成功后计数清零（DB 行删除）", False, str(exc))

    # ---------------- 3) 支付密码"已通过"标记：DB + 一次性 + TTL ----------------
    print('\n[3] 支付密码"已通过"标记（DB）')
    uid = 990123
    paypass.mark_passed(uid)
    row = db.fetch_one("SELECT user_id, expires_at FROM paypass_passed WHERE user_id=%s", (uid,))
    record("标记写入 DB（跨进程可读）", row is not None, f"{row}")
    first = paypass.has_passed(uid)
    second = paypass.has_passed(uid)
    record("★ 一次性消费：第一次 True、第二次 False", first is True and second is False,
           f"first={first} second={second}")
    record("消费后 DB 行已删除",
           db.fetch_one("SELECT 1 AS x FROM paypass_passed WHERE user_id=%s", (uid,)) is None)

    # TTL 过期
    old_ttl = paypass._PASS_TTL_SECONDS
    paypass._PASS_TTL_SECONDS = 1
    try:
        uid2 = 990124
        paypass.mark_passed(uid2)
        time.sleep(1.3)
        expired = paypass.has_passed(uid2)
        record("★ TTL 过期后失效（不会误放行）", expired is False, f"expired_has_passed={expired}")
    finally:
        paypass._PASS_TTL_SECONDS = old_ttl

    _cleanup()
    print("\n（测试数据已清理）")
    print("\n" + "=" * 66)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f" 结果：{passed}/{total} 通过")
    for n, ok, _ in RESULTS:
        if not ok:
            print("   -", n)
    print("=" * 66)
    return 0 if passed == total else 1


def rules_max() -> int:
    import rules
    return int(rules.LOGIN_MAX_FAILS)


def rules_lock() -> int:
    import rules
    return int(rules.LOGIN_LOCK_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
