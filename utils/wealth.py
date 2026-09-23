"""utils/wealth.py —— 理财域（商品目录 + 持仓 + 申购 / 赎回）。

组 P（合并式重构，2026-09-11）：原内联于 mock_bank.main 的
  GET /api/products、GET /api/holdings、POST /api/products/buy、POST /api/redeem
整体抽入本文件，路由与理财域同处（`router = APIRouter()`），main 仅 include_router。

★ 依赖约定（复用组 T 惰性模式）：顶层不 import mock_bank.main；共享 helper
  （json_safe / fail / to_decimal）在各路由函数内惰性 import，避免循环导入。
★ products 是平台级商品目录（全局共享，不过滤 user_id）；用户资产在 holdings。
"""
from __future__ import annotations

import json
import random
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mock_bank import db
from utils.deps import current_uid
from utils.response_codes import ERR

router = APIRouter()


# --------------------------------------------------------------------------- #
#  风险适配（投资者适当性）强制校验 —— 与 docs/knowledge/wealth-products.md §四一致
# --------------------------------------------------------------------------- #
# 用户风险偏好（users.preferences.risk_preference）取值：稳健 / 平衡 / 进取；
# 映射为「可购买的最高产品风险档」（R1=1 / R2=2 / R3=3）。
# 适配规则：产品 risk_level <= 用户最高可购档 才允许；超风险认购拦截并提示降档。
# 未测评用户（risk_preference 为空）按最低风险承受处理（仅可购 R1）；购买 R2/R3 需先在
#   「个人中心」设置风险偏好。如需更严格（未测评一律拦截），把 UNSET_RISK_MAX_LEVEL 改为 0 即可。
RISK_PREF_TO_MAX_LEVEL = {"稳健": 1, "平衡": 2, "进取": 3}
UNSET_RISK_MAX_LEVEL = 1
_RISK_LEVEL_LABEL = {1: "R1（低风险）", 2: "R2（中风险）", 3: "R3（中高风险）"}


def _parse_prefs(raw) -> dict:
    """users.preferences 是 MySQL JSON 列，pymysql 以字符串返回；统一解析为 dict。"""
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return raw if isinstance(raw, dict) else {}


def _user_risk_max(risk_pref) -> int:
    return RISK_PREF_TO_MAX_LEVEL.get(risk_pref, UNSET_RISK_MAX_LEVEL)


def check_wealth_suitability(product_risk: int, risk_pref) -> tuple[bool, dict]:
    """适当性校验：返回 (ok, info)。info 含 product_risk / user_risk / user_max_level /
    allowed / reason（reason: ok | risk_mismatch | risk_unset）。"""
    user_max = _user_risk_max(risk_pref)
    if product_risk > user_max:
        return False, {
            "product_risk": product_risk,
            "user_risk": risk_pref if risk_pref in RISK_PREF_TO_MAX_LEVEL else None,
            "user_max_level": user_max,
            "allowed": False,
            "reason": "risk_mismatch" if risk_pref in RISK_PREF_TO_MAX_LEVEL else "risk_unset",
        }
    return True, {
        "product_risk": product_risk,
        "user_risk": risk_pref if risk_pref in RISK_PREF_TO_MAX_LEVEL else None,
        "user_max_level": user_max,
        "allowed": True,
        "reason": "ok",
    }


@router.get("/api/products")
def list_products(
    risk_level: int | None = Query(None, description="1/2/3"),
    max_amount: float | None = Query(None, description="按起投金额过滤"),
):
    """在售理财产品列表。

    ★ 铁律 2：products 是平台级商品目录，**全局共享**。
      这里刻意不接受 token、不按用户过滤 —— A 和 B 必须看到同一份商品清单（验收 #7）。
      用户的理财资产在 holdings 表：买产品 → 产生一条属于该用户的持仓记录。
    """
    from mock_bank.main import json_safe, fail  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）
    try:
        sql = "SELECT id, name, term, risk_level, annual_rate, min_amount FROM products"
        where, args = ["(status = 'on' OR status IS NULL)"], []   # C6：下架产品用户端不可见
        if risk_level is not None:
            where.append("risk_level = %s")
            args.append(risk_level)
        if max_amount is not None:
            where.append("min_amount <= %s")
            args.append(max_amount)
        sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY annual_rate DESC"
        rows = db.fetch_all(sql, tuple(args))
        return {"ok": True, "scope": "global", "count": len(rows), "products": json_safe(rows)}
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询理财产品失败：{exc}")




@router.get("/api/holdings")
def list_holdings(
    uid: int = Depends(current_uid),
    status: str | None = Query(None, description="holding / redeemed，默认全部"),
):
    """理财持仓列表：JOIN products 带出产品名/期限/风险/年化，附带在持总额。

    products 侧仍是全局商品，只是"谁买了多少"由 h.user_id 隔离。
    """
    from mock_bank.main import json_safe, fail  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）
    try:
        sql = (
            "SELECT h.id, h.user_id, h.product_id, h.amount, h.buy_date, h.status, "
            "p.name AS product_name, p.term, p.risk_level, p.annual_rate, p.min_amount "
            "FROM holdings h JOIN products p ON p.id = h.product_id "
            "WHERE h.user_id = %s"
        )
        args: list[Any] = [uid]
        if status:
            sql += " AND h.status = %s"
            args.append(status)
        sql += " ORDER BY h.status ASC, h.buy_date DESC"
        rows = db.fetch_all(sql, tuple(args))
        holding_rows = [r for r in rows if r["status"] == "holding"]
        total = sum((Decimal(str(r["amount"])) for r in holding_rows), Decimal("0"))
        return {
            "ok": True,
            "count": len(rows),
            "holding_count": len(holding_rows),
            "total_amount": float(total),
            "holdings": json_safe(rows),
        }
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询持仓失败：{exc}")




class WealthBuyReq(BaseModel):
    product_id: int
    amount: float = Field(default=0, gt=0)


@router.post("/api/products/buy")
def buy_product(req: WealthBuyReq, uid: int = Depends(current_uid)):
    """购买理财：校验产品存在、金额满足起投且余额充足，同事务内扣减余额 +
    写入 holdings 持仓并落操作日志。

    说明：买入与扣款在同一事务（holdings 立即可见、余额即时扣减）；
    扣款口径与 /api/transfer 一致：不指定账户时扣「当前用户名下 id 最小的账户」，
    SELECT ... FOR UPDATE 行锁防并发透支。
    ★ 持仓记在当前认证用户名下，不接 user_id 参数。
    """
    from mock_bank.main import to_decimal, fail  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）
    try:
        amount = to_decimal(req.amount)
    except ValueError as exc:
        return fail(str(exc), code=ERR.BAD_REQUEST, action="buy_product")
    if amount <= 0:
        return fail("申购金额必须大于 0", code=ERR.BAD_REQUEST, action="buy_product")

    serial_no = None
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                # products 是全局商品目录 —— 不做归属校验，只校验存在性
                cur.execute(
                    "SELECT id, name, risk_level, annual_rate, min_amount, status "
                    "FROM products WHERE id=%s",
                    (req.product_id,),
                )
                product = cur.fetchone()
                if not product:
                    db.log_operation(uid, "buy_product", json.dumps(req.model_dump()), "failed", cur=cur)
                    conn.commit()
                    return fail(f"理财产品 {req.product_id} 不存在",
                                code=ERR.PRODUCT_NOT_FOUND, action="buy_product")
                if product.get("status") == "off":   # C6：已下架 → 不可再买
                    db.log_operation(uid, "buy_product", json.dumps(req.model_dump()), "failed", cur=cur)
                    conn.commit()
                    return fail(f"理财产品「{product['name']}」已下架，无法购买；既有持仓不受影响",
                                code=ERR.PRODUCT_OFF, action="buy_product")
                if amount < Decimal(str(product["min_amount"])):
                    db.log_operation(uid, "buy_product", json.dumps(req.model_dump()), "failed", cur=cur)
                    conn.commit()
                    return fail(
                        f"金额不足起投：产品「{product['name']}」起投 "
                        f"{float(product['min_amount']):.2f} 元，本次 {float(amount):.2f} 元",
                        code=ERR.BELOW_MIN_AMOUNT, action="buy_product",
                    )
                # 适当性校验（强制，投资者适当性）：产品风险档不得高于用户可承受最高档；
                # 命中即在扣款前失败回滚（不写持仓 / 不扣余额）。权威入口，前端提示只作辅助。
                cur.execute("SELECT preferences FROM users WHERE id=%s", (uid,))
                _risk_pref = _parse_prefs((cur.fetchone() or {}).get("preferences")).get("risk_preference")
                _ok_suit, _suit_info = check_wealth_suitability(int(product["risk_level"]), _risk_pref)
                if not _ok_suit:
                    _usr_desc = (_suit_info["user_risk"]
                                 or "未设置风险偏好（暂按最低风险承受，仅可购 R1）")
                    _max_lbl = _RISK_LEVEL_LABEL.get(
                        _suit_info["user_max_level"], f"R{_suit_info['user_max_level']} 及以上")
                    db.log_operation(uid, "buy_product", json.dumps(req.model_dump()), "failed", cur=cur)
                    conn.commit()
                    _suit_code = (ERR.RISK_MISMATCH if _suit_info["reason"] == "risk_mismatch"
                                  else ERR.RISK_UNSET)
                    return fail(
                        f"风险不适配：产品「{product['name']}」为 "
                        f"{_RISK_LEVEL_LABEL.get(int(product['risk_level']), 'R'+str(product['risk_level']))}，"
                        f"需风险偏好「{_max_lbl}」及以上方可购买；当前您的设置为「{_usr_desc}」。"
                        f"请更换低风险产品，或在「个人中心」设置风险偏好后再试。",
                        code=_suit_code, action="buy_product", suitability=_suit_info,
                    )
                # 余额校验 + 扣款（行锁；口径同转账：用户名下 id 最小的账户）
                cur.execute(
                    "SELECT id, balance FROM accounts WHERE user_id=%s "
                    "AND COALESCE(account_type,'debit') <> 'credit' "
                    "ORDER BY id ASC LIMIT 1 FOR UPDATE",
                    (uid,),
                )
                acc = cur.fetchone()
                balance = Decimal(str(acc["balance"])) if acc else Decimal("0")
                if not acc or balance < amount:
                    db.log_operation(uid, "buy_product", json.dumps(req.model_dump()), "failed", cur=cur)
                    conn.commit()
                    return fail(
                        f"余额不足：账户余额 {float(balance):.2f} 元，"
                        f"本次申购 {float(amount):.2f} 元，"
                        f"差额 {float(amount - balance):.2f} 元",
                        code=ERR.BUY_INSUFFICIENT, action="buy_product",
                    )
                cur.execute(
                    "UPDATE accounts SET balance = balance - %s WHERE id=%s",
                    (str(amount), acc["id"]),
                )
                serial_no = "WM" + datetime.now().strftime("%Y%m%d%H%M%S") + f"{random.randint(1000, 9999)}"
                # 落持仓：同产品可持有多笔，赎回时按 buy_date 最早的先赎
                cur.execute(
                    "INSERT INTO holdings (user_id, product_id, amount, buy_date, status) "
                    "VALUES (%s, %s, %s, CURDATE(), 'holding')",
                    (uid, req.product_id, str(amount)),
                )
                holding_id = int(cur.lastrowid)
                db.log_operation(
                    uid, "buy_product",
                    {"product_id": req.product_id, "amount": float(amount),
                     "serial_no": serial_no, "balance_after": float(balance - amount)},
                    "success", cur=cur,
                )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "buy_product", json.dumps(req.model_dump()), "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"购买失败：{exc}", code=ERR.BUY_FAILED, action="buy_product")

    return {
        "ok": True,
        "serial_no": serial_no,
        "holding_id": holding_id,
        "product": product["name"],
        "risk_level": product["risk_level"],
        "amount": float(amount),
        "balance_after": float(balance - amount),
        "suitability": _suit_info,
        "message": (f"已申购「{product['name']}」{float(amount):.2f} 元，"
                    f"扣款成功，余额 {float(balance - amount):.2f} 元，流水号 {serial_no}"),
    }




class RedeemReq(BaseModel):
    holding_id: int
    amount: float = Field(default=0, gt=0)


@router.post("/api/redeem")
def redeem(req: RedeemReq, uid: int = Depends(current_uid)):
    """赎回持仓：归属校验 → 金额校验 → 减持仓 / 置已赎回 → 留痕。"""
    from mock_bank.main import to_decimal, fail  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）
    try:
        amount = to_decimal(req.amount)
    except ValueError as exc:
        return fail(str(exc), code=ERR.BAD_REQUEST, action="redeem")
    if amount <= 0:
        return fail("赎回金额必须大于 0", code=ERR.BAD_REQUEST, action="redeem")

    serial_no = "RD" + datetime.now().strftime("%Y%m%d%H%M%S") + f"{random.randint(1000, 9999)}"
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT h.id, h.amount, h.status, p.name AS product_name "
                    "FROM holdings h JOIN products p ON p.id = h.product_id "
                    "WHERE h.id=%s AND h.user_id=%s",
                    (req.holding_id, uid),
                )
                holding = cur.fetchone()
                if not holding:
                    # 归属校验失败与不存在统一按 403，不泄露"该持仓是否存在"
                    db.log_operation(uid, "redeem", {"holding_id": req.holding_id}, "failed", cur=cur)
                    conn.commit()
                    raise HTTPException(status_code=403, detail="无权操作他人资源")
                if holding["status"] == "redeemed":
                    db.log_operation(uid, "redeem", {"holding_id": req.holding_id}, "failed", cur=cur)
                    conn.commit()
                    return fail(f"持仓（{holding['product_name']}）已赎回，无需重复操作",
                                code=ERR.REDEEM_ALREADY, action="redeem")

                held = Decimal(str(holding["amount"]))
                if amount > held:
                    db.log_operation(
                        uid, "redeem",
                        {"holding_id": req.holding_id, "amount": float(amount)}, "failed", cur=cur,
                    )
                    conn.commit()
                    return fail(
                        f"赎回金额超出持仓：{holding['product_name']} 持有 "
                        f"{float(held):.2f} 元，本次赎回 {float(amount):.2f} 元",
                        code=ERR.REDEEM_EXCEED, action="redeem",
                    )
                if amount >= held:
                    cur.execute("UPDATE holdings SET status='redeemed' WHERE id=%s", (req.holding_id,))
                else:
                    cur.execute(
                        "UPDATE holdings SET amount = amount - %s WHERE id=%s",
                        (str(amount), req.holding_id),
                    )
                db.log_operation(
                    uid, "redeem",
                    {"holding_id": req.holding_id, "amount": float(amount), "serial_no": serial_no},
                    "success", cur=cur,
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "redeem", {"holding_id": req.holding_id}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"赎回失败：{exc}", code=ERR.REDEEM_FAILED, action="redeem")

    return {
        "ok": True,
        "serial_no": serial_no,
        "product": holding["product_name"],
        "amount": float(amount),
        "message": f"已赎回「{holding['product_name']}」{float(amount):.2f} 元，流水号 {serial_no}",
    }
