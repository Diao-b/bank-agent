"""routes/risk.py —— 支付风控监控（模块 A）HTTP 接口

端点（通道 B 后台；分级见 routes/_common.ROLE_CAPS）：
  GET  /api/risk/rules              规则列表（risk_view）
  POST /api/risk/rules              更新规则（enabled/threshold）（risk_config）
  GET  /api/risk/events             告警队列（脱敏列表）（risk_view）
  POST /api/risk/events/{id}/handle 处理（confirmed/dismissed/closed）（risk_view）
  POST /api/risk/events/{id}/disclose 展开完整明细（理由+审计）（risk_disclose）

★ 最小披露：列表默认脱敏（user_masked / amount_bucket）；展开完整明细需理由并写
  risk_event_views 审计。严禁任何接口返回 chat_messages / transactions.remark。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from routes._common import _risk_guard, _require_cap, _log_admin
from utils import risk
from mock_bank import db
import profile_db
from utils import security
import json

router = APIRouter()


class _RuleUpdateReq(BaseModel):
    rule_code: str
    enabled: bool
    threshold: dict


class _HandleReq(BaseModel):
    status: str
    note: str = ""


class _DiscloseReq(BaseModel):
    reason: str


class _MyAckReq(BaseModel):
    """用户端对本人提醒的反馈：confirmed=是我本人操作；dismissed=忽略。"""
    action: str
    note: str = ""


@router.get("/api/risk/rules")
def api_risk_rules(request: Request):
    _risk_guard(request)
    return {"ok": True, "rules": risk.list_rules()}


@router.post("/api/risk/rules")
def api_risk_rule_update(req: _RuleUpdateReq, request: Request):
    admin_uid = _require_cap(request, "risk_config")
    try:
        res = risk.update_rule(req.rule_code, req.enabled, req.threshold, int(admin_uid))
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **res}


@router.get("/api/risk/events")
def api_risk_events(status: str = "", severity: str = "", page: int = 1,
                   request: Request = None):
    _risk_guard(request)
    return {"ok": True, **risk.list_events(status=status, severity=severity, page=page)}


@router.get("/api/risk/my-events")
def api_risk_my_events(request: Request):
    """用户端：返回当前登录用户自己的风控提醒（仅本人数据，无脱敏必要）。"""
    token = security.extract_token(
        request.headers.get("authorization"), request.headers.get("x-user-token"))
    decoded = profile_db.decode_token_full(token) if token else None
    if not decoded:
        raise HTTPException(status_code=401, detail="登录态无效或已过期")
    uid, _role = decoded
    rows = db.fetch_all(
        "SELECT id, rule_code, severity, status, hit_at, payload, note "
        "FROM risk_events WHERE user_id=%s ORDER BY hit_at DESC LIMIT 50", (uid,))
    events = []
    for r in rows:
        payload = r.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        # ★ 这里是「用户本人的数据」，不存在跨用户披露问题，故返回完整 payload
        #   （含 count/window_min/amount_bucket/payee_masked/threshold 等），
        #   前端据此渲染可读的中文说明；列表脱敏只针对后台「他人数据」视图。
        events.append({
            "id": r["id"], "rule_code": r["rule_code"], "severity": r["severity"],
            "status": r["status"], "hit_at": str(r["hit_at"]),
            "amount_bucket": payload.get("amount_bucket"),
            "payee_masked": payload.get("payee_masked"),
            "payload": payload,
            "note": r.get("note") or "",
        })
    return {"ok": True, "events": events}


@router.post("/api/risk/my-events/{event_id}/ack")
def api_risk_my_event_ack(event_id: int, req: _MyAckReq, request: Request):
    """用户端：本人确认 / 忽略**自己**的 open 风险提醒。

    ★ 与 /api/risk/events/{id}/handle（admin 专属）的区别：这里只校验「提醒归属
      当前登录用户」，且只允许 open → confirmed / dismissed 的单向流转；
      不写 handled_by（那是后台处置人字段），note 自动补一句来源说明。
    """
    token = security.extract_token(
        request.headers.get("authorization"), request.headers.get("x-user-token"))
    decoded = profile_db.decode_token_full(token) if token else None
    if not decoded:
        raise HTTPException(status_code=401, detail="登录态无效或已过期")
    uid, _role = decoded
    if req.action not in ("confirmed", "dismissed"):
        raise HTTPException(status_code=400, detail="action 仅支持 confirmed / dismissed")
    row = db.fetch_one(
        "SELECT id, status FROM risk_events WHERE id=%s AND user_id=%s", (int(event_id), uid))
    if not row:
        raise HTTPException(status_code=404, detail="提醒不存在或不属于当前用户")
    if row.get("status") != "open":
        raise HTTPException(status_code=409, detail="该提醒已处理过，无需重复操作")
    note = req.note or ("本人确认：是我本人操作" if req.action == "confirmed" else "本人反馈：已忽略")
    db.execute(
        "UPDATE risk_events SET status=%s, note=%s, handled_at=NOW() "
        "WHERE id=%s AND user_id=%s AND status='open'",
        (req.action, note, int(event_id), uid))
    return {"ok": True}


@router.post("/api/risk/events/{event_id}/handle")
def api_risk_event_handle(event_id: int, req: _HandleReq, request: Request):
    admin_uid = _risk_guard(request)
    try:
        res = risk.handle_event(int(event_id), req.status, req.note, int(admin_uid))
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **res}


@router.post("/api/risk/events/{event_id}/disclose")
def api_risk_event_disclose(event_id: int, req: _DiscloseReq, request: Request):
    admin_uid = _require_cap(request, "risk_disclose")
    try:
        res = risk.disclose_event(int(event_id), req.reason, int(admin_uid))
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **res}
