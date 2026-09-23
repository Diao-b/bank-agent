"""
路由域：运营管理后台 /api/admin/* 与 /admin 页面（P2.1）。

★ 管理端只写白名单（风控参数 / 用户登录与角色 / 产品目录 / 知识库同步 / 公告）；
  用户资产（余额 / 流水 / 持仓 / 订阅）没有任何管理端修改接口。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
import profile_db
import config
from mock_bank import db
import rules
from utils import paypass
from utils import inbox
from utils.user_provision import provision_new_user
from pydantic import BaseModel
from fastapi.responses import FileResponse
from pathlib import Path
from datetime import datetime
from decimal import Decimal
import json
import secrets as _secrets

from routes._common import _admin_guard, _log_admin, WEB_DIR

router = APIRouter()
@router.get("/api/admin/overview")
def admin_overview(request: Request = None):
    """概览统计：全部真实聚合（假数据问题的终结）。

    - 用户数：users 全量
    - 今日消息数：chat_messages 当日新增
    - 累计转账笔数与总金额：operation_logs action=transfer 且 result=success
    - 验证码发送数：verification_codes 总发送行数
    - 拦截/失败核验数：operation_logs result=failed（verify_check/transfer 等）
    """
    _admin_guard(request)
    from decimal import Decimal
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM users")
                users_total = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM chat_messages WHERE DATE(created_at)=CURDATE()"
                )
                today_messages = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM chat_messages"
                )
                messages_total = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(CAST(JSON_EXTRACT(detail, '$.amount') AS DECIMAL(18,2))), 0) AS amt "
                    "FROM operation_logs WHERE action IN ('transfer','transfer_out') AND result='success'"
                )
                row = cur.fetchone() or {}
                transfer_count = int(row.get("n") or 0)
                transfer_amount = Decimal(str(row.get("amt") or 0))
                cur.execute("SELECT COUNT(*) AS n FROM verification_codes")
                codes_sent = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM operation_logs WHERE result='failed'"
                )
                failed_total = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM notifications WHERE type='verify_code'"
                )
                inbox_codes = int((cur.fetchone() or {}).get("n") or 0)
        return {
            "ok": True,
            "users_total": users_total,
            "today_messages": today_messages,
            "messages_total": messages_total,
            "transfer_count": transfer_count,
            "transfer_amount": float(transfer_amount),
            "codes_sent": codes_sent,
            "inbox_codes": inbox_codes,
            "failed_total": failed_total,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"统计聚合失败：{exc}") from exc


@router.get("/api/admin/users")
def admin_users(request: Request = None):
    """用户管理：列表（账号/称呼/role/实名状态/注册时间/资产概览）。
    ★ 管理员视图可见真实金额（页面顶部注明"管理员视图：含敏感数据"）。"""
    _admin_guard(request)
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT u.id, u.name, u.username, u.role, u.created_at, "
                    "COALESCE(u.real_name, '') AS real_name, "
                    "COALESCE(u.realname_verified, 0) AS realname_verified, "
                    "COALESCE(u.locked, 0) AS locked, "
                    "COALESCE((SELECT SUM(balance) FROM accounts a WHERE a.user_id=u.id "
                          "AND COALESCE(a.account_type,'debit') <> 'credit'), 0) AS assets "
                    "FROM users u ORDER BY u.id ASC LIMIT 500"
                )
                rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "user_id": int(r["id"]),
                "name": r.get("name"),
                "username": r.get("username") or ("user%s" % r["id"]),
                "role": r.get("role") or "user",
                "real_name": r.get("real_name") or "",
                # ★ 是否已完成正式核验：realname_verified=1（与 ③ 基本信息无关）
                "realnamed": bool(r.get("realname_verified")),
                "locked": bool(r.get("locked")),
                "created_at": str(r.get("created_at")),
                "assets": float(r.get("assets") or 0),
            })
        return {"ok": True, "users": out}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"用户列表失败：{exc}") from exc


@router.get("/api/admin/users/{user_id}")
def admin_user_detail(user_id: int, request: Request = None):
    """用户详情：账户/卡/订阅/持仓概览（只读）。"""
    _admin_guard(request)
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, username, role, created_at, real_name, "
                            "COALESCE(locked,0) AS locked FROM users WHERE id=%s",
                            (user_id,))
                user = cur.fetchone()
                if not user:
                    raise HTTPException(status_code=404, detail="用户不存在")
                def q(sql, args=()):
                    cur.execute(sql, args)
                    return cur.fetchall()
                accounts = q("SELECT id, name, balance, account_type FROM accounts WHERE user_id=%s", (user_id,))
                cards = q("SELECT id, bank, card_name, status FROM cards WHERE user_id=%s", (user_id,))
                subs = q("SELECT id, service_name, fee, cycle, status FROM subscriptions WHERE user_id=%s", (user_id,))
                holdings = q(
                    "SELECT h.id, p.name, h.amount, h.status FROM holdings h "
                    "LEFT JOIN products p ON p.id=h.product_id WHERE h.user_id=%s", (user_id,)
                )
        return {"ok": True, "user": {
            "user_id": int(user["id"]), "name": user.get("name"),
            "username": user.get("username") or ("user%s" % user["id"]),
            "role": user.get("role"), "created_at": str(user.get("created_at")),
            "real_name": user.get("real_name") or "",
            "locked": bool(user.get("locked")),
        },
            "accounts": [{**a, "balance": float(a["balance"])} for a in accounts],
            "cards": cards, "subscriptions": subs, "holdings": holdings,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"用户详情失败：{exc}") from exc


@router.get("/api/admin/verify-codes")
def admin_verify_codes(limit: int = 100, request: Request = None):
    """验证码记录：发送记录列表（用户/用途/时间/状态）。明文只在 /code 端点给。"""
    _admin_guard(request)
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT v.id, v.user_id, COALESCE(u.username, CONCAT('user', v.user_id)) AS username, "
                    "v.scene, v.status, v.attempts, v.expires_at, v.verified_at, v.created_at "
                    "FROM verification_codes v LEFT JOIN users u ON u.id = v.user_id "
                    "ORDER BY v.id DESC LIMIT %s", (max(1, min(int(limit), 500)),)
                )
                rows = cur.fetchall()
        now = datetime.now()
        out = []
        for r in rows:
            status = r.get("status")
            if status == "unused" and r.get("expires_at") and now > r["expires_at"]:
                status = "expired"
            out.append({
                "id": int(r["id"]), "user_id": int(r["user_id"]), "username": r.get("username"),
                "scene": r.get("scene"), "status": status,
                "attempts": int(r.get("attempts") or 0),
                "expires_at": str(r["expires_at"]) if r.get("expires_at") else None,
                "verified_at": str(r["verified_at"]) if r.get("verified_at") else None,
                "created_at": str(r.get("created_at")),
            })
        return {"ok": True, "records": out}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"验证码记录失败：{exc}") from exc


@router.get("/api/admin/verify-codes/{vc_id}/code")
def admin_verify_code_plain(vc_id: int, request: Request = None):
    """查看码（管理员可见明文）——真实银行客服后台即如此：受控后台视角可查码，
    用于客服核实"用户说收不到码"类工单；所有调用都落 admin 审计留痕。"""
    admin_uid = _admin_guard(request)
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, code_plain, status, expires_at FROM verification_codes WHERE id=%s",
                    (vc_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="记录不存在")
                db.log_operation(admin_uid, "admin_view_code", {"vc_id": vc_id},
                                 "success", cur=cur)
            conn.commit()
        return {"ok": True, "id": int(row["id"]), "code": row["code_plain"],
                "status": row["status"], "expires_at": str(row["expires_at"])}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"查看码失败：{exc}") from exc


@router.get("/api/admin/logs")
def admin_logs(user_id: int | None = None, action: str = "", result: str = "",
               limit: int = 200, request: Request = None):
    """操作日志：全局时间线（全用户所有 action），筛选器：用户/类型/结果。"""
    _admin_guard(request)
    try:
        sql = (
            "SELECT o.id, o.user_id, o.action, o.detail, o.result, o.created_at, "
            "COALESCE(u.username, CONCAT('user', o.user_id)) AS username "
            "FROM operation_logs o LEFT JOIN users u ON u.id = o.user_id WHERE 1=1"
        )
        args: list = []
        if user_id:
            sql += " AND o.user_id=%s"
            args.append(int(user_id))
        if action:
            sql += " AND o.action=%s"
            args.append(action)
        if result:
            sql += " AND o.result=%s"
            args.append(result)
        sql += " ORDER BY o.id DESC LIMIT %s"
        args.append(max(1, min(int(limit), 1000)))
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rows = cur.fetchall()
        return {"ok": True, "logs": [
            {
                "id": int(r["id"]), "user_id": int(r["user_id"]), "username": r.get("username"),
                "action": r.get("action"), "detail": r.get("detail"),
                "result": r.get("result"), "created_at": str(r.get("created_at")),
            } for r in rows
        ]}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"日志查询失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# C6. 可运营后台：管理端写操作 API
#  —— 三条铁律：
#     1) 每一次写操作都写 admin_logs（who / what / old→new / when）；
#     2) 可改范围严格限定在白名单（风控参数 / 用户登录与角色 / 产品目录 /
#        知识库同步 / 公告）—— 用户资产（余额 / 流水 / 持仓 / 订阅）
#        不设任何管理端修改接口，金融系统的管理员也不能动客户的钱；
#     3) 涉及 RAG 知识文档数字的风控参数，修改即打 pending_sync 标记，
#        知识文档人工确认更新后徽标才消失。
# --------------------------------------------------------------------------- #


KNOWLEDGE_DIR = Path(config.BASE_DIR).parent / "docs" / "knowledge"


class _ConfigUpdate(BaseModel):
    value: str


class _UserLockReq(BaseModel):
    locked: bool




class _UserRoleReq(BaseModel):
    role: str


class _ProductUpdate(BaseModel):
    name: str | None = None
    term: str | None = None
    risk_level: int | None = None
    annual_rate: float | None = None
    min_amount: float | None = None
    status: str | None = None      # on | off


class _AnnouncementReq(BaseModel):
    title: str
    content: str




def _pending_sync_rows():
    """未确认的 RAG 同步义务（知识库"待同步"徽标数据源）。"""
    return db.fetch_all(
        "SELECT l.id, l.action, l.target_id, l.detail, l.created_at, l.admin_id, "
        "       u.username AS admin_name "
        "FROM admin_logs l LEFT JOIN users u ON u.id = l.admin_id "
        "WHERE l.pending_sync = 1 AND l.sync_done_at IS NULL "
        "ORDER BY l.id DESC LIMIT 200"
    )


# ---- 1) 风控参数（system_config 单源） ------------------------------------- #
@router.get("/api/admin/config")
def admin_config_list(request: Request = None):
    admin_uid = _admin_guard(request)
    items = rules.all_rules()
    for it in items:
        it["pending_sync"] = bool(db.fetch_one(
            "SELECT id FROM admin_logs WHERE target_type='config' AND target_id=%s "
            "AND pending_sync=1 AND sync_done_at IS NULL LIMIT 1", (it["key"],)))
    return {"ok": True, "config": items, "pending": _pending_sync_rows(),
            "actor": admin_uid}


@router.put("/api/admin/config/{key}")
def admin_config_update(key: str, req: _ConfigUpdate, request: Request = None):
    admin_uid = _admin_guard(request)
    spec = rules.RULE_SPECS.get(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知参数：{key}")
    try:
        result = rules.set_rule_value(key, req.value, updated_by=admin_uid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知参数：{key}")
    # ★ 同步义务强制化：涉及知识文档数字的参数 → pending_sync 红标
    _log_admin(admin_uid, "config_update", "config", key,
               detail={"old": str(result["old"]), "new": str(result["new"]),
                       "label": spec["label"], "docs": spec["docs"]},
               pending_sync=bool(spec["knowledge"]))
    return {"ok": True, "key": key, "old": str(result["old"]), "new": str(result["new"]),
            "affects_knowledge": bool(spec["knowledge"]),
            "docs_ref": spec["docs"],
            "message": ("参数已生效。注意：" + spec["docs"] + " 知识文档需同步更新"
                        if spec["knowledge"] else "参数已生效。")}


@router.get("/api/admin/config/{key}/history")
def admin_config_history(key: str, limit: int = 50, request: Request = None):
    _admin_guard(request)
    rows = db.fetch_all(
        "SELECT l.id, l.detail, l.pending_sync, l.sync_done_at, l.created_at, "
        "       u.username AS admin_name "
        "FROM admin_logs l LEFT JOIN users u ON u.id = l.admin_id "
        "WHERE l.action='config_update' AND l.target_id=%s "
        "ORDER BY l.id DESC LIMIT %s", (key, min(int(limit), 200)))
    return {"ok": True, "history": rows}


@router.post("/api/admin/config/{key}/sync-confirm")
def admin_config_sync_confirm(key: str, request: Request = None):
    """知识文档人工更新完成后确认 → 清"待同步"标记（记录确认动作本身）。"""
    admin_uid = _admin_guard(request)
    rows = db.fetch_all(
        "SELECT id FROM admin_logs WHERE target_type='config' AND target_id=%s "
        "AND pending_sync=1 AND sync_done_at IS NULL", (key,))
    if not rows:
        return {"ok": True, "cleared": 0, "message": "该参数没有待同步标记"}
    db.execute(
        "UPDATE admin_logs SET sync_done_at=NOW() WHERE target_type='config' "
        "AND target_id=%s AND pending_sync=1 AND sync_done_at IS NULL", (key,))
    _log_admin(admin_uid, "knowledge_sync_confirm", "config", key,
               detail={"cleared": len(rows), "docs": rules.RULE_SPECS[key]["docs"]})
    return {"ok": True, "cleared": len(rows)}


# ---- 2) 用户管理（白名单：锁定 / 重置支付密码 / 角色。禁改资产） ---------- #
@router.post("/api/admin/users/{user_id}/lock")
def admin_user_lock(user_id: int, req: _UserLockReq, request: Request = None):
    admin_uid = _admin_guard(request)
    row = db.fetch_one("SELECT id, username, role FROM users WHERE id=%s", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["role"] == "admin" and req.locked:
        raise HTTPException(status_code=400, detail="不允许锁定管理员账号")
    db.execute("UPDATE users SET locked=%s WHERE id=%s",
               (1 if req.locked else 0, int(user_id)))
    _log_admin(admin_uid, "user_lock" if req.locked else "user_unlock", "user", user_id,
               detail={"username": row["username"]})
    return {"ok": True, "locked": bool(req.locked),
            "message": (f"已锁定 {row['username']}（登录将被拒绝）"
                        if req.locked else f"已解锁 {row['username']}")}


@router.post("/api/admin/users/{user_id}/reset-paypass")
def admin_user_reset_paypass(user_id: int, request: Request = None):
    """重置支付密码为随机 6 位码 —— 明文只通过站内信送达本人（不落库、不回显管理端）。"""
    admin_uid = _admin_guard(request)
    row = db.fetch_one("SELECT id, username FROM users WHERE id=%s", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    new_pwd = "".join(_secrets.choice("0123456789") for _ in range(6))
    try:
        paypass.set_password(int(user_id), new_pwd)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"重置失败：{exc}")
    # ★ 只保留最新一条重置通知：旧码随删除立即失效语义，避免明文长期躺在收件箱
    # ★ LIKE 里的 % 字面量须转义成 %%（pymysql 会做 % 格式化）
    db.execute(
        "DELETE FROM notifications WHERE user_id=%s AND title LIKE '%%支付密码已重置%%'",
        (int(user_id),))
    inbox.push_notification(
        int(user_id), "notice", "【小微银行】支付密码已重置",
        "管理员已应您的要求重置支付密码。新支付密码为：" + new_pwd +
        "（6 位数字，此前的重置码已作废）。请登录后立即在「安全中心」修改为你自己的密码，"
        "切勿告知他人；本条通知阅后建议删除。")
    # ★ 重置码明文绝不落审计 / 日志：只送达本人收件箱
    _log_admin(admin_uid, "user_reset_paypass", "user", user_id,
               detail={"username": row["username"], "notification": "sent"})
    return {"ok": True,
            "message": f"已重置 {row['username']} 的支付密码，新密码已通过站内信送达本人"}


@router.post("/api/admin/users/{user_id}/role")
def admin_user_role(user_id: int, req: _UserRoleReq, request: Request = None):
    admin_uid = _admin_guard(request)
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role 只能是 user 或 admin")
    row = db.fetch_one("SELECT id, username, role FROM users WHERE id=%s", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["id"] == admin_uid:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")
    if row["role"] == req.role:
        return {"ok": True, "message": "角色未变化"}
    db.execute("UPDATE users SET role=%s WHERE id=%s", (req.role, int(user_id)))
    _log_admin(admin_uid, "user_role", "user", user_id,
               detail={"username": row["username"], "old": row["role"], "new": req.role})
    return {"ok": True, "message": f"已将 {row['username']} 的角色从 {row['role']} 调整为 {req.role}"}


@router.delete("/api/admin/users/{user_id}")
def admin_user_delete(user_id: int, confirm_username: str = "", allow_admin: bool = False,
                      request: Request = None):
    """物理删除用户**及其全部业务数据**（单事务级联）。

    历史背景：本库一直没有外键，早先只能手工 `DELETE FROM users`，结果是
    accounts / cards / transactions / subscriptions 全留成孤儿；一旦 MySQL 自增值
    被复用，新注册用户会静默继承死账号的数据（`provision_new_user` 的幂等闸门会跳过初始化）。
    现在改为走本接口：外键 `ON DELETE CASCADE`（sql/upgrade_delete_cascade.sql）
    + 应用层显式单事务删除，双保险。

    安全护栏：
      - 必须传 `confirm_username` 且与目标账号名完全一致（防删错人）；
      - 不允许删除自己；不允许删除库里最后一个管理员；删管理员必须显式 `allow_admin=true`；
      - `admin_logs` 审计留痕不删，删除动作本身写入审计。
    正式环境请改用 users.deleted_at 软删（资金流水按监管应留痕）。
    """
    admin_uid = _admin_guard(request)
    if int(user_id) == int(admin_uid):
        raise HTTPException(status_code=400, detail="不能删除自己的账号")
    row = db.fetch_one("SELECT id, username, role FROM users WHERE id=%s", (int(user_id),))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    uname = row["username"] or ("user%s" % row["id"])
    if not confirm_username or confirm_username.strip() != uname:
        raise HTTPException(
            status_code=400,
            detail=f"请传 confirm_username 且与目标账号名完全一致（当前目标为 {uname}）以确认删除")
    try:
        result = profile_db.delete_user_cascade(int(user_id), allow_admin=bool(allow_admin))
    except profile_db.DeleteUserError as exc:
        status = {"not_found": 404, "last_admin": 400}.get(exc.code, 400)
        raise HTTPException(status_code=status, detail=exc.message) from exc

    _log_admin(admin_uid, "user_delete", "user", user_id,
               detail={"username": uname, "role": result["role"],
                       "deleted": result["deleted"],
                       "deleted_total": result["deleted_total"]})
    return {"ok": True, "deleted": result["deleted"],
            "deleted_total": result["deleted_total"],
            "message": f"已删除 {uname}（#{result['user_id']}）及其 "
                       f"{result['deleted_total']} 行业务数据，操作已计入审计"}


# ---- 3) 理财产品管理（上架 / 下架 / 编辑；下架 → 用户端不可见不可买） ------ #
@router.get("/api/admin/products")
def admin_products(request: Request = None):
    _admin_guard(request)
    rows = db.fetch_all(
        "SELECT p.id, p.name, p.term, p.risk_level, p.annual_rate, p.min_amount, p.status, "
        "       (SELECT COUNT(*) FROM holdings h WHERE h.product_id=p.id) AS holders "
        "FROM products p ORDER BY p.id ASC")
    return {"ok": True, "products": rows}


@router.put("/api/admin/products/{product_id}")
def admin_product_update(product_id: int, req: _ProductUpdate, request: Request = None):
    admin_uid = _admin_guard(request)
    row = db.fetch_one("SELECT * FROM products WHERE id=%s", (product_id,))
    if not row:
        raise HTTPException(status_code=404, detail="产品不存在")
    changes: dict = {}
    if req.name is not None and req.name.strip():
        changes["name"] = req.name.strip()
    if req.term is not None and req.term.strip():
        changes["term"] = req.term.strip()
    if req.risk_level is not None:
        if req.risk_level not in (1, 2, 3):
            raise HTTPException(status_code=400, detail="风险等级只能为 1/2/3")
        changes["risk_level"] = int(req.risk_level)
    if req.annual_rate is not None:
        if not (0 <= float(req.annual_rate) <= 20):
            raise HTTPException(status_code=400, detail="年化收益率需在 0 ~ 20% 之间")
        changes["annual_rate"] = float(req.annual_rate)
    if req.min_amount is not None:
        if float(req.min_amount) < 0:
            raise HTTPException(status_code=400, detail="起购金额不能为负")
        changes["min_amount"] = float(req.min_amount)
    if req.status is not None:
        if req.status not in ("on", "off"):
            raise HTTPException(status_code=400, detail="status 只能是 on（上架）/ off（下架）")
        changes["status"] = req.status
    if not changes:
        return {"ok": True, "message": "没有需要修改的字段"}
    sets = ", ".join(f"{k}=%s" for k in changes)
    db.execute(f"UPDATE products SET {sets} WHERE id=%s",
               (*changes.values(), int(product_id)))
    _log_admin(admin_uid, "product_update", "product", product_id,
               detail={"name": row["name"],
                       "old": {k: str(row[k]) for k in changes},
                       "new": {k: str(v) for k, v in changes.items()}})
    return {"ok": True, "message": f"产品「{row['name']}」已更新", "changes": changes}


# ---- 4) 知识库（docs/knowledge：列表 / 查看 / 待同步 / 重建索引） ---------- #
@router.get("/api/admin/knowledge")
def admin_knowledge(request: Request = None):
    _admin_guard(request)
    docs = []
    if KNOWLEDGE_DIR.exists():
        for f in sorted(KNOWLEDGE_DIR.glob("*.md")):
            if f.name == "README.md":
                continue   # README 是索引本体，不作为知识文档列出
            docs.append({
                "name": f.name, "size": f.stat().st_size,
                "updated_at": datetime.fromtimestamp(
                    f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return {"ok": True, "docs": docs, "pending": _pending_sync_rows(),
            "dir": str(KNOWLEDGE_DIR)}


@router.get("/api/admin/knowledge/{doc_name}")
def admin_knowledge_doc(doc_name: str, request: Request = None):
    _admin_guard(request)
    if "/" in doc_name or "\\" in doc_name or not doc_name.endswith(".md"):
        raise HTTPException(status_code=400, detail="非法文档名")
    f = KNOWLEDGE_DIR / doc_name
    if not f.exists():
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"ok": True, "name": doc_name, "content": f.read_text(encoding="utf-8")}


@router.post("/api/admin/knowledge/{doc_name}/sync-confirm")
def admin_knowledge_sync_confirm(doc_name: str, request: Request = None):
    """人工更新文档后点「已同步」→ 清掉指向该文档的 pending_sync 标记。"""
    admin_uid = _admin_guard(request)
    rows = db.fetch_all(
        "SELECT id, target_id, detail FROM admin_logs WHERE pending_sync=1 AND sync_done_at IS NULL")
    cleared = 0
    for r in rows:
        ref = ""
        try:
            ref = (json.loads(r["detail"] or "{}") or {}).get("docs", "")
        except Exception:  # noqa: BLE001
            ref = ""
        if ref == doc_name or r["target_id"] == doc_name:
            db.execute("UPDATE admin_logs SET sync_done_at=NOW() WHERE id=%s", (r["id"],))
            cleared += 1
    if cleared:
        _log_admin(admin_uid, "knowledge_sync_confirm", "knowledge", doc_name,
                   detail={"cleared": cleared})
    return {"ok": True, "cleared": cleared,
            "message": (f"已确认 {doc_name} 同步完成，清除 {cleared} 条待同步标记"
                        if cleared else f"{doc_name} 没有待同步标记")}


@router.post("/api/admin/knowledge/rebuild")
def admin_knowledge_rebuild(request: Request = None):
    """重建 RAG 知识索引（重扫 docs/knowledge/*.md → 刷新 README.md 索引表）。"""
    admin_uid = _admin_guard(request)
    try:
        import sys as _sys
        sys_root = str(Path(__file__).resolve().parent / "scripts")
        if sys_root not in _sys.path:
            _sys.path.insert(0, sys_root)
        from rebuild_knowledge_index import rebuild as _rebuild_readme
        readme_stats = _rebuild_readme()          # README 索引表（人读）
        from utils import knowledge as _knowledge
        rag_stats = _knowledge.rebuild()          # ★ 检索索引（Agent 真正检索的 chunks + 向量）
        stats = {**readme_stats, "chunks": rag_stats["chunks"], "embed_model": rag_stats["model"]}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"重建失败：{exc}")
    _log_admin(admin_uid, "knowledge_rebuild", "knowledge", "index", detail=stats)
    return {"ok": True, "stats": stats,
            "message": (f"检索索引已重建：{stats.get('docs', 0)} 篇文档 / "
                        f"{stats.get('chunks', 0)} 个片段（embed：{stats.get('embed_model', '')}）")}


# ---- 5) 系统公告：全员收件箱群发（复用通知体系 type=notice） --------------- #
@router.post("/api/admin/announcement")
def admin_announcement(req: _AnnouncementReq, request: Request = None):
    admin_uid = _admin_guard(request)
    if not req.title.strip() or not req.content.strip():
        raise HTTPException(status_code=400, detail="标题与内容必填")
    users = db.fetch_all("SELECT id FROM users")
    for u in users:
        inbox.push_notification(int(u["id"]), "notice",
                                req.title.strip()[:100], req.content)
    _log_admin(admin_uid, "notice_send", "notice", "all",
               detail={"title": req.title.strip(), "recipients": len(users)})
    return {"ok": True, "recipients": len(users),
            "message": f"公告已发送至 {len(users)} 位用户的收件箱"}


# ---- 6) 操作审计：管理员操作日志（admin_logs 独立视图） -------------------- #
@router.get("/api/admin/admin-logs")
def admin_admin_logs(action: str = "", admin_id: int | None = None,
                     limit: int = 200, request: Request = None):
    _admin_guard(request)
    where, args = ["1=1"], []
    if action:
        where.append("l.action=%s"); args.append(action)
    if admin_id:
        where.append("l.admin_id=%s"); args.append(admin_id)
    args.append(min(int(limit), 500))
    rows = db.fetch_all(
        "SELECT l.*, u.username AS admin_name FROM admin_logs l "
        "LEFT JOIN users u ON u.id=l.admin_id WHERE " + " AND ".join(where) +
        " ORDER BY l.id DESC LIMIT %s", tuple(args))
    return {"ok": True, "logs": rows, "pending": _pending_sync_rows()}


# --------------------------------------------------------------------------- #
# C5. /admin 独立页面（独立路由 + 独立布局；与用户端 SPA 分离）
# --------------------------------------------------------------------------- #
@router.get("/admin")
def page_admin():
    return FileResponse(WEB_DIR / "admin.html")


@router.get("/admin/login")
def page_admin_login():
    # C6：运营管理后台独立登录入口（黑金体系 · 独立视觉变体）
    return FileResponse(WEB_DIR / "admin_login.html")
