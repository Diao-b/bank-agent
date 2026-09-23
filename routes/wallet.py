"""
路由域：资产总览 / 收件箱 / 验证码 / 支付密码 / 一次性票据签发 / 实名（P2.1）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
import profile_db
from mock_bank import db
from utils import paypass
from utils import inbox
from utils import op_auth as _op_auth
from pydantic import BaseModel
from datetime import datetime
from datetime import date
from decimal import Decimal

from routes._common import _resolve_uid

router = APIRouter()
# --------------------------------------------------------------------------- #
# A1. 敏感金额遮蔽：/api/overview 默认脱敏（无任何真值字段）；
#     ?reveal=true 返回真实聚合（总资产=余额合计；今日/本月支出=流水聚合）。
#     ★ 后端全部 Decimal 计算；reveal 不留痕（主动查询不算"泄露事件"，
#       真正的告警留痕在风控/核验路径，见 utils/paypass._log）。
# --------------------------------------------------------------------------- #
@router.get("/api/overview")
def overview(reveal: str = "", request: Request = None):
    from decimal import Decimal
    uid = _resolve_uid(request)
    if not str(reveal).lower() in ("true", "1", "yes"):
        # ★ 脱敏响应：绝不出现任何真值字段（连 0.00 都不给，避免"¥0.00 假值"）
        return {
            "ok": True, "masked": True,
            "total_assets": None, "today_expense": None, "month_expense": None,
            "mask_text": "¥ ··· ··",
            "hint": "金额已脱敏，主动查询（reveal=true）才下发真实聚合",
        }
    # reveal=true：真实聚合（Decimal -> float 仅供 JSON 序列化）
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(balance), 0) AS total FROM accounts "
                    "WHERE user_id=%s AND COALESCE(account_type,'debit') <> 'credit'", (uid,)
                )
                total = Decimal(str((cur.fetchone() or {}).get("total") or 0))
                now = datetime.now()
                cur.execute(
                    "SELECT COALESCE(SUM(t.amount), 0) AS exp FROM transactions t "
                    "JOIN accounts a ON a.id = t.account_id "
                    "WHERE a.user_id=%s AND t.type='expense' AND t.date=CURDATE()",
                    (uid,),
                )
                today = Decimal(str((cur.fetchone() or {}).get("exp") or 0))
                cur.execute(
                    "SELECT COALESCE(SUM(t.amount), 0) AS exp FROM transactions t "
                    "JOIN accounts a ON a.id = t.account_id "
                    "WHERE a.user_id=%s AND t.type='expense' "
                    "AND t.date >= DATE_FORMAT(CURDATE(), '%%Y-%%m-01')",
                    (uid,),
                )
                month = Decimal(str((cur.fetchone() or {}).get("exp") or 0))
        return {
            "ok": True, "masked": False,
            "total_assets": float(total),
            "today_expense": float(today),
            "month_expense": float(month),
            "server_date": now.strftime("%Y-%m-%d"),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"聚合查询失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# C2. 收件箱 API（全部认证 + 用户隔离；不代理到 mock_bank，走本库 notifications）
#     收件箱轮询（顶栏 unread_count）不要用 SSE —— 现有 SSE 是对话流专用，
#     混用易出 bug；前端 5s 轮询最稳。
#     ★ 惰性 housekeeping：收件箱是"跨行到账 / 退票"通知的落点，轮询即触发
#       到期清算（与订阅扣款引擎同一惰性模式），用户打开收件箱就能看到结果。
# --------------------------------------------------------------------------- #
def _run_housekeeping(uid: int) -> None:
    """读接口顺带触发后台惰性任务（订阅补扣 + 跨行到期清算）。
    失败只告警，绝不阻断读接口。"""
    try:
        from utils import settlement_engine

        settlement_engine.process_housekeeping(uid)
    except Exception as exc:  # noqa: BLE001
        print("[warn] housekeeping 失败（不影响读接口）:", exc)


@router.get("/api/inbox")
def inbox_list(unread_only: str = "", limit: int = 50, request: Request = None):
    from utils import inbox as inbox_mod
    uid = _resolve_uid(request)
    _run_housekeeping(uid)
    try:
        items = inbox_mod.list_notifications(
            uid, unread_only=str(unread_only).lower() in ("1", "true", "yes"), limit=limit
        )
        return {"ok": True, "messages": items, "unread_count": inbox_mod.unread_count(uid)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"收件箱读取失败：{exc}") from exc


@router.get("/api/inbox/unread_count")
def inbox_unread_count(request: Request = None):
    from utils import inbox as inbox_mod
    uid = _resolve_uid(request)
    _run_housekeeping(uid)
    return {"ok": True, "count": inbox_mod.unread_count(uid)}


@router.post("/api/inbox/{notification_id}/read")
def inbox_mark_read(notification_id: int, request: Request = None):
    from utils import inbox as inbox_mod
    uid = _resolve_uid(request)
    hit = inbox_mod.mark_read(uid, notification_id)
    if not hit:
        raise HTTPException(status_code=404, detail="消息不存在")
    return {"ok": True, "unread_count": inbox_mod.unread_count(uid)}


@router.post("/api/inbox/read_all")
def inbox_mark_all_read(request: Request = None):
    from utils import inbox as inbox_mod
    uid = _resolve_uid(request)
    return {"ok": True, "marked": inbox_mod.mark_all_read(uid), "unread_count": 0}


# --------------------------------------------------------------------------- #
# C3. 验证码发码/校验收口到本服务（落库 + 收件箱双写同事务）
#     ★ 必须定义在兜底代理（/api/{path:path}）之前，否则会被代理吞掉。
#     mock_bank 的同名端点保留作兼容，但业务链路一律走这里。
# --------------------------------------------------------------------------- #
class _VerifySendReq(BaseModel):
    scene: str = "transfer"


class _VerifyCheckReq(BaseModel):
    code: str


_SCENE_PURPOSE = {
    "transfer": "转账安全验证",
    "buy_product": "理财购买安全验证",
    "redeem": "赎回安全验证",
    "freeze_card": "冻结银行卡安全验证",
    "unfreeze_card": "解冻银行卡安全验证",
    "cancel_subscription": "取消订阅安全验证",
    "realname": "实名认证",
    "phone_bind": "绑定手机",
}


@router.post("/api/verify/send")
def verify_send_v2(req: _VerifySendReq, request: Request = None):
    from utils import verify_codes
    # ★ 安全修复：本接口必须持有效登录态（此前无 token 会回落默认账号真发码），
    #   且 scene 必须在业务白名单内（防未知场景刷码写库）。
    h = (request.headers.get("authorization") or request.headers.get("x-user-token") or "")
    if h.startswith("Bearer "):
        h = h[7:]
    ident = profile_db.decode_token_full(h.strip())
    if not ident:
        raise HTTPException(status_code=401, detail="请先登录后再获取验证码")
    uid = ident[0]
    allowed_scenes = set(_SCENE_PURPOSE) | {"phone_bind", "internal_transfer"}
    if req.scene not in allowed_scenes:
        raise HTTPException(status_code=400, detail=f"非法验证码场景：{req.scene}")
    purpose = _SCENE_PURPOSE.get(req.scene, req.scene or "身份核验")
    try:
        r = verify_codes.issue_code(uid, scene=req.scene, purpose=purpose)
    except Exception as exc:  # noqa: BLE001
        # 双写（验证表+收件箱）任一失败 → 整体失败，绝不返回"已发送"
        raise HTTPException(status_code=500, detail=f"验证码发送失败：{exc}") from exc
    # 演示接口不再回显 code —— 收件箱就是"手机"，用户去收件箱取码
    return {"ok": True, "scene": req.scene, "expires_in": r["expires_in"],
            "notification_id": r["notification_id"], "message": r["message"]}


@router.post("/api/verify/check")
def verify_check_v2(req: _VerifyCheckReq, request: Request = None):
    from utils import verify_codes
    uid = _resolve_uid(request)
    r = verify_codes.check_code(uid, req.code)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "验证码不正确")}
    return {"ok": True, "verified": True}


# --------------------------------------------------------------------------- #
# B3. 支付密码：核验 / 设置
#     ★密码禁入任何持久化记录：核验成功只在进程内放置短 TTL "已通过"标记，
#       write_flow 的 verify 队列据此放行；密码本身不进对话、不进 checkpoint。
# --------------------------------------------------------------------------- #
class _PayPwdReq(BaseModel):
    password: str
    thread_id: str | None = None


@router.post("/api/pay/password/verify")
def pay_password_verify(req: _PayPwdReq, request: Request = None):
    from utils import paypass
    uid = _resolve_uid(request)
    r = paypass.verify(uid, req.password)
    return r


@router.post("/api/pay/password/set")
def pay_password_set(req: _PayPwdReq, request: Request = None):
    from utils import paypass
    uid = _resolve_uid(request)
    r = paypass.set_password(uid, req.password)
    if r.get("ok"):
        paypass.mark_passed(uid)  # 设置完直接视为已通过本次核验
    return r


@router.get("/api/pay/password/status")
def pay_password_status(request: Request = None):
    from utils import paypass
    uid = _resolve_uid(request)
    return {"ok": True, "has_password": paypass.has_password(uid), "locked": paypass.is_locked(uid)}


# --------------------------------------------------------------------------- #
# P0.2：一次性操作授权票据签发端点（op-auth token）
#   ★ 直连写端点（transfer / buy / redeem / freeze / ...）消费本票据；无有效票据 → 4xx。
#   ★ 本端点要求"已通过短信验证码（scene 匹配且未过期）+ 已通过支付密码"，
#     即 walk 真实的 verify/paypass 路径后才发证 —— 生产无任何绕过分支。
#   ★ write_flow 在图内核实通过后，直接在进程内调 _op_auth.issue_op_token 发证，
#     不经过本 HTTP 端点；本端点仅供对话流之外的"真实票据路径"测试 / 外部合规调用使用。
# --------------------------------------------------------------------------- #
class _OpAuthorizeReq(BaseModel):
    scene: str
    code: str | None = None  # 短信验证码（该 scene 需短信核实时必填；由调用方从收件箱取）


@router.post("/api/op/authorize")
def op_authorize(req: _OpAuthorizeReq, request: Request = None):
    uid = _resolve_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    # 1) 支付密码必须已通过（进程内标记，由 /api/pay/password/verify 设置）
    from utils import paypass
    if not paypass.has_passed(uid):
        raise HTTPException(
            status_code=401,
            detail="请先完成支付密码核验（/api/pay/password/verify）后再申请操作授权",
        )
    # 2) 该 scene 必须存在一条"已核验且未过期"的验证码（防绕过短信核验直接发证）
    from utils import verify_codes
    try:
        conn = db.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status, verified_at, code_hash FROM verification_codes "
                    "WHERE user_id=%s AND scene=%s AND status='verified' "
                    "AND verified_at > DATE_SUB(NOW(), INTERVAL 5 MINUTE) "
                    "ORDER BY id DESC LIMIT 1",
                    (int(uid), req.scene),
                )
                vc = cur.fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        vc = None
    if not vc:
        raise HTTPException(
            status_code=401,
            detail="本次操作缺少有效的短信验证码核验，请先获取并核验验证码",
        )
    # 2.5) 若调用方带来了验证码，额外比对哈希，确保"确实核验过这一条"
    if req.code:
        if verify_codes._hash(req.code.strip()) != vc["code_hash"]:
            raise HTTPException(status_code=401, detail="短信验证码与已核验记录不符")
    # 3) 签发一次性票据（单次有效、按 scene 绑定、短 TTL）
    r = _op_auth.issue_op_token(uid, req.scene)
    return {
        "ok": True,
        "token": r["token"],
        "expires_in": r["expires_in"],
        "scene": r["scene"],
    }


# --------------------------------------------------------------------------- #
# C3-验收#3：实名认证验证码同样进收件箱
#   旧版"实名"只是填个姓名文本框。现在实名要走验证码（收件箱送达），
#   核验通过才落 users.real_name —— 复用 C3 的双写同事务发码通道。
# --------------------------------------------------------------------------- #
class _RealnameReq(BaseModel):
    real_name: str
    code: str


@router.post("/api/realname/send_code")
def realname_send_code(request: Request = None):
    from utils import verify_codes
    uid = _resolve_uid(request)
    try:
        verify_codes.issue_code(uid, scene="realname", purpose="实名认证")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"验证码发送失败：{exc}") from exc
    return {"ok": True, "message": "验证码已发送至您的收件箱，5 分钟内有效"}


@router.post("/api/realname/confirm")
def realname_confirm(req: _RealnameReq, request: Request = None):
    from utils import verify_codes
    uid = _resolve_uid(request)
    name = (req.real_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写真实姓名")
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                # ★ 拦截：实名认证一旦完成，禁止在线修改（敏感身份变更必须柜台办理）。
                #   即使前端隐藏了表单，端点也必须再挡一道，防止被直接打 API 绕过。
                cur.execute("SELECT realname_verified, real_name FROM users WHERE id=%s", (uid,))
                row = cur.fetchone()
                if row and int(row.get("realname_verified") or 0) == 1:
                    return {
                        "ok": False,
                        "error": "您已完成实名认证，姓名属于敏感身份信息，"
                                 "如需变更请携带有效证件前往柜台办理。",
                    }
                r = verify_codes.check_code(uid, req.code, scene="realname")
                if not r.get("ok"):
                    return {"ok": False, "error": r.get("error", "验证码不正确")}
                # 核验通过 → 落库 + 标记已认证（同一连接、同一事务）
                cur.execute(
                    "UPDATE users SET real_name=%s, realname_verified=1 WHERE id=%s",
                    (name, uid),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"ok": True, "real_name": name, "message": "实名认证完成"}
