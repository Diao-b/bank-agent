"""mock_bank/db.py —— pymysql 连接池与查询封装（强制 DictCursor）

对外提供：
    get_conn(use_db=True)  连接上下文管理器（从池借出 / 归还；自己控制事务）
    fetch_all / fetch_one  读
    execute                写（自动 commit）
    log_operation          写操作日志
    pool_stats             连接池统计（只读）

★ P1.3 连接池：把「每次请求新建 + 关闭连接」改为**进程内复用**，省掉每次的 TCP + 认证握手。
  - 借出即独占：pymysql 连接**非线程安全**，池保证同一时刻只有一个使用者（这正是并发安全的根本）。
  - 归还即清场：`rollback()` 掉未提交事务 —— 与「关闭连接」语义一致，避免 REPEATABLE READ
    快照 / 半截事务"串味"给下一个借用者（不 rollback 会让下一个用户读到旧快照）。
  - 借出前 `ping(reconnect=True)` 探活；空闲过久的连接回收；池满则短暂等待，超时开"溢出"连接
    （用完即弃），**绝不因池满而失败**。
  - `use_db=False`（建库/初始化）不进池：池内连接都绑定了 DB。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import pymysql

import config

_MAX = max(1, int(config.DB_POOL_SIZE))
_IDLE_SECONDS = max(0, int(config.DB_POOL_IDLE_SECONDS))
_WAIT_SECONDS = max(0.1, float(config.DB_POOL_WAIT_SECONDS))

_free: deque[tuple[Any, float]] = deque()   # (连接, 归还时间戳)
_live = 0                                   # 已创建且未关闭（空闲 + 借出）—— 仅统计"池内"连接
_overflow_total = 0                         # 累计开出的溢出连接数（观测用）
_cv = threading.Condition()                 # 保护 _free / _live / _overflow_total


class _Lease:
    __slots__ = ("conn", "pooled")

    def __init__(self, conn: Any, pooled: bool) -> None:
        self.conn = conn
        self.pooled = pooled


def _new_conn() -> Any:
    return pymysql.connect(**config.db_config(use_db=True))


def _discard(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001  关闭失败无需抛
        pass


def _acquire() -> _Lease:
    """借出一条连接（池内复用优先；池满等待；超时开溢出）。"""
    global _live, _overflow_total
    deadline = time.time() + _WAIT_SECONDS
    while True:
        conn = None
        reserved = False
        with _cv:
            now = time.time()
            while _free:
                cand, ts = _free.pop()
                if _IDLE_SECONDS and (now - ts) > _IDLE_SECONDS:
                    _live = max(0, _live - 1)   # 过期回收
                    _discard(cand)
                    continue
                conn = cand
                break
            if conn is None:
                if _live < _MAX:
                    _live += 1                  # 先占额度，再在锁外真正建连（避免建连阻塞全局）
                    reserved = True

        if conn is not None:
            try:
                conn.ping(reconnect=True)       # 探活（坏连接丢弃重建）
                return _Lease(conn, True)
            except Exception:  # noqa: BLE001
                with _cv:
                    _live = max(0, _live - 1)
                    _cv.notify()
                _discard(conn)
                continue

        if reserved:
            try:
                return _Lease(_new_conn(), True)
            except Exception:
                with _cv:
                    _live = max(0, _live - 1)
                    _cv.notify()
                raise

        # 池满：短暂等待，超时则开溢出连接（绝不失败）
        remaining = deadline - time.time()
        if remaining <= 0:
            with _cv:
                _overflow_total += 1
            return _Lease(_new_conn(), False)
        with _cv:
            _cv.wait(min(remaining, 0.5))


def _release(lease: _Lease) -> None:
    """归还连接：清场（rollback）后放回池；溢出/坏连接直接关闭。"""
    global _live
    conn = lease.conn
    if not lease.pooled:
        _discard(conn)
        return
    try:
        conn.rollback()                         # 清场：防未提交事务/旧快照串给下一个借用者
    except Exception:  # noqa: BLE001
        with _cv:
            _live = max(0, _live - 1)
            _cv.notify()
        _discard(conn)
        return
    with _cv:
        _free.append((conn, time.time()))
        _cv.notify()


@contextmanager
def get_conn(use_db: bool = True) -> Iterator[Any]:
    """获取连接；use_db=False 时不选库（用于建库，且不进池）。退出归还/关闭。"""
    if not use_db:
        conn = pymysql.connect(**config.db_config(use_db=False))
        try:
            yield conn
        finally:
            _discard(conn)
        return

    lease = _acquire()
    try:
        yield lease.conn
    finally:
        _release(lease)


def pool_stats() -> dict:
    """连接池统计（只读，供观测/自检）。"""
    with _cv:
        return {
            "max": _MAX,
            "live": _live,
            "free": len(_free),
            "leased": max(0, _live - len(_free)),
            "overflow_total": _overflow_total,
            "idle_seconds": _IDLE_SECONDS,
            "wait_seconds": _WAIT_SECONDS,
        }


# --------------------------------------------------------------------------- #
#  便捷查询
# --------------------------------------------------------------------------- #
def fetch_all(sql: str, args: Sequence[Any] | None = None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            rows = cur.fetchall()
    return list(rows)


def fetch_one(sql: str, args: Sequence[Any] | None = None) -> dict | None:
    rows = fetch_all(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: Sequence[Any] | None = None) -> int:
    """执行写语句并 commit，返回 lastrowid。"""
    with get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(sql, args or ())
                last_id = cur.lastrowid
            conn.commit()
            return last_id
        except Exception:
            conn.rollback()
            raise


def log_operation(user_id: int, action: str, detail: Any, result: str,
                  cur: Any = None) -> None:
    """记录操作日志：写端点必须调用。

    ★ 传 cur 时复用调用方事务 —— 留痕必须与业务同生共死：
      业务成功但日志另起连接写失败 = 一次没有留痕的资金变动，审计上等于没发生过。
      不传 cur 时行为不变（自己开连接并提交），供旁路 / 失败场景使用。
    """
    import json

    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    if cur is not None:
        cur.execute(
            "INSERT INTO operation_logs (user_id, action, detail, result) VALUES (%s, %s, %s, %s)",
            (user_id, action, detail, result),
        )
        return None
    return execute(
        "INSERT INTO operation_logs (user_id, action, detail, result) VALUES (%s, %s, %s, %s)",
        (user_id, action, detail, result),
    )
