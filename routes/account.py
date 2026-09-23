"""
路由域：账号与页面（P2.1）。

认证 / 注册 / 画像 / 手机绑定 / 会话列表 / 静态页面（/ · /login · /chat）。
★ 与 chat 的交互点（_assert_conversation_owner / _get_driver）留在 run_server。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from utils import security
import profile_db
import config
from mock_bank import db
import rules
from utils import login_guard
from utils.user_provision import provision_new_user
from pydantic import BaseModel
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
import re

from routes._common import _resolve_uid, WEB_DIR

router = APIRouter()


class ProfileUpdate(BaseModel):
    real_name: str | None = None
    gender: str | None = None
    occupation: str | None = None
    preferences: dict | None = None


# --------------------------------------------------------------------------- #
# 认证（审计项 M3）：原先任意密码可登录、register 不建用户、token 无签名无过期。
#   现在：用户密码走 pbkdf2 哈希校验；token 为 HMAC 签名且 24h 过期；
#   register 真正落库；解析出的 user_id 用于画像 / 会话 / 聊天落库。
#   前端请求失败时会回落本地 mock（登录页 mockAuth 仍要求演示密码 123456）。
# --------------------------------------------------------------------------- #
class _AuthReq(BaseModel):
    username: str = ""
    password: str = ""
    confirm: str = ""


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


# ---- 登录防爆破（审计 R5）：同账号连续失败 N 次锁 M 分钟 ----
# ★ P1.4：计数与锁定从「进程内 dict」迁到 DB（login_attempts）—— 多 worker 下不再被稀释。
#   （原先 _LOGIN_FAILS 是进程内 dict：N 个 worker = 放宽 N 倍。）


@router.post("/api/auth/login")
def auth_login(req: _AuthReq):
    username = (req.username or "").strip()
    locked_min = login_guard.locked_remaining_minutes(username)
    if locked_min is not None:
        raise HTTPException(status_code=429, detail=f"失败次数过多，请约 {locked_min} 分钟后再试")
    res = profile_db.authenticate(username, req.password or "")
    if isinstance(res, dict) and res.get("error") == "locked":
        # C6：管理员锁定的账号 —— 明确拒绝（验收：锁定用户 → 该用户登录被拒）
        raise HTTPException(status_code=403, detail="账号已被锁定，请联系管理员或柜台处理")
    if not res:
        n, locked = login_guard.record_failure(
            username, rules.LOGIN_MAX_FAILS, rules.LOGIN_LOCK_SECONDS)
        if locked:
            raise HTTPException(status_code=429, detail="失败次数过多，账号已临时锁定 15 分钟")
        raise HTTPException(status_code=401,
                            detail=f"用户名或密码错误（还可尝试 {rules.LOGIN_MAX_FAILS - n} 次）")
    login_guard.clear_failures(username)
    # C5：token payload 携带 role；admin 登录后前端据此默认跳 /admin
    role = profile_db.get_user_role(res["user_id"]) or "user"
    return {
        "ok": True,
        "token": profile_db.issue_token(res["user_id"], role=role),
        "user": {"username": res["username"], "user_id": res["user_id"], "role": role},
    }


@router.post("/api/auth/guest")
def auth_guest(req: _AuthReq):
    # ★ 安全修复（审计 P0.1）：演示游客通道是"认证后门"——无需任何凭证即为默认账号签发 token。
    #   仅开发态（ENV=dev 且 DEV_ALLOW_GUEST=1）可用；生产环境无条件关闭，返回 403。
    if not config.auth_guest_enabled():
        raise HTTPException(status_code=403, detail="演示游客通道已关闭，请使用账号登录")
    # 单账号演示：游客映射到默认账号（无需注册）
    uid = config.DEFAULT_USER_ID
    return {
        "ok": True,
        "token": profile_db.issue_token(uid),
        "user": {"username": "guest", "user_id": uid},
    }


@router.post("/api/auth/register")
def auth_register(req: _AuthReq):
    if not req.username or not req.password:
        raise HTTPException(status_code=400, detail="用户名和密码必填")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    res = profile_db.create_user(req.username, req.password)
    if not res:
        raise HTTPException(status_code=409, detail="用户名已存在")

    # ★ uid 复用兜底（2026-09-21）：MySQL 自增值一旦被复用，而上一个主人当初只被手工
    #   `DELETE FROM users`（业务数据没清），新账号名下就会残留别人的账户 / 卡 / 流水；
    #   更糟的是 provision_new_user 的幂等闸门（已有 accounts 就跳过）会**静默跳过初始化**，
    #   于是新用户直接继承死账号的全部数据。这里在「刚建号」这个唯一可信的时点先清残留。
    recycled = 0
    try:
        residue = profile_db.count_user_assets(res["user_id"])
        if residue:
            recycled = residue
            purged = profile_db.purge_user_assets(res["user_id"])
            print(f"[warn] uid={res['user_id']} 命中 uid 复用：上一个用户残留 {residue} 行，"
                  f"已清空 {purged['deleted_total']} 行后重新初始化")
            left = profile_db.count_user_assets(res["user_id"])
            if left:
                raise RuntimeError(f"残留未清干净（剩 {left} 行）")
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 用户 {res['user_id']} 残留清理失败：{exc}")
        raise HTTPException(status_code=500, detail=f"账号创建前的残留数据清理失败：{exc}") from exc

    # ★ 注册即开户：立刻给新用户一套独立的演示数据沙箱（账户 / 卡 / 收款人 / 流水 / 订阅）。
    #   以前 create_user 只 INSERT users，新用户登录后查余额为空、转账报"账户不存在"。
    #   provision_new_user 自带幂等闸门（已有 accounts 记录就跳过），重复注册不会脏数据。
    provision: dict = {}
    try:
        provision = provision_new_user(res["user_id"], req.username)
    except Exception as exc:  # noqa: BLE001
        # 初始化失败不回滚注册：账号已建、能登录，可事后用 scripts/provision_user.py 补。
        # 但必须报出来 —— 静默失败会让"新用户没数据"这个问题再次隐身。
        print(f"[error] 用户 {res['user_id']} 数据沙箱初始化失败：{exc}")
        provision = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "token": profile_db.issue_token(res["user_id"]),
        "user": {"username": res["username"], "user_id": res["user_id"]},
        "provision": provision,
        # true → 该 uid 曾被占用（自增值复用），已清掉上一个用户的残留数据
        "uid_recycled": bool(recycled),
        "residue_purged": recycled,
    }


# --------------------------------------------------------------------------- #
# 用户画像 / 个性化问候语
#   当前登录用户优先取自签名 token（审计项 M3），缺省回落演示账号。
# --------------------------------------------------------------------------- #
@router.get("/api/profile")
def get_profile(req: Request):
    """返回当前用户的完整画像（含账户概览、用户名、头像占位）。"""
    payload = profile_db.get_profile_payload(_resolve_uid(req))
    if not payload:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, **payload}


@router.put("/api/profile")
def put_profile(req: ProfileUpdate, request: Request):
    """编辑可编辑字段；gender 变更自动重算 title_prefix；返回最新画像（含 title_prefix）。"""
    try:
        payload = profile_db.update_user_profile(
            _resolve_uid(request),
            real_name=req.real_name,
            gender=req.gender,
            occupation=req.occupation,
            preferences=req.preferences,
        )
    except ValueError as exc:
        # 安全不变式违规（如已实名用户改姓名）→ 明确 400，不静默吞掉
        raise HTTPException(status_code=400, detail=str(exc))
    if not payload:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, **payload}


# --------------------------------------------------------------------------- #
#  手机号绑定（渐进式账户：注册即可对话/浏览，绑手机后解锁写操作）
#   流程：bind-request（校验号码 + 发码 + 暂存 phone_pending）
#        → bind-confirm（校验码 + 落 phone + 写 operation_logs）
#   验证码复用既有基础设施（scene='phone_bind'），与转账验证码隔离。
# --------------------------------------------------------------------------- #
class _PhoneBindReq(BaseModel):
    phone: str | None = None
    code: str | None = None


@router.post("/api/profile/phone/bind-request")
def phone_bind_request(req: _PhoneBindReq, request: Request):
    """申请绑定手机号：校验 11 位、1 开头 → 暂存待绑定号 → 发 6 位验证码（5 分钟有效）。

    不回显验证码（收件箱就是"手机"）；只回 expires_in。
    """
    uid = _resolve_uid(request)
    phone = (req.phone or "").strip()
    if not re.fullmatch(r"1\d{10}", phone):
        return JSONResponse(
            {"ok": False, "error": "请输入正确的 11 位手机号（以 1 开头）"}, status_code=400
        )
    # 暂存待绑定号；确认成功才落到 phone 列
    with profile_db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET phone_pending=%s WHERE id=%s", (phone, uid))
        conn.commit()
    from utils import verify_codes
    try:
        r = verify_codes.issue_code(uid, scene="phone_bind", purpose="绑定手机")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"验证码发送失败：{exc}") from exc
    return {
        "ok": True,
        "expires_in": r["expires_in"],
        "message": r["message"],
    }


@router.post("/api/profile/phone/bind-confirm")
def phone_bind_confirm(req: _PhoneBindReq, request: Request):
    """确认绑定：校验验证码（scene='phone_bind'）→ 取暂存号落 phone → 写 operation_logs。"""
    uid = _resolve_uid(request)
    code = (req.code or "").strip()
    if not code:
        return JSONResponse({"ok": False, "error": "请输入验证码"}, status_code=400)
    from utils import verify_codes
    check = verify_codes.check_code(uid, code, scene="phone_bind")
    if not check.get("ok"):
        return JSONResponse(
            {"ok": False, "error": check.get("error", "验证码不正确")}, status_code=400
        )
    # 校验通过：取出暂存的待绑定号（scene 已限定，phone_pending 必然存在）
    with profile_db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT phone_pending FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
            pending = (row or {}).get("phone_pending") if row else None
            if not pending:
                return JSONResponse(
                    {"ok": False, "error": "绑定流程已失效，请重新获取验证码"}, status_code=400
                )
            masked = profile_db.mask_phone(pending)
            cur.execute(
                "UPDATE users SET phone=%s, phone_pending=NULL, phone_tail=%s WHERE id=%s",
                (pending, masked, uid),
            )
            db.log_operation(uid, "phone_bind", f"用户绑定手机号 {masked}", "success", cur=cur)
        conn.commit()
    payload = profile_db.get_profile_payload(uid)
    return {"ok": True, "message": "手机号绑定成功", **payload}



@router.get("/api/conversations/{conv_id}/greeting")
def conversation_greeting(conv_id: str, request: Request):
    """新会话首条问候语：首次生成并入库（role='assistant'、node_trace='[]'），
    历史会话直接返回已存问候（幂等，不重复生成）。"""
    text = profile_db.get_or_create_greeting(conv_id, _resolve_uid(request))
    return {"ok": True, "greeting": text, "conversation_id": conv_id}


# --------------------------------------------------------------------------- #
# 会话列表 / 历史回读（审计项 M2）
#   此前这三个能力完全不存在（实测 /api/conversations 等一律 404），
#   对话只活在前端 React 内存里，刷新即丢。
# --------------------------------------------------------------------------- #
@router.get("/api/conversations")
def list_conversations(limit: int = 50, request: Request = None):
    """当前用户的会话列表，按最近活跃倒序。"""
    uid = _resolve_uid(request) if request else config.DEFAULT_USER_ID
    try:
        return {"ok": True, "conversations": profile_db.list_conversations(uid, limit)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "conversations": []}


@router.get("/api/conversations/{conv_id}/messages")
def conversation_messages(conv_id: str, limit: int = 500, request: Request = None):
    """回读指定会话的完整消息（含 greeting），供刷新后恢复现场。

    ★ 归属过滤：只允许读当前用户名下的会话（防知道 conv_id 就扒别人对话）。
    """
    uid = _resolve_uid(request) if request else config.DEFAULT_USER_ID
    try:
        return {"ok": True, "conversation_id": conv_id,
                "messages": profile_db.get_conversation_messages(conv_id, limit, user_id=uid)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "messages": []}


@router.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str, request: Request = None):
    """删除会话及其消息。返回是否命中；LangGraph 的 checkpoint 不在 MySQL，需另行清理。"""
    uid = _resolve_uid(request) if request else config.DEFAULT_USER_ID
    try:
        return {"ok": True, "deleted": profile_db.delete_conversation(conv_id, uid)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "deleted": False}


# 页面路由：/ 与 /login 与 /chat（定义在前，优先于静态挂载）
@router.get("/")
def page_root():
    # 首页即登录门户：运行程序后先到登录页（可选登录 / 游客模式），而非直接裸进聊天页
    return FileResponse(WEB_DIR / "login.html")


@router.get("/login")
def page_login():
    return FileResponse(WEB_DIR / "login.html")


@router.get("/chat")
def page_chat():
    return FileResponse(WEB_DIR / "index.html")
