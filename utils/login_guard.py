"""utils/login_guard.py —— 登录失败计数 / 临时锁定（P1.4 进程内状态治理）

原先 `run_server._LOGIN_FAILS` 是**进程内 dict**：N 个 worker 等于把"连续失败 N_MAX 次锁定"
放宽成 N_MAX × worker 数 —— 这是一处安全状态被进程边界稀释的问题。

现在改为 DB 表 `login_attempts`（P1.4 迁移 upgrade_runtime_state.sql）：多 worker / 多实例
共享同一份计数与锁定，语义与原先一致（成功登录清零；锁定到期后计数继续累计）。

时间一律用 **DB 的 NOW()**，避免应用与数据库时钟不一致。
"""
from __future__ import annotations

from mock_bank import db


def locked_remaining_minutes(username: str) -> int | None:
    """返回剩余锁定分钟数（向上取整）；未锁定返回 None。"""
    if not username:
        return None
    row = db.fetch_one(
        "SELECT TIMESTAMPDIFF(SECOND, NOW(), locked_until) AS secs "
        "FROM login_attempts "
        "WHERE username=%s AND locked_until IS NOT NULL AND locked_until > NOW()",
        (username,),
    )
    secs = (row or {}).get("secs")
    if secs is None:
        return None
    return max(1, int(int(secs) // 60) + 1)


def record_failure(username: str, max_fails: int, lock_seconds: int) -> tuple[int, bool]:
    """登记一次失败，返回 (累计失败数, 是否本次触发锁定)。原子 upsert。"""
    with db.get_conn() as conn:
        try:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO login_attempts (username, fail_count, first_fail_at) "
                    "VALUES (%s, 1, NOW()) "
                    "ON DUPLICATE KEY UPDATE fail_count = fail_count + 1",
                    (username,),
                )
                c.execute("SELECT fail_count FROM login_attempts WHERE username=%s", (username,))
                n = int((c.fetchone() or {}).get("fail_count") or 0)
                locked = n >= int(max_fails)
                if locked:
                    c.execute(
                        "UPDATE login_attempts SET locked_until=DATE_ADD(NOW(), INTERVAL %s SECOND) "
                        "WHERE username=%s",
                        (int(lock_seconds), username),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return n, locked


def clear_failures(username: str) -> None:
    """登录成功后清零（幂等）。"""
    db.execute("DELETE FROM login_attempts WHERE username=%s", (username,))
