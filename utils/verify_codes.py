"""
utils/verify_codes.py —— 验证码落库 + 收件箱双写（C3 核心改造）

★ 与旧实现（mock_bank 进程内存 dict）的差别：
  1. 验证码写入 MySQL verification_codes（可审计、后台可见、重启不丢）；
  2. 同一事务内写入 notifications（收件箱必达）—— 任何一侧失败整体回滚，
     绝不返回"已发送"；
  3. 外部通道（邮箱/推送）尽力同发，失败不影响收件箱。

幂等：issue_code 复用"同用户同场景未过期未使用"的最新码 ——
  write_flow 的 verify 节点在 interrupt 恢复时会重放，重放不得换码。
校验：code_hash 比对（sha256），成功置 verified、失败计数；错 3 次作废。
过期：check 时顺带把过期未用的历史码批量置 expired（后台状态准确）。
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

import pymysql

import config
import rules
from mock_bank import db  # 连接收口到单一源（组 R）
from utils import inbox

# 业务常量单源见 rules.py（C6 升级：DB 表 system_config 为准）——
# 运行时一律 rules.get_rule(...) 取值，禁止 import 时固化（管理端改参数需即时生效）。


def _conn():
    return db.get_conn()


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def expire_stale(cur: Any) -> None:
    """把已过期但仍是 unused 的码置为 expired（保证后台"状态"列准确）。"""
    cur.execute(
        "UPDATE verification_codes SET status='expired' "
        "WHERE status='unused' AND expires_at < NOW()"
    )


def issue_code(user_id: int, scene: str, purpose: str = "") -> dict:
    """发码：验证表 + 收件箱双写同事务；幂等复用未过期未使用的码。

    返回 {"ok": True, "code", "expires_in", "notification_id", "reused"}。
    失败抛异常（调用方翻译成业务错误）—— 绝不半成功。
    """
    ttl = rules.get_rule('verify_code_ttl_seconds')   # C6：TTL 运行时从 system_config 读取
    with _conn() as conn:
        with conn.cursor() as cur:
            expire_stale(cur)
            cur.execute(
                "SELECT id, code_plain, expires_at, notification_id FROM verification_codes "
                "WHERE user_id=%s AND scene=%s AND status='unused' AND expires_at > NOW() "
                "ORDER BY id DESC LIMIT 1",
                (int(user_id), scene),
            )
            row = cur.fetchone()
            if row:
                reused = True
                code = row["code_plain"]
                expires_at = row["expires_at"]
                notif_id = row["notification_id"]
                if not notif_id:
                    # 历史行没有收件箱消息（理论上不会发生）→ 补一条，保住"收件箱必达"
                    notif_id = inbox.push_notification(
                        user_id, "verify_code", "【小微银行】验证码",
                        _content(code, purpose),
                        meta={"code": code, "purpose": purpose},
                        expires_at=expires_at, cur=cur,
                    )
                    cur.execute("UPDATE verification_codes SET notification_id=%s WHERE id=%s",
                                (notif_id, row["id"]))
            else:
                reused = False
                code = f"{random.randint(0, 999999):06d}"
                expires_at = datetime.now() + timedelta(seconds=ttl)
                # ★ 顺序：先写验证表拿到行，再在同一事务写收件箱；任一失败整体回滚
                cur.execute(
                    "INSERT INTO verification_codes "
                    "(user_id, scene, code_plain, code_hash, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (int(user_id), scene, code, _hash(code), expires_at),
                )
                vc_id = int(cur.lastrowid)
                notif_id = inbox.push_notification(
                    user_id, "verify_code", "【小微银行】验证码",
                    _content(code, purpose),
                    meta={"code": code, "purpose": purpose},
                    expires_at=expires_at, cur=cur,
                )
                cur.execute("UPDATE verification_codes SET notification_id=%s WHERE id=%s",
                            (notif_id, vc_id))
                try:
                    db.log_operation(int(user_id), "verify_send",
                                     {"scene": scene, "vc_id": vc_id}, "success", cur=cur)
                except Exception:  # noqa: BLE001  留痕失败不影响发码
                    pass
        conn.commit()
    # 外部通道并行增强：失败不影响收件箱必达
    inbox.send_external_channel(user_id, "【小微银行】验证码", _content(code, purpose))
    return {
        "ok": True, "code": code, "scene": scene, "expires_in": ttl,
        "notification_id": notif_id, "reused": reused,
        "message": f"验证码已发送至您的收件箱，{ttl} 秒内有效",
    }


def _content(code: str, purpose: str) -> str:
    return (
        f"您的验证码为 {code}，5分钟内有效，用途：{purpose or '身份核验'}。请勿泄露。"
    )


def check_code(user_id: int, code: str, scene: str | None = None) -> dict:
    """校验验证码：哈希比对 + 次数限制 + 过期判定。返回 {"ok", "error"}。"""
    raw = (code or "").strip()
    with _conn() as conn:
        try:
            with conn.cursor() as cur:
                expire_stale(cur)
                cur.execute(
                    "SELECT id, code_hash, scene, attempts, status, expires_at, notification_id "
                    "FROM verification_codes WHERE user_id=%s AND status='unused' "
                    "ORDER BY id DESC LIMIT 1",
                    (int(user_id),),
                )
                row = cur.fetchone()
                if not row:
                    return {"ok": False, "error": "验证码不存在或已过期，请重新获取"}
                if scene and row["scene"] != scene:
                    return {"ok": False, "error": "验证码用途不符，请重新获取"}
                if datetime.now() > row["expires_at"]:
                    cur.execute("UPDATE verification_codes SET status='expired' WHERE id=%s",
                                (row["id"],))
                    conn.commit()
                    return {"ok": False, "error": "验证码已过期，请重新获取"}
                if hashlib.sha256(raw.encode("utf-8")).hexdigest() != row["code_hash"]:
                    attempts = int(row["attempts"]) + 1
                    status = ("void" if attempts >= rules.get_rule('verify_code_max_attempts')
                             else "unused")
                    cur.execute(
                        "UPDATE verification_codes SET attempts=%s, status=%s WHERE id=%s",
                        (attempts, status, row["id"]),
                    )
                    conn.commit()
                    if status == "void":
                        return {"ok": False, "error": "验证码错误次数过多，本次操作已作废，请重新发起"}
                    return {"ok": False, "error": "验证码不正确"}
                cur.execute(
                    "UPDATE verification_codes SET status='verified', verified_at=NOW() WHERE id=%s",
                    (row["id"],),
                )
                # 收件箱侧：码已验证成功，标记消息已读（用户已"用过手机"）
                if row.get("notification_id"):
                    cur.execute(
                        "UPDATE notifications SET is_read=1 "
                        "WHERE id=%s AND user_id=%s AND type='verify_code'",
                        (row["notification_id"], int(user_id)),
                    )
                try:
                    db.log_operation(int(user_id), "verify_check",
                                     {"scene": row["scene"]}, "success", cur=cur)
                except Exception:  # noqa: BLE001
                    pass
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"ok": True}
