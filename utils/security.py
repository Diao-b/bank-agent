"""
utils/security.py —— 数据隔离的唯一收口：当前用户解析 + 资源归属校验

★ 隔离铁律（改这个包之前先读）：
  1. user_id 只能来自两处：HTTP 请求的签名 token，或进程内显式 set_current_user_id()。
     —— 任何"前端传 user_id"的通道（Query / Body）都是水平越权漏洞，已全部移除。
  2. 所有写操作在动数据之前必须过 ensure_ownership()，非本人资源一律 403。
  3. products（理财目录）是平台商品，**不参与归属校验**，见 ensure_ownership 的显式拦截。

为什么不直接 raise HTTPException：
  ensure_ownership 要能在「已开启的事务游标」上工作（写操作需要 FOR UPDATE 且与业务同事务），
  所以这里只抛业务异常 OwnershipError，由路由层翻译成 403。这样同一段校验逻辑
  既能用在只读预检，也能用在事务内。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Iterator

import pymysql

import config
from mock_bank import db  # 连接收口到单一源（组 R）

# --------------------------------------------------------------------------- #
#  进程内当前用户（Agent / 脚本路径没有 HTTP 请求，靠它显式指定操作者）
# --------------------------------------------------------------------------- #
_CTX_USER_ID: ContextVar[int | None] = ContextVar("bank_current_user_id", default=None)


def set_current_user_id(user_id: int | None) -> None:
    """设置进程内当前用户；传 None 表示交回 HTTP 请求 / 默认用户决定。"""
    _CTX_USER_ID.set(int(user_id) if user_id else None)


def get_current_user_id() -> int | None:
    return _CTX_USER_ID.get()


class current_user_ctx:
    """with current_user_ctx(uid): —— 作用域内按该用户执行，退出自动还原。"""

    def __init__(self, user_id: int | None) -> None:
        self._uid = user_id
        self._token: Any = None

    def __enter__(self) -> int | None:
        self._token = _CTX_USER_ID.set(int(self._uid) if self._uid else None)
        return _CTX_USER_ID.get()

    def __exit__(self, *exc: Any) -> None:
        _CTX_USER_ID.reset(self._token)


# --------------------------------------------------------------------------- #
#  进程内当前认证 token（Agent 层要调 mock_bank 的 HTTP 接口，
#  但那边的 user_id 只认 Authorization 头 —— 所以进程内除了 uid 上下文，
#  还要带着「原样转发给 mock_bank 的 token」，两个必须成对出现）
# --------------------------------------------------------------------------- #
_CTX_AUTH_TOKEN: ContextVar[str | None] = ContextVar("bank_current_auth_token", default=None)


def set_current_auth_token(token: str | None) -> None:
    """设置进程内当前认证 token（未登录/匿名场景传 None）。"""
    _CTX_AUTH_TOKEN.set(token or None)


def get_current_auth_token() -> str | None:
    return _CTX_AUTH_TOKEN.get()


def auth_headers() -> dict[str, str]:
    """给 mock_bank 请求附加的认证头；无 token 返回空 dict（那边回落默认用户）。"""
    token = _CTX_AUTH_TOKEN.get()
    return {"Authorization": f"Bearer {token}"} if token else {}


# --------------------------------------------------------------------------- #
#  token 解析
# --------------------------------------------------------------------------- #
def extract_token(authorization: str | None = None, x_user_token: str | None = None) -> str | None:
    """从 Authorization: Bearer <t> / x-user-token: <t> 里取出裸 token。"""
    raw = (authorization or x_user_token or "").strip()
    if raw[:7].lower() == "bearer ":
        raw = raw[7:].strip()
    return raw or None


def user_id_from_token(token: str | None) -> int | None:
    """签名校验 + 过期校验；失败返回 None。"""
    if not token:
        return None
    from profile_db import decode_token  # 延迟导入：profile_db 只在库内互相引用

    return decode_token(token)


class AuthError(Exception):
    """token 存在但非法/过期。与"没带 token"区别对待：前者 401，后者按匿名策略。"""


def resolve_user_id(token: str | None = None) -> int:
    """解析当前操作者 user_id。

    三种情况：
      - token 有效            → token 里的 uid（唯一可信来源）
      - token 存在但非法/过期 → 抛 AuthError（→ 401）。绝不静默降级，
                                否则"随便塞个 token"就能把身份糊弄过去
      - 没带 token            → BANK_ALLOW_ANON=1（默认，开发态）回落 DEFAULT_USER_ID；
                                BANK_ALLOW_ANON=0 时抛 AuthError，用于生产
    """
    uid = user_id_from_token(token)
    if uid:
        return uid
    if token:
        raise AuthError("登录态无效或已过期，请重新登录")
    if get_current_user_id():
        return get_current_user_id()  # type: ignore[return-value]
    if not config.BANK_ALLOW_ANON:
        raise AuthError("缺少登录态")
    return config.DEFAULT_USER_ID


# FastAPI 路由里这样用（头参数由 FastAPI 自动注入）：
#     from fastapi import Depends, Header
#     def current_uid(
#         authorization: str | None = Header(default=None),
#         x_user_token: str | None = Header(default=None, alias="x-user-token"),
#     ) -> int:
#         return security.current_user_id(authorization, x_user_token)
#
#     @app.get("/api/cards")
#     def list_cards(uid: int = Depends(current_uid)): ...
# 抽成函数而不是直接暴露 Header 默认值，是因为 security 层不依赖 FastAPI，
# 好让 Agent / 脚本（无 HTTP 上下文）复用同一份解析逻辑。


# --------------------------------------------------------------------------- #
#  资源归属校验
# --------------------------------------------------------------------------- #
class OwnershipError(Exception):
    """资源不属于当前用户（→ HTTP 403）。"""

    def __init__(self, table: str, row_id: Any) -> None:
        self.table = table
        self.row_id = row_id
        super().__init__(f"无权操作他人资源：{table}#{row_id}")


class NotFoundError(Exception):
    """资源不存在（→ HTTP 404）。"""

    def __init__(self, table: str, row_id: Any) -> None:
        self.table = table
        self.row_id = row_id
        super().__init__(f"{table}#{row_id} 不存在")


# 表名 → 归属判定 SQL。
# 形态约定：两个占位符，(row_id, user_id)。必须是"表自带 user_id"或"能通过 JOIN 追到 user_id"。
# ★ products 故意不在表里：它是平台商品目录，所有人都该看到同一份。
OWNERSHIP_SQL: dict[str, str] = {
    "accounts": "SELECT a.* FROM accounts a WHERE a.id=%s AND a.user_id=%s",
    "cards": "SELECT c.* FROM cards c WHERE c.id=%s AND c.user_id=%s",
    "payees": "SELECT p.* FROM payees p WHERE p.id=%s AND p.user_id=%s",
    "subscriptions": "SELECT s.* FROM subscriptions s WHERE s.id=%s AND s.user_id=%s",
    "holdings": "SELECT h.* FROM holdings h WHERE h.id=%s AND h.user_id=%s",
    "operation_logs": "SELECT o.* FROM operation_logs o WHERE o.id=%s AND o.user_id=%s",
    # transactions 自身没有 user_id，通过 account_id 追到 accounts.user_id
    "transactions": (
        "SELECT t.* FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "WHERE t.id=%s AND a.user_id=%s"
    ),
}

# 允许"存在但不属于我"时报错而不泄露存在的表（用于 403 文案统一）
GLOBAL_TABLES = ("products",)


def ensure_ownership(
    table: str,
    row_id: Any,
    user_id: int,
    cur: Any = None,
    *,
    for_update: bool = False,
) -> dict:
    """校验 table#row_id 属于 user_id，返回该行；不属于则抛 OwnershipError。

    参数：
      table    只能是 OWNERSHIP_SQL 里的白名单表名（防 SQL 注入 + 防误用 products）
      row_id   主键
      user_id  当前认证用户
      cur      已开启事务的游标。传入则复用该事务（写操作必须传，保证校验与写入原子）
      for_update  行锁，用于"先校验后扣款"这类需要防并发的写操作

    ★ 关键设计：不传 cur 时自己开一个只读连接 —— 只用于预检，
      真正的写操作必须传业务事务的 cur，否则校验与写入之间有时间窗。
    """
    if table in GLOBAL_TABLES:
        raise ValueError(
            f"'{table}' 是平台级全局数据（理财商品目录），不属于任何用户，"
            "禁止做归属校验；用户的理财资产在 holdings 表"
        )
    sql = OWNERSHIP_SQL.get(table)
    if not sql:
        raise ValueError(f"未知的资源表 '{table}'，请先在 OWNERSHIP_SQL 白名单里登记归属判定 SQL")
    if row_id is None:
        raise NotFoundError(table, row_id)

    if for_update:
        sql += " FOR UPDATE"

    if cur is not None:
        cur.execute(sql, (row_id, user_id))
        row = cur.fetchone()
    else:
        with db.get_conn() as conn:
            with conn.cursor() as c:
                c.execute(sql, (row_id, user_id))
                row = c.fetchone()

    if not row:
        # 归属校验失败时不再区分"不存在"与"别人的"：
        # 区分了就等于告诉攻击者"这个 id 存在"，属于信息泄露。
        raise OwnershipError(table, row_id)
    return row


def ensure_all_ownership(
    table: str,
    row_ids: Any,
    user_id: int,
    cur: Any = None,
) -> list[dict]:
    """批量归属校验（全部分配给同一用户才通过）。"""
    ids = list(row_ids or [])
    if not ids:
        return []
    rows = [ensure_ownership(table, rid, user_id, cur=cur) for rid in ids]
    return rows


def primary_account_id(cur: Any, user_id: int) -> int:
    """当前用户的主账户 id（id 最小的未删除账户）。转账缺省扣它。"""
    cur.execute(
        "SELECT id FROM accounts WHERE user_id=%s ORDER BY id ASC LIMIT 1", (user_id,)
    )
    row = cur.fetchone()
    if not row:
        raise NotFoundError("accounts", f"user_id={user_id}")
    return int(row["id"])


def iter_owned_account_ids(cur: Any, user_id: int) -> list[int]:
    """当前用户名下全部 account_id —— 流水 / 汇总按它做范围过滤。"""
    cur.execute("SELECT id FROM accounts WHERE user_id=%s ORDER BY id", (user_id,))
    return [int(r["id"]) for r in cur.fetchall()]
