"""
scripts/test_db_pool.py —— P1.3 连接池与并发安全 · 验收自测

背景：`mock_bank/db.py` 原先每次 `get_conn()` 都新建 + 关闭连接（每请求一次 TCP + 认证握手）；
且若调用方未提交，连接关闭虽会隐式回滚，但**池化后若不在归还时清场**，会有两类事故：
  ① 未提交事务串给下一个借用者；
  ② REPEATABLE READ 旧快照泄漏，下一个用户读到"过期数据"。

覆盖：
  1. 复用：连续 get_conn 复用同一连接（MySQL thread_id 稳定）；
  2. 归还清场：块内未提交的写入，归还后不可见（已回滚），但块内可见；
  3. ★无旧快照：借用者 A 读过快照 → 归还 → 他人提交新数据 → 下一个借用者能读到新数据；
  4. 并发：多线程读写不报错、结果自洽；
  5. 池上限：顺序复用不会无限增长（live ≤ max）；
  6. 溢出：池满（人为把 max 调到 1）时仍能借到连接（溢出连接，不失败）；
  7. use_db=False 路径仍可用（建库用，不进池）。

用法（bank-agent/ 下，.venv，需 MySQL；不需起 8003）：
    .venv/Scripts/python.exe scripts/test_db_pool.py
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mock_bank import db  # noqa: E402

SENTINEL_ACTION = "pooldbg"
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _tid(conn) -> int:
    try:
        return int(conn.thread_id())
    except Exception:  # noqa: BLE001
        return id(conn)


def _cleanup() -> None:
    db.execute(f"DELETE FROM operation_logs WHERE action='{SENTINEL_ACTION}'")


def _sentinel_count() -> int:
    return int(db.fetch_one(
        f"SELECT COUNT(*) AS n FROM operation_logs WHERE action='{SENTINEL_ACTION}'")["n"])


def main() -> int:
    print("=" * 64)
    print(" P1.3 连接池与并发安全 · 验收自测")
    print("=" * 64)
    _cleanup()

    # ---------------- 1) 复用 ----------------
    print("\n[1] 连接复用（thread_id 稳定）")
    tids = []
    for _ in range(6):
        with db.get_conn() as conn:
            tids.append(_tid(conn))
    record("连续 6 次 get_conn 复用同一连接", len(set(tids)) == 1, f"distinct={len(set(tids))} {tids}")

    # ---------------- 2) 归还清场（未提交 → 回滚）----------------
    print("\n[2] 归还清场：未提交写入不串给下一个借用者")
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO operation_logs (user_id, action, detail, result) "
                        f"VALUES (1, '{SENTINEL_ACTION}', 'in-block', 'failed')")
            cur.execute(f"SELECT COUNT(*) AS n FROM operation_logs WHERE action='{SENTINEL_ACTION}'")
            in_block = int(cur.fetchone()["n"])
        # 故意不 commit
    record("块内可见（写入生效）", in_block == 1, f"in_block={in_block}")
    record("归还后不可见（已回滚，未串味）", _sentinel_count() == 0, f"after={_sentinel_count()}")

    # ---------------- 3) 无旧快照泄漏 ----------------
    print("\n[3] 无旧快照：借用者读过 → 归还 → 他人写 → 下一个借用者读到新数据")
    with db.get_conn() as conn:          # A：先读一次，开启 REPEATABLE READ 快照
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM operation_logs WHERE action='{SENTINEL_ACTION}'")
            a_seen = int(cur.fetchone()["n"])
    db.execute(f"INSERT INTO operation_logs (user_id, action, detail, result) "
               f"VALUES (1, '{SENTINEL_ACTION}', 'by-other', 'failed')")   # 他人提交新数据
    with db.get_conn() as conn:          # 复用同一连接：若未清场快照，这里会读到旧值
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM operation_logs WHERE action='{SENTINEL_ACTION}'")
            b_seen = int(cur.fetchone()["n"])
    record("★ 新借用者读到最新提交（快照已清）", a_seen == 0 and b_seen == 1,
           f"A={a_seen} B={b_seen}")

    # ---------------- 4) 并发读写 ----------------
    print("\n[4] 并发：8 线程 × 15 次读写")
    errors: list[str] = []
    lock = threading.Lock()

    def worker(k: int) -> None:
        for i in range(15):
            try:
                db.fetch_one("SELECT 1 AS one")
                db.execute(f"INSERT INTO operation_logs (user_id, action, detail, result) "
                           f"VALUES (1, '{SENTINEL_ACTION}', 't{k}-{i}', 'failed')")
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"t{k}#{i}: {exc}")

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    record("并发无异常", not errors, f"errors={len(errors)} {errors[:2]}")
    worker_rows = int(db.fetch_one(
        "SELECT COUNT(*) AS n FROM operation_logs WHERE action=%s AND detail LIKE %s",
        (SENTINEL_ACTION, "t%"))["n"])
    record("并发写入数正确 (8×15)", worker_rows == 120, f"count={worker_rows}")

    # ---------------- 5) 池上限 ----------------
    print("\n[5] 池上限与统计")
    st = db.pool_stats()
    record("live 不超过 max", st["live"] <= st["max"], f"{st}")
    record("统计含 leased/free/overflow", {"leased", "free", "overflow_total"} <= set(st))

    # ---------------- 6) 溢出（池满不失败）----------------
    print("\n[6] 池满溢出：排空池后把 max 调到 1")
    old_max, old_wait = db._MAX, db._WAIT_SECONDS
    with db._cv:                      # 排空空闲连接，便于确定性触发"池满"
        while db._free:
            _c, _ts = db._free.pop()
            db._live = max(0, db._live - 1)
            db._discard(_c)
    db._MAX, db._WAIT_SECONDS = 1, 0.2
    try:
        with db.get_conn() as held:   # 占住唯一池位
            _tid(held)
            with db.get_conn() as second:
                ok2 = _tid(second) is not None
        overflow = db.pool_stats()["overflow_total"]
        record("池满时仍借到连接（溢出，不失败）", ok2 and overflow >= 1,
               f"ok={ok2} overflow_total={overflow}")
    finally:
        db._MAX, db._WAIT_SECONDS = old_max, old_wait

    # ---------------- 7) use_db=False ----------------
    print("\n[7] use_db=False 路径")
    with db.get_conn(use_db=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS one")
            record("use_db=False 可用（不进池）", int(cur.fetchone()["one"]) == 1)

    _cleanup()
    print("\n（测试留痕已清理）")
    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f" 结果：{passed}/{total} 通过")
    for n, ok, _ in RESULTS:
        if not ok:
            print("   -", n)
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
