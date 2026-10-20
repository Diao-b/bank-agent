"""routes/card_key.py —— 卡密充值（模块 B）HTTP 接口

端点：
  POST /api/card-key/generate    生成批次（super_admin；明文仅此一次返回，零余额操作）
  GET  /api/card-key/batches     批次列表（脱敏，admin+）
  GET  /api/card-key/cards      批次内卡密（脱敏，admin+）
  POST /api/card-key/void       作废（super_admin；仅 unused）
  GET  /api/card-key/export      导出批次元数据 CSV（理由+留痕，admin+）
  POST /api/card-key/redeem     原子核销（用户；幂等+限流+上限+账实一致）
  GET  /api/card-key/my-redeems 本人核销历史（用户）
  GET  /api/card-key/limits     充值上限（用户；充值 UI 用）

★ 安全：所有明文只在 generate 响应一次性返回；list/cards/export 一律脱敏；
  作废/导出强制理由并写 admin_logs；核销走 _resolve_uid（登录态）+ 卡密本身（持有因子）。
"""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from decimal import Decimal

from routes._common import _cardkey_guard, _resolve_uid, _log_admin
from utils import card_keys
from utils import idempotency
import rules

router = APIRouter()


# --------------------------------------------------------------------------- #
#  请求体
# --------------------------------------------------------------------------- #
class _GenReq(BaseModel):
    amount: str | float
    count: int
    remark: str = ""


class _VoidReq(BaseModel):
    card_id: int
    reason: str


class _RedeemReq(BaseModel):
    code: str
    account_id: int | None = None


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return (request.client.host if request.client else "0.0.0.0")


# --------------------------------------------------------------------------- #
#  生成（super_admin）
# --------------------------------------------------------------------------- #
@router.post("/api/card-key/generate")
def api_card_generate(req: _GenReq, request: Request):
    admin_uid = _cardkey_guard(request, "cardkey_generate")
    try:
        res = card_keys.generate_batch(Decimal(str(req.amount)), int(req.count),
                                       int(admin_uid), req.remark or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # ★ 留痕在 card_keys.generate_batch 内统一完成（单一来源，只记元数据不记明文）
    return {"ok": True, "batch_no": res["batch_no"], "amount": res["amount"],
            "count": res["count"], "codes": res["codes"]}


# --------------------------------------------------------------------------- #
#  列表（admin+）
# --------------------------------------------------------------------------- #
@router.get("/api/card-key/batches")
def api_card_batches(request: Request):
    _cardkey_guard(request, "cardkey_list")
    return {"ok": True, "batches": card_keys.list_batches()}


@router.get("/api/card-key/cards")
def api_card_cards(batch_no: str = "", request: Request = None):
    _cardkey_guard(request, "cardkey_list")
    if not batch_no:
        raise HTTPException(status_code=400, detail="batch_no 必填")
    return {"ok": True, "batch_no": batch_no, "cards": card_keys.list_cards(batch_no)}


# --------------------------------------------------------------------------- #
#  作废（super_admin）
# --------------------------------------------------------------------------- #
@router.post("/api/card-key/void")
def api_card_void(req: _VoidReq, request: Request):
    admin_uid = _cardkey_guard(request, "cardkey_void")
    res = card_keys.void_card(int(req.card_id), int(admin_uid), req.reason or "")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "作废失败"))
    # ★ 留痕在 card_keys.void_card 内统一完成（单一来源）
    return {"ok": True, "card_id": req.card_id}


# --------------------------------------------------------------------------- #
#  导出（admin+；理由+留痕；只含脱敏元数据）
# --------------------------------------------------------------------------- #
@router.get("/api/card-key/export")
def api_card_export(batch_no: str = "", reason: str = "", request: Request = None):
    admin_uid = _cardkey_guard(request, "cardkey_export")
    if not batch_no:
        raise HTTPException(status_code=400, detail="batch_no 必填")
    if not (reason or "").strip():
        raise HTTPException(status_code=400, detail="导出必须填写理由（留痕）")
    cards = card_keys.export_batch_meta(batch_no)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["code_prefix", "amount", "status", "used_at", "used_by"])
    for c in cards:
        w.writerow([c.get("code_prefix"), c.get("amount"), c.get("status"),
                    c.get("used_at"), c.get("used_by")])
    _log_admin(admin_uid, "cardkey_export", "card_batch", batch_no,
               detail={"reason": reason, "count": len(cards)})
    headers = {
        "Content-Disposition": f"attachment; filename=cardkey_{batch_no}.csv",
        "X-Export-Reason": (reason or "")[:200],
    }
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv", headers=headers)


# --------------------------------------------------------------------------- #
#  原子核销（用户）
# --------------------------------------------------------------------------- #
@router.post("/api/card-key/redeem")
def api_card_redeem(req: _RedeemReq, request: Request,
                    idem_key: str | None = Header(None, alias="Idempotency-Key")):
    uid = _resolve_uid(request)
    if not idem_key:
        raise HTTPException(status_code=400, detail="缺少 Idempotency-Key 头（幂等必需）")
    body = json.dumps(req.model_dump(), ensure_ascii=False, sort_keys=True)
    request_hash = idempotency.req_hash("POST", "/api/card-key/redeem", body)
    try:
        res = card_keys.redeem(req.code, uid, req.account_id, idem_key,
                               request_hash, _client_ip(request))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"核销失败：{exc}")
    if not res.get("ok"):
        code = res.get("code")
        if code == "rate_limited":
            raise HTTPException(status_code=429, detail=res.get("error"))
        if code == "conflict":
            raise HTTPException(status_code=409, detail=res.get("error"))
        raise HTTPException(status_code=400, detail=res.get("error"))
    # ★ 模块 A 联动：核销成功后跑 R-CHARGE（异常充值）评估（缺失则静默跳过）
    try:
        from utils import risk
        risk.eval_charge_redeem(uid, Decimal(str(res["amount"])), res.get("txn_id"))
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, **res}


# --------------------------------------------------------------------------- #
#  本人核销历史（用户）
# --------------------------------------------------------------------------- #
@router.get("/api/card-key/my-redeems")
def api_card_my_redeems(request: Request):
    uid = _resolve_uid(request)
    return {"ok": True, "redeems": card_keys.my_redeems(uid)}


# --------------------------------------------------------------------------- #
#  充值上限（用户；充值 UI 用）
# --------------------------------------------------------------------------- #
@router.get("/api/card-key/limits")
def api_card_limits(request: Request):
    _resolve_uid(request)
    return {
        "ok": True,
        "single_recharge_max": str(rules.get_rule("card_key_single_recharge_max")),
        "daily_recharge_max": str(rules.get_rule("card_key_daily_recharge_max")),
    }
