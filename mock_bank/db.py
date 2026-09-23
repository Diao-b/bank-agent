"""
mock_bank/db.py —— pymysql 连接与查询封装（强制 DictCursor）

对外提供：
    get_conn(use_db=True)  连接上下文管理器（自己控制事务）
    fetch_all / fetch_one  读
    execute                写（自动 commit）
    log_operation          写操作日志
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import pymysql

import config


@contextmanager
def get_conn(use_db: bool = True) -> Iterator[pymysql.connections.Connection]:
    """获取连接；use_db=False 时不选库（用于建库）。退出自动关闭。"""
    conn = pymysql.connect(**config.db_config(use_db=use_db))
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - 关闭失败无需抛
            pass


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
