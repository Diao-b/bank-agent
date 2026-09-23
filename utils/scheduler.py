"""utils/scheduler.py —— 轻量后台调度器（P1.2）

把「订阅到期补扣 + 跨行到期清算」从**读接口惰性触发**升级为**独立周期任务**：
用户不访问任何页面，到期扣款 / 到账 / 退票也会发生（离线用户不再一直"卡在在途"）。

设计要点：
  - 单进程单例：一个 daemon 线程 + 固定间隔；进程退出线程即结束。
  - 非重入：上一轮未跑完则跳过本轮（长任务不堆叠）。
  - 并发安全：底层 `utils/settlement_engine` 自带 `SELECT ... FOR UPDATE` 行锁 + 幂等去重，
    与读接口的惰性触发、乃至多实例并发运行都不会重复扣款 / 重复退票。
  - 失败隔离：单用户失败不影响其它用户；整轮失败只记录，不退出线程。
  - 可观测：每轮汇总写入 `status()`，供只读接口 `GET /api/housekeeping` 暴露。

开关（config）：`HOUSEKEEPING_ENABLED`（默认开）、`HOUSEKEEPING_INTERVAL_SECONDS`（默认 60s）、
`HOUSEKEEPING_BATCH_USERS`（单轮到期订阅用户上限）。
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime

import config
from mock_bank import db

_thread: threading.Thread | None = None
_wake = threading.Event()       # 退出/唤醒等待
_reentry = threading.Lock()     # 非重入
_state: dict = {
    "enabled": False,
    "interval_seconds": 0,
    "runs": 0,
    "started_at": None,
    "last_run_at": None,
    "last_duration_ms": None,
    "last_charged": 0,
    "last_settled": 0,
    "last_returned": 0,
    "last_backfill_users": 0,
    "last_errors": 0,
    "last_error": None,
}


def status() -> dict:
    """当前调度器状态快照（供只读接口暴露）。"""
    s = dict(_state)
    s["running"] = bool(_thread and _thread.is_alive())
    return s


def _due_subscription_users() -> list[int]:
    """有「到期未扣」订阅的用户（去重、限量）。避免对全部用户逐个空跑。"""
    limit = max(1, int(config.HOUSEKEEPING_BATCH_USERS))
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT user_id FROM subscriptions "
                "WHERE status='active' AND next_due IS NOT NULL AND next_due <= CURDATE() "
                "ORDER BY user_id LIMIT %s",
                (limit,),
            )
            return [int(r["user_id"]) for r in cur.fetchall()]


def run_once() -> dict:
    """跑一轮：跨行到期清算（全量）+ 订阅到期补扣（按到期用户）。返回本轮汇总。

    ★ 对照读接口的惰性触发：本函数**不经过任何 HTTP 读接口**，直接驱动引擎，
      因此「无人访问也会推进」就是它存在的意义。
    """
    from utils import settlement_engine  # 惰性引用，避免循环导入

    if not _reentry.acquire(blocking=False):
        return {"skipped": "reentry"}  # 上一轮未结束，跳过本轮

    t0 = time.time()
    charged = settled = returned = ded_users = errors = 0
    last_err: str | None = None
    try:
        # 1) 跨行到期清算：引擎支持 user_id=None → 一条查询覆盖全部到期在途
        try:
            stl = settlement_engine.process_due_settlements(None)
            settled = int(stl.get("settled_count") or 0)
            returned = int(stl.get("returned_count") or 0)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            last_err = f"settlements: {exc}"

        # 2) 订阅到期补扣：只对「有到期订阅」的用户跑
        try:
            for uid in _due_subscription_users():
                try:
                    ded = settlement_engine.run_due_deductions(uid)
                    ded_users += 1
                    charged += int(ded.get("charged_count") or 0)
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    last_err = f"uid={uid}: {exc}"
        except Exception as exc:  # noqa: BLE001
            errors += 1
            last_err = f"due-users: {exc}"

        dur = int((time.time() - t0) * 1000)
        _state.update(
            runs=int(_state["runs"]) + 1,
            last_run_at=datetime.now().isoformat(timespec="seconds"),
            last_duration_ms=dur,
            last_charged=charged,
            last_settled=settled,
            last_returned=returned,
            last_backfill_users=ded_users,
            last_errors=errors,
            last_error=last_err,
        )
        return {"ok": True, "charged": charged, "settled": settled, "returned": returned,
                "deduction_users": ded_users, "errors": errors, "duration_ms": dur}
    finally:
        _reentry.release()


def _loop() -> None:
    interval = max(1, int(config.HOUSEKEEPING_INTERVAL_SECONDS))
    while not _wake.is_set():
        try:
            run_once()
        except Exception:  # noqa: BLE001  线程不能因单轮异常而死
            traceback.print_exc()
        _wake.wait(interval)   # 可被 stop() 立即唤醒


def start() -> bool:
    """启动后台调度线程（幂等）。返回 True 表示本次真正启动。"""
    global _thread
    if not config.HOUSEKEEPING_ENABLED:
        print("[scheduler] 已禁用（HOUSEKEEPING_ENABLED=0）")
        return False
    if _thread and _thread.is_alive():
        return False
    _wake.clear()
    _state.update(
        enabled=True,
        interval_seconds=max(1, int(config.HOUSEKEEPING_INTERVAL_SECONDS)),
        started_at=datetime.now().isoformat(timespec="seconds"),
    )
    _thread = threading.Thread(target=_loop, daemon=True, name="housekeeping-scheduler")
    _thread.start()
    print(f"[scheduler] 已启动：每 {_state['interval_seconds']}s 跑一轮 "
          f"跨行到期清算 + 订阅到期补扣（无需用户访问）")
    return True


def stop() -> None:
    """通知后台线程退出（进程退出前调用；daemon 线程本身会随进程结束）。"""
    _wake.set()
