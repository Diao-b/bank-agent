"""idempotency.py —— 写操作幂等助手（Phase 4 升级，复刻 mock_bank/main.py 算法）

★ 与 upgrade_idempotency.sql 的语义一致：
    status='pending' 占位（并发抢锁）→ 'done' 终态可回放；业务失败删占位允许重试。
    uk_idem_user(user_id, idem_key) 把并发串行化；request_hash 防同 key 打不同请求。

★ 与 mock_bank 的不同：本模块把"占位 / 业务 / 终态"放进**同一事务**（调用方传 cur），
  保证"扣款成功"与"幂等落 done"要么都成、要么都回滚——避免"钱加了但幂等没落"导致
  同 key 重试被 IN_FLIGHT 卡死。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

_IDEM_TTL_HOURS = 24

# 复用 mock_bank 的连接收口（单一数据源，组 R）
from mock_bank import db  # noqa: E402


def req_hash(method: str, path: str, body: str) -> str:
    """method|path|body 的 sha256 —— 防"同 Idempotency-Key 打不同请求"。"""
    return hashlib.sha256(
        (method + "|" + path + "|" + (body or "")).encode("utf-8")
    ).hexdigest()


def lookup(uid: int, idem_key: str) -> dict | None:
    """查是否已存在该 key 的占位 / 终态（含未过期校验）。"""
    try:
        return db.fetch_one(
            "SELECT request_hash, response_json, status FROM idempotency_records "
            "WHERE user_id=%s AND idem_key=%s AND expires_at > NOW()",
            (int(uid), idem_key),
        )
    except Exception:  # noqa: BLE001 表缺失 / DB 异常 → 退化为"无幂等"，不阻断
        return None


def payload_to_json(resp: dict) -> str:
    return json.dumps(resp, ensure_ascii=False, default=str)


def parse_payload(raw: Any) -> dict | None:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def claim(cur: Any, uid: int, idem_key: str, action: str, request_hash: str) -> bool:
    """在调用方事务内插占位（唯一键抢占）。返回 False = 已被占用（并发撞车）。"""
    try:
        cur.execute(
            "INSERT INTO idempotency_records "
            "(idem_key, user_id, action, request_hash, status, expires_at) "
            "VALUES (%s,%s,%s,%s,'pending', DATE_ADD(NOW(), INTERVAL %s HOUR))",
            (idem_key, int(uid), action, request_hash, _IDEM_TTL_HOURS),
        )
        return True
    except Exception:  # noqa: BLE001 IntegrityError（并发抢到）/ 表缺失
        return False


def finalize(cur: Any, uid: int, idem_key: str, payload: dict | None) -> None:
    """在同一事务内：payload 非 None → 落 done 终态；None → 删占位允许重试。"""
    if payload is None:
        cur.execute(
            "DELETE FROM idempotency_records "
            "WHERE user_id=%s AND idem_key=%s AND status='pending'",
            (int(uid), idem_key),
        )
    else:
        cur.execute(
            "UPDATE idempotency_records SET status='done', response_json=%s "
            "WHERE user_id=%s AND idem_key=%s AND status='pending'",
            (payload_to_json(payload), int(uid), idem_key),
        )
