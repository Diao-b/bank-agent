"""utils/op_auth.py —— 一次性操作授权票据（one-time op-auth token）。

P0.2：堵住"直连写端点绕过 write_flow 的 verify/paypass"的后门。

设计要点：
  - 票据由 8178（run_server / write_flow）在"确认走完核实流程"后签发；
  - 8003 的各资金写端点（transfer / buy / redeem / freeze / ...）消费票据；
  - 票据：一次性（消费即置 consumed）、按 scene 绑定、短 TTL、HMAC 签名
    （复用 config.AUTH_SECRET，与登录 token 同源不同用途）；
  - 直连 8003 而无有效票据 → 4xx（审计 P0.2 验收点）；生产无任何绕过分支。

★ 不改变任何业务字段语义：票据只走请求头 `X-Op-Token`，不进请求体/响应体。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Request

import config
from mock_bank import db
from utils.deps import current_uid


# --------------------------------------------------------------------------- #
#  底层：票据格式 = "{uid}.{scene}.{nonce}.{exp}.{sig}"，DB 仅存 sha256(raw)
# --------------------------------------------------------------------------- #
def _conn():
    return db.get_conn()


def _ttl() -> int:
    try:
        return int(config.OP_TOKEN_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        return 300


def _sign(payload: str) -> str:
    return hmac.new(
        config.AUTH_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _verify_sig(payload: str, sig: str) -> bool:
    expected = _sign(payload).encode("utf-8")
    return hmac.compare_digest(expected, sig.encode("utf-8"))


def issue_op_token(uid: int, scene: str, purpose: str = "") -> dict:
    """签发一次性操作票据。返回 {ok, token, expires_in, scene, purpose}。"""
    nonce = os.urandom(16).hex()
    exp = int(time.time()) + _ttl()
    payload = f"{int(uid)}.{scene}.{nonce}.{exp}"
    sig = _sign(payload)
    raw = f"{payload}.{sig}"  # 明文票据（含 exp / sig，便于消费端本地预校验）
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    expires_at = datetime.now() + timedelta(seconds=_ttl())
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO op_tickets (user_id, scene, token_hash, status, expires_at) "
                "VALUES (%s, %s, %s, 'active', %s)",
                (int(uid), scene, token_hash, expires_at),
            )
        conn.commit()
    return {
        "ok": True,
        "token": raw,
        "expires_in": _ttl(),
        "scene": scene,
        "purpose": purpose,
    }


def consume_op_token(uid: int, scene: str, raw: str | None) -> dict:
    """消费一次性票据。成功 {ok:True}；失败 {ok:False, error, code}。

    失败码：OP_TOKEN_MISSING / OP_TOKEN_INVALID / OP_TOKEN_EXPIRED /
            OP_TOKEN_USED / OP_TOKEN_MISMATCH（均映射到 HTTP 401）。
    """
    if not raw or not raw.strip():
        return {"ok": False, "error": "缺少操作授权票据（X-Op-Token）",
                "code": "OP_TOKEN_MISSING"}
    raw = raw.strip()
    parts = raw.split(".")
    if len(parts) != 5:
        return {"ok": False, "error": "操作授权票据格式非法", "code": "OP_TOKEN_INVALID"}
    uid_s, scene_s, _nonce, exp_s, sig = parts
    # 1) 签名校验（防篡改：uid / scene / exp 都参与签名）
    if not _verify_sig(f"{uid_s}.{scene_s}.{_nonce}.{exp_s}", sig):
        return {"ok": False, "error": "操作授权票据签名无效", "code": "OP_TOKEN_INVALID"}
    # 2) uid / scene 与调用方一致（防 A 的票据被 B 冒用 / 跨场景复用）
    if uid_s != str(int(uid)) or scene_s != scene:
        return {"ok": False, "error": "操作授权票据与本次操作不匹配",
                "code": "OP_TOKEN_MISMATCH"}
    # 3) 未过期（本地预校验，DB 仍为准）
    try:
        if int(exp_s) < int(time.time()):
            return {"ok": False, "error": "操作授权票据已过期", "code": "OP_TOKEN_EXPIRED"}
    except ValueError:
        return {"ok": False, "error": "操作授权票据格式非法", "code": "OP_TOKEN_INVALID"}
    # 4) DB 单次消费（SELECT ... FOR UPDATE + 状态翻转，并发下严格单次）
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with _conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status FROM op_tickets "
                    "WHERE token_hash=%s AND user_id=%s AND scene=%s "
                    "FOR UPDATE",
                    (token_hash, int(uid), scene),
                )
                row = cur.fetchone()
                if not row or row["status"] != "active":
                    # 行不存在 / 已消费 / 已过期（过期由 expire 任务或下方兜底处理）
                    cur.execute(
                        "SELECT id FROM op_tickets WHERE token_hash=%s AND user_id=%s "
                        "AND scene=%s AND status='active' AND expires_at > NOW() "
                        "FOR UPDATE",
                        (token_hash, int(uid), scene),
                    )
                    if not cur.fetchone():
                        conn.commit()
                        return {"ok": False, "error": "操作授权票据无效或已使用",
                                "code": "OP_TOKEN_USED"}
                cur.execute(
                    "UPDATE op_tickets SET status='consumed', consumed_at=NOW() "
                    "WHERE token_hash=%s AND user_id=%s AND scene=%s",
                    (token_hash, int(uid), scene),
                )
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
            raise
    return {"ok": True}


class RequireOpToken:
    """FastAPI 依赖：要求当前请求携带合法且未使用的操作授权票据（X-Op-Token）。

    用法：在写端点加参数 `_: None = Depends(RequireOpToken("<scene>"))`。
    uid 由 current_uid 解析（与业务参数同源），票据一次性、按 scene 绑定。
    """

    def __init__(self, scene: str) -> None:
        self.scene = scene

    def __call__(self, request: Request, uid: int = Depends(current_uid)) -> None:
        raw = request.headers.get("x-op-token")
        res = consume_op_token(uid, self.scene, raw)
        if not res.get("ok"):
            raise HTTPException(status_code=401, detail=res.get("error", "操作授权票据无效"))
