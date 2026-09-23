"""路由与共享 helper 的公共层（P2.1）。

★ 这里只放"被多个路由域共用"的最小集合；域专用 helper 留在各自域文件里。
★ 本模块禁止 import run_server（避免循环导入）。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException, Request

from mock_bank import db
import profile_db
from utils import security

# web/ 静态目录（原 run_server.WEB_DIR；本文件在 routes/ 下，故多退一级）
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# --------------------------------------------------------------------------- #
# C5. 后台管理鉴权：先认证再验 role —— 401（未认证/无效 token）与 403（非管理员）
#     语义分开；本 guard 不放行任何 user 角色。
# --------------------------------------------------------------------------- #
def _admin_guard(request: Request) -> int:
    """admin_guard：非 admin 一律 403；无效 token 401。返回 admin 的 user_id。"""
    token = security.extract_token(
        request.headers.get("authorization"), request.headers.get("x-user-token")
    )
    got = profile_db.decode_token_full(token)
    if not got:
        raise HTTPException(status_code=401, detail="登录态无效或已过期")
    uid, role = got
    # token 里的 role 只是快速通道；DB 复核为准（防旧 token 提权）
    db_role = profile_db.get_user_role(uid)
    if db_role != "admin" or role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return uid


def _resolve_uid(req: Request) -> int:
    """从 Authorization / x-user-token 解析 user_id。

    ★ 安全修复（审计 R1/R2）：库内已是多真实用户，匿名回落已关闭
      （BANK_ALLOW_ANON=0）—— 无有效 token 一律 401；管理员锁定的账号
      连旧 token 也一并 403（否则"锁定"只挡新登录、旧会话还能营业 24h）。
    """
    h = req.headers.get("authorization") or req.headers.get("x-user-token") or ""
    if h.startswith("Bearer "):
        h = h[7:]
    token = h.strip()
    try:
        uid = security.resolve_user_id(token)
    except security.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if profile_db.get_user_locked(uid):
        raise HTTPException(status_code=403, detail="账号已被锁定，请联系管理员或柜台处理")
    return uid


def _bank_headers(req: Request | None) -> dict:
    """★ 转发到 Mock 银行时必须带上身份头。

    以前代理只转发 query，token 到不了 mock_bank —— 那边解析不到 user_id，
    所有用户看到的都是默认演示账号的数据。这是"前端带了 token 却仍然串号"的根因。
    """
    if req is None:
        return {}
    out = {}
    for key, value in req.headers.items():
        if key.lower() in ("authorization", "x-user-token", "content-type", "accept",
                           "x-op-token"):
            out[key] = value
    return out


def _log_admin(admin_id: int, action: str, target_type: str = "",
               target_id: str = "", detail: dict | None = None,
               pending_sync: bool = False) -> int:
    """管理员操作留痕（admin_logs）。所有管理端写操作必经此口。"""
    db.execute(
        "INSERT INTO admin_logs (admin_id, action, target_type, target_id, detail, pending_sync) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (int(admin_id), action, target_type, str(target_id),
         json.dumps(detail or {}, ensure_ascii=False, default=str),
         1 if pending_sync else 0),
    )
    row = db.fetch_one("SELECT LAST_INSERT_ID() AS id")
    return int(row["id"])
