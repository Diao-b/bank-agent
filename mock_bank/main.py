"""
mock_bank/main.py —— Mock 银行服务（FastAPI）

启动：
    uvicorn mock_bank.main:app --reload --port 8000
    （或 python -m mock_bank.main）

★ 数据隔离铁律（改动前必读）
───────────────────────────────────────────────────────────────────────
1. user_id 只有一个来源：认证依赖 current_uid（签名 token 解析）。
   ★任何端点都不得再从 Query / Body 接收 user_id —— 那是水平越权漏洞
     （用户 A 传 user_id=B 就能读 B 的资产），已全部移除。
2. 所有资产类查询一律带 user_id 过滤；transactions 自身没有 user_id，
   通过 JOIN accounts 收敛到当前用户名下的账户。
3. 所有写操作五步走：认证 → ensure_ownership → 校验 → 执行 → operation_logs 留痕。
   跨别人的资源一律 403「无权操作他人资源」，留痕与业务写在同一个事务里。
4. ★ products（理财目录）是平台级商品，全局共享，不加 user_id、不过归属校验。
   用户的理财资产在 holdings 表（买产品产生持仓记录）。

错误码约定：
    400 业务校验失败（余额不足 / 金额非法 / 状态冲突）
    401 登录态缺失或失效
    403 资源不属于当前用户（越权）
    404 资源不存在
"""
from __future__ import annotations

import hashlib
import json
import random
import threading
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field

import config
import rules
from utils import security
from utils import inbox
from utils.response_codes import ERR
from utils import settlement_engine, card_validator, card_service
from utils.deps import current_uid
from utils.op_auth import RequireOpToken
from utils.transfer import execute_transfer, resolve_payee, lookup_local_card
from utils.security import NotFoundError, OwnershipError
from . import db

# --------------------------------------------------------------------------- #
#  统一结构化响应协议（Phase 3）—— 响应包裹中间件
#  ★ 仅作用于**写操作**（POST/PUT/DELETE 的 /api/ 路由）：在序列化后的 JSON 上
#    统一补 `action`（按路由路径解析）与 `code`（成功=SUCCESS；未显式打码的
#    失败=BUSINESS_ERROR）。读操作（GET）与 SSE 不受影响。
#  ★ 不改动任何既有字段：仅做加法，前端 / Agent 解析旧字段完全兼容。
#  ★ 注册在 CORSMiddleware 之前 → 本中间件为最外层；内层 CORS 已给原始响应
#    加了跨域头，重建响应时整份 headers 拷贝，跨域不受影响。
# --------------------------------------------------------------------------- #
def _resolve_action(method: str, path: str) -> str:
    """写操作路径 → 动作名（与路由一一对应，供回执 action 字段使用）。"""
    explicit = {
        ("POST", "/api/transfer"): "transfer",
        ("POST", "/api/products/buy"): "buy_product",
        ("POST", "/api/redeem"): "redeem",
        ("POST", "/api/cards/apply"): "apply_card",
        ("PUT", "/api/cards/adjust_limit"): "adjust_card_limit",
        ("POST", "/api/cards/freeze"): "freeze_card",
        ("POST", "/api/cards/unfreeze"): "unfreeze_card",
        ("POST", "/api/subscriptions/contract"): "subscription_contract",
        ("POST", "/api/subscriptions/discover/confirm"): "subscription_confirm",
        ("POST", "/api/subscriptions/discover/ignore"): "subscription_ignore",
        ("POST", "/api/payees/create"): "create_payee",
        ("POST", "/api/accounts/transfer-internal"): "transfer_internal",
    }
    key = (method, path)
    if key in explicit:
        return explicit[key]
    # 含 id 的路由（模板形如 /api/x/{id} 或 /api/x/{id}/cancel）
    if method == "DELETE" and path.startswith("/api/cards/"):
        return "delete_card"
    if method == "DELETE" and path.startswith("/api/subscriptions/"):
        return "delete_subscription"
    if method == "PUT" and path.startswith("/api/subscriptions/"):
        return "update_subscription"
    if method == "POST" and path.endswith("/cancel"):
        return "cancel_subscription"
    if method == "POST" and path.endswith("/confirm"):
        return "confirm_subscription"
    if method == "POST" and path.endswith("/ignore"):
        return "ignore_subscription"
    # 通用兜底：取路径末段（去掉末尾数字 id）→ snake_case
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1].isdigit():
        parts = parts[:-1]
    return "_".join(parts) if parts else "unknown"


# --------------------------------------------------------------------------- #
#  幂等执行（Phase 4）—— 写操作去重
#  ★ 客户端带 `Idempotency-Key` 头即启用；不带则行为与启用前完全一致（零变化）。
#  ★ 去重锚点 = (user_id, idem_key) 唯一键：并发下由唯一索引串行化，只有一支能占位。
#    已 done → 回放原回执（加 idempotent_replay=True），**不重新执行**；
#    pending → IDEMPOTENT_IN_FLIGHT；同 key 不同请求体 → IDEMPOTENT_KEY_REUSED。
#  ★ 业务失败**不落 done**（删占位），允许用户修正后带同一 key 重试。
# --------------------------------------------------------------------------- #
_IDEM_TTL_HOURS = 24


def _as_dict(raw) -> dict:
    """MySQL JSON 列经 pymysql 可能返回 str，统一成 dict。"""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _uid_from_request(request):
    """从请求头解析当前用户（复用 utils.security —— FastAPI-free，中间件可用）。"""
    try:
        token = security.extract_token(
            request.headers.get("authorization"), request.headers.get("x-user-token"))
        return security.user_id_from_token(token)
    except Exception:  # noqa: BLE001
        return None


def _idem_lookup(uid: int, key: str):
    try:
        return db.fetch_one(
            "SELECT request_hash, response_json, status FROM idempotency_records "
            "WHERE user_id=%s AND idem_key=%s AND expires_at > NOW()", (uid, key))
    except Exception:  # noqa: BLE001 表不存在 / DB 异常 → 退化为"无幂等"，不阻断业务
        return None


def _idem_claim(uid: int, key: str, action: str, req_hash: str) -> bool:
    """占位（唯一键抢占）。返回 False = 别人已占（并发撞车）。"""
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO idempotency_records "
                    "(idem_key, user_id, action, request_hash, status, expires_at) "
                    "VALUES (%s,%s,%s,%s,'pending', DATE_ADD(NOW(), INTERVAL %s HOUR))",
                    (key, int(uid), action, req_hash, _IDEM_TTL_HOURS))
            conn.commit()
        return True
    except Exception:  # noqa: BLE001 IntegrityError（并发抢到）或表缺失
        return False


def _idem_finalize(uid: int, key: str, payload: str | None) -> None:
    """payload 非 None = 业务成功 → 落终态回执；None = 未成功 → 删占位允许重试。"""
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                if payload is None:
                    cur.execute(
                        "DELETE FROM idempotency_records "
                        "WHERE user_id=%s AND idem_key=%s AND status='pending'",
                        (int(uid), key))
                else:
                    cur.execute(
                        "UPDATE idempotency_records SET status='done', response_json=%s "
                        "WHERE user_id=%s AND idem_key=%s AND status='pending'",
                        (payload, int(uid), key))
            conn.commit()
    except Exception:  # noqa: BLE001
        pass


class ResponseEnvelopeMiddleware(BaseHTTPMiddleware):
    """写操作 JSON 回执包裹层：① 统一补 action/code/message；② 幂等去重。

    ★ 注册顺序：本中间件**先** add、CORS **后** add → CORS 在最外层，
      因此本层的**所有**返回（含不调用 call_next 的幂等回放）都会拿到跨域头。
    """

    async def dispatch(self, request, call_next):
        from starlette.responses import Response as _StarletteResponse  # noqa: PLC0415

        method, path = request.method, request.url.path
        is_write = method in ("POST", "PUT", "DELETE") and path.startswith("/api/")
        action = _resolve_action(method, path) if is_write else None

        # ================= 幂等前置（仅在带 Idempotency-Key 时启用）=================
        idem_key = (request.headers.get("idempotency-key") or "").strip() if is_write else ""
        idem_uid = None
        req_hash = ""
        claimed = False
        if idem_key:
            body_bytes = await request.body()   # BaseHTTPMiddleware 已缓存，下游仍可读
            req_hash = hashlib.sha256(
                (method + "|" + path + "|" + body_bytes.decode("utf-8", "ignore")).encode("utf-8")
            ).hexdigest()
            idem_uid = _uid_from_request(request)
            if idem_uid:
                hit = _idem_lookup(idem_uid, idem_key)
                if hit:
                    if hit.get("request_hash") != req_hash:
                        return _idem_json(409, action, ERR.IDEMPOTENT_KEY_REUSED,
                                          f"幂等键「{idem_key}」已被用于另一个请求，请更换幂等键")
                    if hit.get("status") == "done":
                        replay = _as_dict(hit.get("response_json"))
                        if replay:
                            replay["idempotent_replay"] = True
                            return _idem_json(200, action, replay.get("code", "SUCCESS"),
                                              replay.get("message") or "重复请求已被幂等拦截，返回原回执",
                                              extra=replay)
                    return _idem_json(409, action, ERR.IDEMPOTENT_IN_FLIGHT,
                                      "相同幂等键的请求正在处理中，请勿重复提交")
                claimed = _idem_claim(idem_uid, idem_key, action or "unknown", req_hash)
                if not claimed:
                    # 并发撞车：另一支刚占位 → 按上面的规则再判一次
                    hit = _idem_lookup(idem_uid, idem_key)
                    if hit:
                        if hit.get("request_hash") != req_hash:
                            return _idem_json(409, action, ERR.IDEMPOTENT_KEY_REUSED,
                                              f"幂等键「{idem_key}」已被用于另一个请求，请更换幂等键")
                        if hit.get("status") == "done":
                            replay = _as_dict(hit.get("response_json"))
                            if replay:
                                replay["idempotent_replay"] = True
                                return _idem_json(200, action, replay.get("code", "SUCCESS"),
                                                  replay.get("message") or "重复请求已被幂等拦截，返回原回执",
                                                  extra=replay)
                        return _idem_json(409, action, ERR.IDEMPOTENT_IN_FLIGHT,
                                          "相同幂等键的请求正在处理中，请勿重复提交")

        # ================= 执行原请求 =================
        response = await call_next(request)

        # 过滤器（★ 在消费 response body 之前完成：body_iterator 只能读一次，
        #   一旦读过就必须重建响应，否则原响应会变成空 body —— 这是踩过的坑）
        if not is_write:
            return response
        ctype = response.headers.get("content-type", "") or ""
        if not ctype.startswith("application/json"):
            if claimed and idem_uid:
                _idem_finalize(idem_uid, idem_key, None)
            return response

        try:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
        except Exception:  # noqa: BLE001
            if claimed and idem_uid:
                _idem_finalize(idem_uid, idem_key, None)
            return response

        def rebuild(payload: bytes):
            """重建响应（整份 headers 拷贝；跨域头由外层 CORS 统一添加）。"""
            headers = dict(response.headers)
            headers["content-length"] = str(len(payload))
            return _StarletteResponse(
                content=payload,
                status_code=response.status_code,
                headers=headers,
                media_type="application/json",
            )

        # ★ 走到这里 body 已被消费 —— 之后所有 return 都必须 rebuild，不能返回原 response
        try:
            data = json.loads(body)
        except Exception:  # noqa: BLE001
            if claimed and idem_uid:
                _idem_finalize(idem_uid, idem_key, None)
            return rebuild(body)
        if not isinstance(data, dict) or "ok" not in data:
            if claimed and idem_uid:
                _idem_finalize(idem_uid, idem_key, None)
            return rebuild(body)   # 非业务回执（如 404/405 的 {"detail":...}）原样返回

        if action and "action" not in data:
            data["action"] = action
        if data.get("ok") is True:
            if "code" not in data:
                data["code"] = "SUCCESS"
        else:
            if "code" not in data:
                data["code"] = "BUSINESS_ERROR"
            # ★ 统一 human-readable 字段：成功带 message、失败带 error 是历史不一致，
            #   这里给失败补 message（= error），保留 error 以兼容既有前端/Agent 解析。
            if "message" not in data and "error" in data:
                data["message"] = data["error"]

        payload = json.dumps(data, ensure_ascii=False)
        # ================= 幂等后置：成功落终态 / 未成功删占位 =================
        if claimed and idem_uid:
            _idem_finalize(idem_uid, idem_key, payload if data.get("ok") is True else None)
        return rebuild(payload.encode("utf-8"))


def _idem_json(status: int, action, code: str, message: str, extra: dict | None = None):
    """构造幂等短路响应（回放 / in-flight / key 复用冲突）。

    ★ 跨域头由外层 CORSMiddleware 统一添加，这里无需手工处理。
    """
    from starlette.responses import JSONResponse  # noqa: PLC0415

    body: dict = {"ok": status == 200, "code": code, "message": message}
    if action:
        body["action"] = action
    if status != 200:
        body["error"] = message
    if extra:                       # 回放：以原回执为准（覆盖上面的占位字段）
        body.update(extra)
    return JSONResponse(status_code=status, content=body)


app = FastAPI(title="Mock Bank API", version="0.4.0 (多用户隔离)")
# ★ 注册顺序有讲究：Starlette 的 add_middleware 是 insert(0)，最终**后 add 的在最外层**。
#   这里 CORS 后 add → CORS 在最外层，因此 ResponseEnvelopeMiddleware 的**所有**返回
#  （包括幂等回放这类**不经过 call_next** 的短路响应）都会被加上跨域头。
app.add_middleware(ResponseEnvelopeMiddleware)  # 写操作回执：补 action/code + 幂等去重
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 业务常量单源见 rules.py（组 W）：信用卡调额范围由 rules 提供；
# VALID_CARD_TYPES / CARD_DEFAULT_CREDIT_LIMIT / VALID_CYCLES 已收口到 rules
# （main 不再自持，subscription 等改为直接从 rules 引用）。


# --------------------------------------------------------------------------- #
#  工具函数
# --------------------------------------------------------------------------- #
def json_safe(obj: Any) -> Any:
    """Decimal / date / datetime -> JSON 原生类型。"""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def fail(message: str, *, code=None, action=None, **extra) -> dict:
    """统一失败回执：{ok:False, error, [code], [action], **extra}。

    ★ 统一结构化响应协议（Phase 3）：写操作失败统一携带机器可读 `code`
      （见 utils.response_codes.ERR），便于前端 / Agent 直接比对原因，
      无需解析中文文案。未传 code 时由响应包裹中间件兜底为 BUSINESS_ERROR。
      action 同理（未传则由包裹层按路由路径注入）。
    """
    out: dict = {"ok": False, "error": message}
    if code is not None:
        out["code"] = code
    if action is not None:
        out["action"] = action
    out.update(extra)
    return out


def ok_response(action: str, *, code: str = "SUCCESS", message=None, **extra) -> dict:
    """统一成功回执：{ok:True, action, code:'SUCCESS', [message], **extra}。

    仅在**显式**构造回执时使用；路由直接返回字面量 dict 的成功分支由
    响应包裹中间件统一补 action/code，不需逐处调用本函数。
    """
    out: dict = {"ok": True, "action": action, "code": code}
    if message is not None:
        out["message"] = message
    out.update(extra)
    return out


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    """日期加减 n 个月（n 可为负），日超出时取当月最后一天。"""
    y, m = divmod((d.year * 12 + d.month - 1) + n, 12)
    m += 1
    last_day = (date(y + (m // 12), (m % 12) + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last_day))


def to_decimal(value: Any) -> Decimal:
    """入参金额 → Decimal；非法值抛 ValueError（路由层转 400）。"""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"金额非法：{value!r}")
    if d.is_nan() or d.is_infinite():
        raise ValueError(f"金额非法：{value!r}")
    return d


def owned(cur, table: str, row_id: Any, uid: int, **kw) -> dict:
    """在事务游标上做归属校验：不属于当前用户 → 403。"""
    try:
        return security.ensure_ownership(table, row_id, uid, cur=cur, **kw)
    except OwnershipError:
        raise HTTPException(status_code=403, detail="无权操作他人资源")
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def user_account_ids(cur, uid: int) -> list[int]:
    return security.iter_owned_account_ids(cur, uid)


# =========================================================================== #
#  健康检查
# =========================================================================== #
@app.get("/")
def root():
    return {
        "service": "mock-bank",
        "version": "0.4.0 (多用户隔离)",
        "note": "所有资产类接口按 Authorization 里的 token 隔离；products 为全局商品目录",
        "endpoints": [
            "GET  /api/balance",
            "GET  /api/transactions",
            "GET  /api/transactions/summary",
            "GET  /api/payees",
            "POST /api/payees/create",
            "GET  /api/products",
            "GET  /api/cards",
            "GET  /api/subscriptions",
            "GET  /api/holdings",
            "GET  /api/operation_logs",
            "POST /api/verify/send",
            "POST /api/verify/check",
            "POST /api/transfer",
            "POST /api/products/buy",
            "POST /api/redeem",
            "POST /api/cards/apply",
            "PUT  /api/cards/adjust_limit",
            "POST /api/cards/freeze",
            "POST /api/cards/unfreeze",
            "DELETE /api/cards/{card_id}",
            "POST /api/subscriptions/contract",
            "GET  /api/subscriptions/discover",
            "POST /api/subscriptions/discover/confirm",
            "POST /api/subscriptions/discover/ignore",
            "PUT  /api/subscriptions/{sub_id}",
            "DELETE /api/subscriptions/{sub_id}",
            "POST /api/subscriptions/{sub_id}/cancel",
            "POST /api/subscriptions/{sub_id}/confirm",
            "POST /api/subscriptions/{sub_id}/ignore",
        ],
    }


@app.get("/api/health")
def health():
    """连通性探针。

    ★ 以前这里返回全库 accounts 总数，等于把"平台有多少账户"泄露给任何匿名请求者。
      现在只回答"数据库活着吗"。
    """
    try:
        db.fetch_one("SELECT 1 AS ok")
        return {"ok": True, "db": "up"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "db": "down", "error": str(exc)}


# =========================================================================== #
#  读端点（全部按当前认证用户过滤）
# =========================================================================== #
@app.get("/api/balance")
def get_balance(
    uid: int = Depends(current_uid),
    account_id: int | None = Query(None, description="不传则返回该用户全部账户"),
):
    """账户余额。"""
    try:
        # ★ 惰性 housekeeping：余额是"在途扣款/退票后恢复"的直接体现，
        #   进本接口先跑一次到期清算 + 订阅补扣（读接口顺带触发，无常驻定时器）。
        try:
            settlement_engine.process_housekeeping(uid)
        except Exception:  # noqa: BLE001
            pass
        if account_id is not None:
            # 指定账户也必须先过归属：传别人的 account_id 一样是越权
            row = db.fetch_one(
                "SELECT id, user_id, name, balance FROM accounts WHERE id=%s AND user_id=%s",
                (account_id, uid),
            )
            if not row:
                return fail(f"账户 {account_id} 不存在或不属于当前用户")
            return {"ok": True, "accounts": [json_safe(row)], "total_balance": json_safe(row["balance"])}

        rows = db.fetch_all(
            "SELECT id, user_id, name, balance, account_type, is_primary, "
            "RIGHT(COALESCE(card_no,''), 4) AS card_tail "
            "FROM accounts WHERE user_id=%s AND COALESCE(account_type,'debit') <> 'credit' "
            "ORDER BY id",
            (uid,),
        )
        total = sum((r["balance"] for r in rows), Decimal("0"))
        return {"ok": True, "accounts": json_safe(rows), "total_balance": json_safe(total)}
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询余额失败：{exc}")


@app.get("/api/transactions")
def get_transactions(
    uid: int = Depends(current_uid),
    start: str | None = Query(None, description="起始日期 YYYY-MM-DD，默认 3 个月前"),
    end: str | None = Query(None, description="结束日期 YYYY-MM-DD，默认今天"),
    category: str | None = Query(None, description="分类过滤，如 餐饮/购物/娱乐"),
    account_id: int | None = Query(None),
    type: str | None = Query(None, description="expense / income，默认全部"),
):
    """流水查询：返回明细 + 汇总（总支出 / 总收入 / 分类占比）。

    ★ 隔离：transactions 自身没有 user_id，靠 JOIN accounts 把范围收敛到
      当前用户名下的账户。account_id 过滤器只是二次收窄，不能越权。
    ★ 惰性 housekeeping：流水是订阅扣款与跨行清算的主战场，进本接口先跑一次
      process_housekeeping（订阅补扣 + 跨行到期清算），用户看到的流水永远是
      完整账（含在途"清算中"与已退票"退票退回"）。
    """
    try:
        try:
            # ★ 统一惰性入口：订阅到期补扣 + 跨行到期清算。
            #   两个引擎各自 try/except，任一失败都不阻断流水查询。
            settlement_engine.process_housekeeping(uid)
        except Exception:  # noqa: BLE001 - 后台任务失败不阻断流水查询
            pass

        today = date.today()
        start_d = date.fromisoformat(start) if start else today - timedelta(days=90)
        end_d = date.fromisoformat(end) if end else today
        if start_d > end_d:
            return fail(f"start({start_d}) 不能晚于 end({end_d})")

        where = ["a.user_id = %s", "t.`date` BETWEEN %s AND %s"]
        args: list[Any] = [uid, start_d, end_d]
        if category:
            where.append("t.category = %s")
            args.append(category)
        if account_id is not None:
            where.append("t.account_id = %s")
            args.append(account_id)
        if type:
            where.append("t.type = %s")
            args.append(type)
        where_sql = " AND ".join(where)

        rows = db.fetch_all(
            f"""
            SELECT t.id, t.account_id, a.name AS account_name, t.`date`,
                   t.amount, t.category, t.merchant, t.type, t.remark, t.settle_status
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            WHERE {where_sql}
            ORDER BY t.`date` DESC, t.id DESC
            """,
            tuple(args),
        )

        by_category: dict[str, dict] = {}
        total_expense = Decimal("0")
        total_income = Decimal("0")
        for r in rows:
            amt = Decimal(str(r["amount"]))
            if r["type"] == "income":
                total_income += amt
                continue
            total_expense += amt
            c = by_category.setdefault(r["category"], {"category": r["category"], "amount": Decimal("0"), "count": 0})
            c["amount"] += amt
            c["count"] += 1

        for c in by_category.values():
            c["amount"] = float(c["amount"])
            c["ratio"] = round(c["amount"] / float(total_expense), 4) if total_expense else 0.0
        by_category_sorted = sorted(by_category.values(), key=lambda x: x["amount"], reverse=True)

        return {
            "ok": True,
            "range": {"start": start_d.isoformat(), "end": end_d.isoformat()},
            "summary": {
                "count": len(rows),
                "total_expense": float(total_expense),
                "total_income": float(total_income),
                "by_category": by_category_sorted,
            },
            "transactions": json_safe(rows),
        }
    except ValueError:
        return fail("日期格式应为 YYYY-MM-DD")
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询流水失败：{exc}")


@app.get("/api/transactions/summary")
def transactions_summary(
    uid: int = Depends(current_uid),
    start: str | None = Query(None, description="起始日期 YYYY-MM-DD，默认本月 1 号"),
    end: str | None = Query(None, description="结束日期 YYYY-MM-DD，默认今天"),
):
    """账单汇总：按分类聚合，并给出"上一个等长窗口"的环比。

    ★ 只统计当前用户名下的账户流水 —— A 和 B 各算各的（验收 #9）。
    ★ 惰性 housekeeping：在途金额计入支出（已扣款），退票后体现为收入，
      本月支出自动修正，数据自洽。
    """
    try:
        try:
            settlement_engine.process_housekeeping(uid)
        except Exception:  # noqa: BLE001
            pass
        today = date.today()
        this_start = date.fromisoformat(start) if start else month_start(today)
        this_end = date.fromisoformat(end) if end else today
        if this_start > this_end:
            return fail(f"start({this_start}) 不能晚于 end({this_end})")

        span = (this_end - this_start).days + 1
        last_end = this_start - timedelta(days=1)
        last_start = last_end - timedelta(days=span - 1)

        with db.get_conn() as conn:
            with conn.cursor() as cur:
                ids = user_account_ids(cur, uid)
        if not ids:
            return {
                "ok": True, "count": 0, "list": [],
                "period": {"start": this_start.isoformat(), "end": this_end.isoformat()},
                "last_period": {"start": last_start.isoformat(), "end": last_end.isoformat()},
                "total_expense": 0.0, "last_total_expense": 0.0,
            }

        placeholders = ",".join(["%s"] * len(ids))

        def _agg(s: date, e: date) -> dict[str, dict]:
            rows = db.fetch_all(
                f"""
                SELECT t.category, SUM(t.amount) AS amount, COUNT(*) AS cnt
                FROM transactions t
                WHERE t.account_id IN ({placeholders}) AND t.`date` BETWEEN %s AND %s
                  AND t.type = 'expense'
                GROUP BY t.category
                """,
                tuple(ids) + (s, e),
            )
            return {r["category"]: {"amount": Decimal(str(r["amount"])), "count": int(r["cnt"])}
                    for r in rows}

        this = _agg(this_start, this_end)
        last = _agg(last_start, last_end)

        this_total = sum((v["amount"] for v in this.values()), Decimal("0"))
        last_total = sum((v["amount"] for v in last.values()), Decimal("0"))

        out = []
        for cat in sorted(set(this) | set(last)):
            ta = this.get(cat, {}).get("amount", Decimal("0"))
            la = last.get(cat, {}).get("amount", Decimal("0"))
            cnt = int(this.get(cat, {}).get("count", 0))
            share = (ta / this_total) if this_total else Decimal("0")
            if la == 0:
                change_pct = None if ta == 0 else 100.0
            else:
                change_pct = float((ta - la) / la * 100)
            out.append({
                "category": cat,
                "amount": float(ta),
                "count": cnt,
                "share": float(share),
                "last_amount": float(la),
                "change_pct": None if change_pct is None else round(change_pct, 2),
            })

        return {
            "ok": True,
            "period": {"start": this_start.isoformat(), "end": this_end.isoformat()},
            "last_period": {"start": last_start.isoformat(), "end": last_end.isoformat()},
            "total_expense": float(this_total),
            "last_total_expense": float(last_total),
            "count": len(out),
            "list": out,
        }
    except ValueError:
        return fail("日期格式应为 YYYY-MM-DD")
    except Exception as exc:  # noqa: BLE001
        return fail(f"汇总流水失败：{exc}")


@app.get("/api/payees")
def list_payees(
    uid: int = Depends(current_uid),
    keyword: str | None = Query(None, description="昵称模糊查询，如 妈妈"),
):
    """收款人列表，支持昵称模糊匹配（注意：可能命中多条同名记录）。"""
    try:
        sql = """
            SELECT id, user_id, nickname, real_name, bank, bank_id, card_no,
                   account_tail, is_frequent, linked_user_id, account_id
            FROM payees WHERE user_id = %s
        """
        args: list[Any] = [uid]
        if keyword:
            sql += " AND nickname LIKE %s"
            args.append(f"%{keyword}%")
        sql += " ORDER BY is_frequent DESC, id ASC"
        rows = db.fetch_all(sql, tuple(args))
        return {
            "ok": True,
            "keyword": keyword,
            "count": len(rows),
            "ambiguous": len(rows) > 1,
            "payees": json_safe(rows),
        }
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询收款人失败：{exc}")


class PayeeCreateReq(BaseModel):
    payee_name: str = Field(min_length=1, max_length=32, description="收款人显示昵称")
    bank_id: int = Field(description="bank_directory 外键（前端银行下拉选择）")
    card_no: str = Field(min_length=12, max_length=19, description="完整卡号（Luhn 校验）")
    real_name: str | None = Field(default=None, max_length=64,
                                  description="跨行收款人必填真实姓名；站内由系统实名覆盖")


@app.post("/api/payees/create")
def create_payee(req: PayeeCreateReq, uid: int = Depends(current_uid)):
    """新增收款人（★唯一合法的建收款人途径）。

    账户校验模型（转账标的物是「账户」不是「姓名」）：
    1. bank_id 必须来自 bank_directory（前端下拉选择，不接受自由填银行名）；
    2. card_no Luhn 校验 → 不合法直接「卡号有误」；
    3. BIN 识别银行，必须与该 bank_id 对应银行一致 → 否则「卡号与所选银行不符」；
    4. 选微众银行（本行）→ 卡号须在 accounts 反查存在，否则「该微众卡号不存在」
       （★不能创建指向虚空账户的收款人）；存在则自动 linked_user_id 绑定 +
       real_name 以系统实名覆盖（防伪冒名）→ 站内收款人；
    5. 他行 → 跨行收款人（本行无法验证，如实标注），real_name 必填；
    6. 同 card_no 已存在该用户收款人簿 → 提示已存在（可改昵称）。
    """
    nickname = (req.payee_name or "").strip()
    card_no = card_validator.normalize_card_input(req.card_no)
    real_name_in = (req.real_name or "").strip() if req.real_name else ""
    if not nickname:
        return fail("收款人昵称不能为空")
    if not card_no:
        return fail("卡号不能为空")

    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                # 1) 银行必须来自目录
                cur.execute(
                    "SELECT id, bank_name, bank_code, is_local FROM bank_directory WHERE id=%s",
                    (int(req.bank_id),),
                )
                bank = cur.fetchone()
                if not bank:
                    return fail("所选银行不在银行目录中，请通过银行列表选择")

                # 2) Luhn 校验
                if not card_validator.luhn_check(card_no):
                    return fail("卡号有误，请核对后重试")

                # 3) BIN 与所选银行一致
                bin_code = card_validator.bank_from_card(card_no)
                if bin_code != bank["bank_code"]:
                    # ★ 坑（第二次踩）：BIN_TO_BANK 的键是「BIN 前缀」（'6225'），
                    #   而这里拿到的是 bank_code（'CMBC'）—— 直接 .get() 永远查不到，
                    #   会一律退化成「其他银行」，让提示失去意义（用户看不出自己填的是哪家卡）。
                    #   正确做法：拿 bank_code 去 bank_directory 查银行名。
                    cur.execute("SELECT bank_name FROM bank_directory WHERE bank_code=%s",
                                (bin_code,))
                    other_row = cur.fetchone() if bin_code else None
                    other = (other_row or {}).get("bank_name") or "无法识别的银行"
                    return fail(f"卡号与所选银行不符：该卡号归属「{other}」，而非「{bank['bank_name']}」")

                is_local = bool(bank["is_local"])
                linked_user_id = None
                system_real_name = None
                target_account_id = None
                track = "cross_bank"

                if is_local:
                    # 4) 微众卡号反查账户（★核心拦截：随便填的卡号直接被拒）
                    #    ★ 卡片-账户一体化：走 lookup_local_card，
                    #      同时命中 accounts.card_no（账户本号）与 cards.card_no_full
                    #      （账户下挂靠的卡，例如新开的储蓄卡、同一账户的第 2 张卡），
                    #      并把「目标账户」记进 payees.account_id —— 转账才能入账到
                    #      这张卡对应的账户，而不是对方 id 最小的账户。
                    info = lookup_local_card(card_no, cur=cur)
                    if info and info.get("credit_card"):
                        db.log_operation(uid, "add_payee",
                                         {"payee_name": nickname, "card_no": card_no,
                                          "reason": "credit_card_not_supported"}, "failed", cur=cur)
                        conn.commit()
                        return fail("信用卡不支持收款（余额为可用额度，非存款），请使用储蓄卡号")
                    if not info or not info.get("account_id"):
                        db.log_operation(uid, "add_payee",
                                         {"payee_name": nickname, "card_no": card_no,
                                          "reason": "webbank_card_not_found"}, "failed", cur=cur)
                        conn.commit()
                        return fail("该微众卡号不存在，请核对卡号")
                    linked_user_id = int(info["user_id"])
                    target_account_id = int(info["account_id"])
                    system_real_name = info["real_name"]   # ★系统实名覆盖（防伪冒名）
                    track = "insite"
                else:
                    # 5) 跨行：真实姓名必填
                    if not real_name_in:
                        return fail("跨行收款人需填写对方真实姓名")
                    system_real_name = real_name_in
                    track = "cross_bank"

                # 6) 重复检测（同卡号同用户）
                cur.execute("SELECT id FROM payees WHERE user_id=%s AND card_no=%s LIMIT 1",
                            (uid, card_no))
                if cur.fetchone():
                    db.log_operation(uid, "add_payee",
                                     {"payee_name": nickname, "card_no": card_no,
                                      "reason": "duplicated"}, "failed", cur=cur)
                    conn.commit()
                    return fail(f"收款人卡号尾号 {card_no[-4:]} 已存在于您的收款人簿，可改昵称后重试")

                cur.execute(
                    "INSERT INTO payees (user_id, nickname, real_name, bank, bank_id, "
                    "card_no, account_tail, linked_user_id, account_id, is_frequent) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0)",
                    (uid, nickname, system_real_name, bank["bank_name"], int(bank["id"]),
                     card_no, card_no[-4:], linked_user_id, target_account_id),
                )
                payee_id = cur.lastrowid
                # ★ 跨行收款人 → 同时登记「他行可清算账户库」(external_accounts)。
                #   语义：用户提供的账户经基础校验（Luhn + BIN 与所选银行一致）后
                #   视为可清算（演示口径；生产由收款行核验账户真实存在）。
                #   ★ 登记时机只在新增收款人 —— 绝不在转账成功时登记（否则幽灵卡号
                #     第一次转账就会"成功"，退票闭环失效）。
                #   这样：收款人簿内的跨行账户转账 = 能到账；
                #         临时编造的卡号（未走本流程）= 清算退票。
                if track == "cross_bank":
                    cur.execute(
                        "INSERT INTO external_accounts (card_no, real_name, bank_id) "
                        "SELECT %s, %s, %s FROM DUAL "
                        "WHERE NOT EXISTS (SELECT 1 FROM external_accounts WHERE card_no=%s)",
                        (card_no, system_real_name, int(bank["id"]), card_no),
                    )
                db.log_operation(uid, "add_payee",
                                 {"payee_id": payee_id, "payee_name": nickname,
                                  "bank": bank["bank_name"], "track": track,
                                  "linked_user_id": linked_user_id,
                                  "account_id": target_account_id},
                                 "success", cur=cur)
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            return fail(f"新增收款人失败：{exc}")

    masked = card_validator.mask_real_name(system_real_name)
    return {
        "ok": True,
        "track": track,
        "status": "insite" if track == "insite" else "cross_bank",
        "payee": {
            "id": payee_id, "user_id": uid, "payee_name": nickname,
            "nickname": nickname, "real_name": masked, "bank": bank["bank_name"],
            "bank_id": int(bank["id"]), "card_no": card_no,
            "account_tail": card_no[-4:], "is_frequent": 0,
            "linked_user_id": linked_user_id, "account_id": target_account_id,
        },
        "message": (
            "已新增站内收款人（小微用户·实时到账）" if track == "insite"
            else "已新增跨行收款人（2 小时内到账）"
        ),
    }


@app.get("/api/bank-directory")
def list_bank_directory(uid: int = Depends(current_uid)):
    """全量银行目录（前端新增收款人下拉选择器数据源）。

    ★ 不接受自由填银行名：新增收款人时 bank_id 必须来自本列表。
    返回按本行优先、其余按银行名排序，便于前端「常用/搜索」体验。
    """
    try:
        rows = db.fetch_all(
            "SELECT id, bank_name, bank_code, is_local "
            "FROM bank_directory ORDER BY is_local DESC, bank_name ASC"
        )
        return {"ok": True, "count": len(rows), "banks": json_safe(rows)}
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询银行目录失败：{exc}")


class PayeeResolveReq(BaseModel):
    payee_id: int | None = None
    nickname: str | None = None
    card_tail: str | None = None
    card_no: str | None = None
    real_name: str | None = None


@app.post("/api/payees/resolve")
def resolve_payee_endpoint(req: PayeeResolveReq, uid: int = Depends(current_uid)):
    """★ 转账收款人解析唯一入口（对话流 / 表单流 / 联动任务链全走这里）。

    把「昵称 / 卡号 / 尾号 / 姓名」统一解析为校验结果：
      - insite / cross_bank：校验通过，附带 track / 实名(脱敏) / 尾号 / 到账时效；
      - not_found：收款人簿无此人（断掉"随便填名字就能转"的漏洞），引导新增；
      - rejected：卡号非法 / 虚拟卡号不存在，如实拒绝；
      - ambiguous：多候选（双妈妈），交前端 / clarify 选择。
    详见 utils/resolve_payee 注释。
    """
    try:
        result = resolve_payee(
            {
                "payee_id": req.payee_id,
                "nickname": req.nickname,
                "card_tail": req.card_tail,
                "card_no": req.card_no,
                "real_name": req.real_name,
            },
            uid,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        return fail(f"收款人解析失败：{exc}")


@app.get("/api/user")
def get_user(uid: int = Depends(current_uid)):
    """当前登录用户信息（顶栏问候与侧边栏展示）。

    ★ 不再接受 user_id 参数：以前传 ?user_id=2 就能扒到别人的姓名和注册时间。
    """
    try:
        row = db.fetch_one(
            "SELECT id, name, created_at FROM users WHERE id=%s", (uid,)
        )
        if not row:
            return fail(f"用户 {uid} 不存在")
        return {"ok": True, "user": json_safe(row)}
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询用户失败：{exc}")


@app.get("/api/operation_logs")
def list_operation_logs(uid: int = Depends(current_uid), limit: int = Query(20)):
    """当前用户的操作留痕（调试 / 安全中心页）。"""
    try:
        rows = db.fetch_all(
            "SELECT id, user_id, action, detail, result, created_at "
            "FROM operation_logs WHERE user_id=%s ORDER BY id DESC LIMIT %s",
            (uid, limit),
        )
        return {"ok": True, "logs": json_safe(rows)}
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询操作日志失败：{exc}")


# =========================================================================== #
#  写端点：转账
# =========================================================================== #
class TransferReq(BaseModel):
    from_account_id: int | None = Field(default=None, description="省略则扣当前用户主账户")
    payee_id: int
    amount: float = Field(gt=0)
    code: str | None = Field(default=None, description="验证码，可选（M2 工作流会强制要求）")


@app.post("/api/transfer")
def transfer(req: TransferReq, request: Request, uid: int = Depends(current_uid),
             _: None = Depends(RequireOpToken("transfer"))):
    """转账执行（薄壳）—— 业务逻辑见 utils/transfer.execute_transfer。

    站内轨实时到账（扣款+入账+双流水+双回执+双方留痕，同事务）；
    跨行轨扣款即进在途（settle_status='pending' + pending_settlements 台账），
    清算后到账或全额退回；详见该函数 docstring 的设计原则与事务边界。
    """
    return execute_transfer(
        uid=uid,
        from_account_id=req.from_account_id,
        payee_id=req.payee_id,
        amount_raw=req.amount,
        payload=req.model_dump(),
        inject=request.headers.get("X-Test-Inject"),
    )


@app.get("/api/transfer/limit")
def transfer_limit(uid: int = Depends(current_uid)):
    """转账限额查询（只读）：单笔/单日累计限额 + 当日已用/剩余/重置时间。

    ★ 供对话流 fill_params 前置提示（省一次验证码与两次中断）；
      权威校验仍在 POST /api/transfer 的事务内（对账 + 行锁 + 计数器）。
    """
    try:
        limit = Decimal(str(rules.get_rule("transfer_daily_limit")))
        if limit <= 0:
            return {"ok": True, "limit": 0.0, "used": 0.0, "remaining": 0.0,
                    "reset_at": _limit_reset_at(), "applied": False}
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT used_amount FROM transfer_daily_quota "
                    "WHERE user_id=%s AND quota_date=CURDATE()", (uid,))
                row = cur.fetchone()
                used = Decimal(str(row["used_amount"])) if row else Decimal("0")
        remaining = limit - used
        reset_at = (datetime.now() + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return {"ok": True, "limit": float(limit), "used": float(used),
                "remaining": float(remaining), "reset_at": reset_at.isoformat(),
                "applied": True}
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询转账限额失败：{exc}")


# =========================================================================== #
#  读端点：收款人尾号校验（防转错人，仅提醒不阻断）
# =========================================================================== #
@app.get("/api/payees/check")
def check_payee(
    nickname: str = Query(..., description="收款人昵称（模糊匹配当前用户名下收款人）"),
    card_tail: str | None = Query(None, description="输入的卡号后四位；留空只做存在性校验"),
    uid: int = Depends(current_uid),
):
    """转账时输入收款人 → 按"昵称 + 银行卡后四位"定位并提醒（不改变安全流程）。

    返回：
      - 无匹配收款人        → {found:false, message}
      - 多个同名（两个妈妈）→ {found:"ambiguous", candidates:[...]}，提示选择
      - 唯一 + 尾号不符      → {found:true, match:false, message, account_tail, is_insite}
                               ★ 不阻止提交，只提醒（可能用户记错尾号，也可能换卡了；
                                 最终以选中 payee 的账户为准）
      - 唯一 + 尾号一致      → {found:true, match:true, is_insite}

    ★ 后四位校验的是"站内用户名下/收款人记录里的卡"——用于判断该收款人是否本站用户
      及其卡信息是否匹配。无论结果如何，风控分级/验证/确认照常执行。
    """
    try:
        sql = (
            "SELECT id, user_id, nickname, bank, account_tail, is_frequent, linked_user_id "
            "FROM payees WHERE user_id=%s AND nickname LIKE %s ORDER BY is_frequent DESC, id ASC"
        )
        matches = db.fetch_all(sql, (uid, f"%{nickname}%"))
    except Exception as exc:  # noqa: BLE001
        return fail(f"收款人校验失败：{exc}")

    if not matches:
        return {
            "found": False,
            "message": "未找到该收款人，可在常用收款人列表中选择或新增",
        }

    if len(matches) > 1:
        return {
            "found": "ambiguous",
            "message": f"找到 {len(matches)} 个叫「{nickname}」的收款人，请选择其中一个",
            "candidates": [
                {
                    "payee_id": int(m["id"]),
                    "nickname": m["nickname"],
                    "bank": m["bank"],
                    "account_tail": m["account_tail"],
                    "is_frequent": bool(m["is_frequent"]),
                    "is_insite": m.get("linked_user_id") is not None,
                }
                for m in matches
            ],
        }

    p = matches[0]
    real_tail = p["account_tail"]
    is_insite = p.get("linked_user_id") is not None
    tail_match = (card_tail is None) or (str(card_tail).strip() == str(real_tail).strip())
    if not tail_match:
        return {
            "found": True,
            "match": False,
            "message": f"尾号不符，该收款人账户尾号为 {real_tail}，请核对",
            "payee_id": int(p["id"]),
            "nickname": p["nickname"],
            "bank": p["bank"],
            "account_tail": real_tail,
            "is_insite": is_insite,
        }
    return {
        "found": True,
        "match": True,
        "payee_id": int(p["id"]),
        "nickname": p["nickname"],
        "bank": p["bank"],
        "account_tail": real_tail,
        "is_insite": is_insite,
    }


# =========================================================================== #
#  读端点：跨行退票记录查询（转账前"该账户近期有退票记录"预警）
# =========================================================================== #
@app.get("/api/settlements/return-history")
def settlement_return_history(
    payee_id: int | None = Query(None, description="收款人 ID（取其卡号查退票记录）"),
    card_no: str | None = Query(None, description="或直接给卡号"),
    uid: int = Depends(current_uid),
):
    """查询某收款账户当日的跨行退票次数（防重复转错）。

    ★ 用途：向"近期多次被退票"的卡号再次转账时，fill_params 阶段主动提醒用户核对。
    ★ 只提醒不阻断 —— 风控分级 / 验证 / 确认照常执行。
    """
    try:
        target_card = card_no
        if not target_card and payee_id is not None:
            row = db.fetch_one(
                "SELECT card_no FROM payees WHERE id=%s AND user_id=%s",
                (int(payee_id), uid),
            )
            target_card = (row or {}).get("card_no")
        if not target_card:
            return {"ok": True, "card_no": None, "return_count": 0, "warn": False}
        n = settlement_engine.count_recent_returns(target_card, user_id=uid)
        return {
            "ok": True,
            "card_no": target_card,
            "card_tail": str(target_card)[-4:],
            "return_count": n,
            "warn": n >= 2,
            "message": (
                f"该账户（尾号 {str(target_card)[-4:]}）近期有退票记录，请核对后再转"
                if n >= 2 else ""
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return fail(f"退票记录查询失败：{exc}")


# =========================================================================== #
#  账户（卡片挂靠 + 本人账户间资金归集）
# =========================================================================== #
@app.get("/api/accounts")
def list_accounts(uid: int = Depends(current_uid)):
    """当前用户名下全部账户（前端「挂靠已有账户」下拉 / 归集目标选择的数据源）。

    每项含：账户名 / 掩码卡号 / 尾号 / 余额 / 账户类型 / 是否主账户 /
    已挂卡数 / 是否还能再挂卡。信用账户（account_type='credit'）单独标出 ——
    它不参与收付轨道，前端不把它列为归集目标。
    """
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                accounts = card_service.list_accounts(cur, uid)
        return {"ok": True, "count": len(accounts), "accounts": json_safe(accounts)}
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询账户失败：{exc}")


class InternalTransferReq(BaseModel):
    from_account_id: int = Field(description="转出账户（必须是本人储蓄账户）")
    to_account_id: int = Field(description="转入账户（必须是本人储蓄账户）")
    amount: float = Field(gt=0, description="归集金额")


@app.post("/api/accounts/transfer-internal")
def transfer_internal(req: InternalTransferReq, uid: int = Depends(current_uid),
                     _: None = Depends(RequireOpToken("internal_transfer"))):
    """本人账户间资金归集（内部划转）：把某张卡的钱归到主账户，让新卡的钱能用。

    ★ 为什么需要它：单主账户体系下，转账付款扣的是「主账户」（accounts 里 id
      最小的账户，见 POST /api/transfer）。新开的储蓄卡账户「只进不出」，
      钱进得来、花不出去 —— 归集就是把这个缺口补上。

    规则（与需求一致）：
      1) 校验：两账户同属当前用户（★ 归属校验，传别人的 account_id 一律拒绝）；
         转出/转入不能是同一账户；转出账户余额充足；双方都不可以是信用账户
         （信用账户余额语义 = 可用额度，不是存款，不参与划转）。
      2) 执行：from 扣 + to 入（同一事务）。
      3) 免验证码：本人账户之间，风险分级里 internal_transfer 归为「仅支付密码」。
         ★ 支付密码核验在 utils/paypass 侧完成（前端先调 /api/pay/password/verify），
           本端点不再二次发码 —— 但仍是写操作，受电话绑定闸门约束。
      4) 留痕 + 双方收件箱通知（"资金归集 ¥xxx"）：
         扣款方 / 入账方各一条通知，operation_logs action='internal_transfer'。

    ★ 事务边界与转账一致：扣款 / 入账 / 双流水 / 双通知 / 留痕，一个连接一个事务，
      任一失败整体回滚 —— 绝不出现「扣了钱没入账」或「通知了没扣款」。
    """
    try:
        amount = to_decimal(req.amount)
    except ValueError as exc:
        return fail(str(exc))
    if amount <= 0:
        return fail("归集金额必须大于 0")
    if int(req.from_account_id) == int(req.to_account_id):
        return fail("转出账户与转入账户不能是同一个")

    serial_no = None
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                # ---- 1) 双方账户（归属 + 行锁：两把锁按 id 升序，避免并发互锁） ----
                ids = sorted([int(req.from_account_id), int(req.to_account_id)])
                src = card_service.owned_account(cur, uid, ids[0], for_update=True)
                dst = card_service.owned_account(cur, uid, ids[1], for_update=True)
                if int(src["id"]) == int(req.from_account_id):
                    from_acc, to_acc = src, dst
                else:
                    from_acc, to_acc = dst, src

                for acc in (from_acc, to_acc):
                    if (acc.get("account_type") or "debit") == "credit":
                        conn.rollback()
                        return fail("信用账户不参与资金归集（其余额为可用额度，不是存款）")

                # ---- 2) 余额校验 ----
                if Decimal(str(from_acc["balance"])) < amount:
                    db.log_operation(
                        uid, "internal_transfer",
                        {"from_account_id": from_acc["id"], "to_account_id": to_acc["id"],
                         "amount": float(amount), "reason": "insufficient"},
                        "failed", cur=cur)
                    conn.commit()
                    return fail(
                        f"余额不足：账户「{from_acc['name']}」当前余额 "
                        f"{float(from_acc['balance']):.2f} 元，需归集 {float(amount):.2f} 元"
                    )

                new_from = Decimal(str(from_acc["balance"])) - amount
                new_to = Decimal(str(to_acc["balance"])) + amount
                cur.execute("UPDATE accounts SET balance=%s WHERE id=%s",
                            (new_from, from_acc["id"]))
                cur.execute("UPDATE accounts SET balance=%s WHERE id=%s",
                            (new_to, to_acc["id"]))

                # ---- 3) 双方流水（remark 固定"内部划转"，与对外转账区分对账口径）----
                cur.execute(
                    "INSERT INTO transactions (account_id, `date`, amount, category, merchant, "
                    "type, remark) VALUES (%s, CURDATE(), %s, %s, %s, %s, %s)",
                    (from_acc["id"], amount, "转账", f"内部划转至 {to_acc['name']}",
                     "expense", "内部划转"),
                )
                cur.execute(
                    "INSERT INTO transactions (account_id, `date`, amount, category, merchant, "
                    "type, remark) VALUES (%s, CURDATE(), %s, %s, %s, %s, %s)",
                    (to_acc["id"], amount, "转账", f"内部划转自 {from_acc['name']}",
                     "income", "内部划转"),
                )
                serial_no = ("IT" + datetime.now().strftime("%Y%m%d%H%M%S")
                             + f"{random.randint(1000, 9999)}")

                # ---- 4) 双方收件箱通知（同一用户的转出方 / 转入方各一条）----
                inbox.push_notification(
                    uid, "internal_transfer", "资金归集·转出",
                    f"已从账户「{from_acc['name']}」归集 ¥{float(amount):,.2f} 到"
                    f"「{to_acc['name']}」。流水号 {serial_no}，"
                    f"转出后余额 ¥{float(new_from):,.2f}。",
                    meta={"amount": float(amount), "direction": "out", "serial_no": serial_no,
                          "from_account": from_acc["name"], "to_account": to_acc["name"],
                          "balance_after": float(new_from)},
                    cur=cur,
                )
                inbox.push_notification(
                    uid, "internal_transfer", "资金归集·转入",
                    f"账户「{to_acc['name']}」已收到归集资金 ¥{float(amount):,.2f}"
                    f"（来自「{from_acc['name']}」）。流水号 {serial_no}，"
                    f"入账后余额 ¥{float(new_to):,.2f}。",
                    meta={"amount": float(amount), "direction": "in", "serial_no": serial_no,
                          "from_account": from_acc["name"], "to_account": to_acc["name"],
                          "balance_after": float(new_to)},
                    cur=cur,
                )

                # ---- 5) 留痕（安全中心可见；与对外转账的 transfer 语义分开）----
                db.log_operation(
                    uid, "internal_transfer",
                    {"from_account_id": from_acc["id"], "from_account_name": from_acc["name"],
                     "to_account_id": to_acc["id"], "to_account_name": to_acc["name"],
                     "amount": float(amount), "serial_no": serial_no},
                    "success", cur=cur,
                )
            conn.commit()
        except card_service.CardApplyError as exc:
            conn.rollback()
            return fail(exc.message)
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "internal_transfer", json.dumps(req.model_dump()), "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"资金归集失败：{exc}")

    return {
        "ok": True,
        "serial_no": serial_no,
        "amount": float(amount),
        "from_account_id": int(from_acc["id"]),
        "from_account_name": from_acc["name"],
        "from_balance_after": float(new_from),
        "to_account_id": int(to_acc["id"]),
        "to_account_name": to_acc["name"],
        "to_balance_after": float(new_to),
        "message": (f"已将 ¥{float(amount):,.2f} 从「{from_acc['name']}」归集到"
                    f"「{to_acc['name']}」，流水号 {serial_no}"),
    }


# =========================================================================== #
#  订阅路由（已抽离到 utils/subscription.py，组 S 合并重构）
# =========================================================================== #
from utils import subscription  # noqa: E402
app.include_router(subscription.router)  # 订阅域路由
from utils import card as card_domain  # noqa: E402
app.include_router(card_domain.router)  # 卡域路由（组 C+）
from utils import wealth as wealth_domain  # noqa: E402
app.include_router(wealth_domain.router)  # 理财域路由（组 P）


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "mock_bank.main:app",
        host=config.BANK_API_HOST,
        port=config.BANK_API_PORT,
        reload=True,
    )
