"""
utils/paypass.py —— 支付密码（B 体系：操作安全第二要素）

总原则：操作安全 = 支付密码（你知）+ 验证码（你有），风险分级组合。

安全铁律：
  1. 只存哈希（sha256(code + 盐)，盐派生自用户 id + AUTH_SECRET）——绝不落明文；
  2. 错 3 次锁定至当日 23:59:59（locked_until），锁定状态直接拒绝；
  3. ★含失败的每次校验都留痕（operation_logs，action=pay_password_verify），
     安全中心/后台管理可查；
  4. 通过校验后仅在进程内放置一个短 TTL 的"已通过"标记（uid 维度），
     write_flow 的 verify 队列据此放行 —— 密码本身绝不进入 LangGraph 状态 /
     chat_messages / checkpoint（对话里只落 "[已输入支付密码]"）。
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime

import pymysql

import config
from mock_bank import db  # 连接收口到单一源（组 R）

# 业务常量单源见 rules.py（组 W）：支付密码重试上限
# （错 3 次锁账户至当日 23:59:59 —— 与验证码「作废码」语义不同，故独立常量）。
import rules  # C6：支付密码最大尝试次数运行时从 system_config 读取

_PASS_TTL_SECONDS = 120  # 通过标记有效期：足够走完 resume 流程，又不留长期后门

# 进程内"支付密码已通过"标记：uid -> expiry_ts
_PASSED: dict[int, float] = {}


def _conn():
    return db.get_conn()


def _salt_for(user_id: int) -> bytes:
    """确定性盐：派生自 AUTH_SECRET + uid。同一用户哈希稳定，便于比对。"""
    base = f"{config.AUTH_SECRET}:{int(user_id)}:paypwd".encode("utf-8")
    return hashlib.sha256(base).digest()[:16]


def hash_password(user_id: int, password: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             _salt_for(user_id), 100_000)
    return "pbkdf2$sha256$100000$" + dk.hex()


def has_password(user_id: int) -> bool:
    with _conn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT pay_password_hash FROM users WHERE id=%s", (int(user_id),))
            row = c.fetchone()
    return bool(row and row.get("pay_password_hash"))


def is_locked(user_id: int) -> bool:
    with _conn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT pay_pwd_locked_until FROM users WHERE id=%s", (int(user_id),)
            )
            row = c.fetchone()
    if not row or not row.get("pay_pwd_locked_until"):
        return False
    return datetime.now() < row["pay_pwd_locked_until"]


def mark_passed(user_id: int) -> None:
    _PASSED[int(user_id)] = time.time() + _PASS_TTL_SECONDS


def has_passed(user_id: int) -> bool:
    exp = _PASSED.get(int(user_id))
    if not exp:
        return False
    if time.time() > exp:
        _PASSED.pop(int(user_id), None)
        return False
    # 一次性消费：标记用过即失效（每个 verify 队列项只放行一次）
    _PASSED.pop(int(user_id), None)
    return True


def _log(user_id: int, ok: bool, detail: str) -> None:
    """每次核验（含失败）都留痕。失败不影响业务返回。"""
    try:
        db.log_operation(int(user_id), "pay_password_verify", detail,
                         "success" if ok else "failed")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 支付密码核验留痕失败: {exc}")


def set_password(user_id: int, password: str) -> dict:
    """设置 / 修改支付密码：仅 6 位数字。"""
    if not re.fullmatch(r"\d{6}", (password or "").strip()):
        return {"ok": False, "error": "支付密码必须为 6 位数字"}
    with _conn() as conn:
        with conn.cursor() as c:
            c.execute(
                "UPDATE users SET pay_password_hash=%s, pay_pwd_attempts=0, "
                "pay_pwd_locked_until=NULL WHERE id=%s",
                (hash_password(user_id, password.strip()), int(user_id)),
            )
        conn.commit()
    _log(user_id, True, '{"action": "set_password"}')
    return {"ok": True, "message": "支付密码已设置"}


def verify(user_id: int, password: str) -> dict:
    """支付密码核验：锁定检查 → 比对 → 错 3 次锁到当日 23:59:59。"""
    raw = (password or "").strip()
    if is_locked(user_id):
        remain = _locked_remaining_text(user_id)
        _log(user_id, False, '{"action": "verify", "blocked": "locked"}')
        return {"ok": False, "locked": True, "error": f"支付密码已锁定，请{remain}后再试"}
    if not re.fullmatch(r"\d{6}", raw):
        _log(user_id, False, '{"action": "verify", "reason": "format"}')
        return {"ok": False, "error": "支付密码必须为 6 位数字"}
    with _conn() as conn:
        try:
            with conn.cursor() as c:
                c.execute(
                    "SELECT pay_password_hash, pay_pwd_attempts, pay_pwd_locked_until "
                    "FROM users WHERE id=%s FOR UPDATE", (int(user_id),)
                )
                row = c.fetchone()
                if not row:
                    return {"ok": False, "error": "用户不存在"}
                stored = row.get("pay_password_hash") or ""
                attempts = int(row.get("pay_pwd_attempts") or 0)
                if not stored:
                    conn.commit()
                    return {"ok": False, "need_set": True, "error": "尚未设置支付密码"}
                if hash_password(user_id, raw) == stored:
                    c.execute(
                        "UPDATE users SET pay_pwd_attempts=0, pay_pwd_locked_until=NULL "
                        "WHERE id=%s", (int(user_id),)
                    )
                    conn.commit()
                    mark_passed(user_id)
                    _log(user_id, True, '{"action": "verify"}')
                    return {"ok": True, "message": "支付密码核验通过"}
                attempts += 1
                if attempts >= rules.get_rule('paypass_max_attempts'):
                    # 锁定到当日 23:59:59
                    locked_until = datetime.now().replace(
                        hour=23, minute=59, second=59, microsecond=0
                    )
                    c.execute(
                        "UPDATE users SET pay_pwd_attempts=%s, pay_pwd_locked_until=%s "
                        "WHERE id=%s", (attempts, locked_until, int(user_id))
                    )
                    conn.commit()
                    _log(user_id, False, '{"action": "verify", "locked": true}')
                    return {
                        "ok": False, "locked": True,
                        "error": "支付密码连续错误次数过多，已锁定至今日 23:59:59",
                    }
                c.execute(
                    "UPDATE users SET pay_pwd_attempts=%s WHERE id=%s",
                    (attempts, int(user_id)),
                )
                conn.commit()
                _log(user_id, False, f'{{"action": "verify", "attempts": {attempts}}}')
                max_attempts = rules.get_rule('paypass_max_attempts')
                return {
                    "ok": False,
                    "attempts_left": max_attempts - attempts,
                    "error": f"支付密码不正确，还可尝试 {max_attempts - attempts} 次",
                }
        except Exception:
            conn.rollback()
            raise


def _locked_remaining_text(user_id: int) -> str:
    with _conn() as conn:
        with conn.cursor() as c:
            c.execute("SELECT pay_pwd_locked_until FROM users WHERE id=%s", (int(user_id),))
            row = c.fetchone()
    if not row or not row.get("pay_pwd_locked_until"):
        return "稍后"
    remain = row["pay_pwd_locked_until"] - datetime.now()
    minutes = max(1, int(remain.total_seconds() // 60) + 1)
    return f"{minutes} 分钟"


def verify_record_count(user_id: int) -> int:
    """安全中心"身份核验次数"统计卡：支付密码核验成功次数。"""
    with _conn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT COUNT(*) AS n FROM operation_logs "
                "WHERE user_id=%s AND action='pay_password_verify' AND result='success'",
                (int(user_id),),
            )
            return int((c.fetchone() or {}).get("n") or 0)
