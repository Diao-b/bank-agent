"""utils/subscription.py —— 订阅域（detect + deduct + 状态机 confirm/cancel/ignore）。

原 main.py 订阅路由整段抽离而来（组 S 合并式重构，行为零变化）。
装饰器挂在 `router = APIRouter()` 上，由 mock_bank.main 在模块末尾 include_router。
共享 helper（owned / json_safe / to_decimal / fail / current_uid / VALID_CYCLES /
add_months）定义在 mock_bank.main，include 发生在 main 模块加载末尾、各 helper 均已
定义之后，故此处顶部 import 不触发循环导入崩溃。
"""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field

from mock_bank import db
from utils.settlement_engine import run_due_deductions
from utils.response_codes import ERR
from mock_bank.main import (
    owned,
    json_safe,
    to_decimal,
    fail,
    current_uid,
    add_months,
)

# 业务常量单源见 rules.py（组 W）：订阅识别阈值 + 计费周期枚举
# （不再从 mock_bank.main 取 VALID_CYCLES，解除 main↔subscription 的常量耦合）。
from rules import (  # noqa: E402
    VALID_CYCLES,
    SCAN_MONTHS,
    MIN_TXNS,
    MONTHLY_MIN,
    MONTHLY_MAX,
    YEARLY_MIN,
    YEARLY_MAX,
    AMOUNT_STD_RATIO,
    EXCLUDED_CATEGORIES,
)


# =========================================================================== #
#  订阅流水识别引擎（detect）—— 原 utils/subscription_detector.py 并入（行为零变化）
#  从近 N 个月流水里挖出「疑似周期性订阅扣款」的商户，供用户确认后转正。
#  ★ 阈值常量（SCAN_MONTHS / MIN_TXNS / MONTHLY_* / YEARLY_* / AMOUNT_STD_RATIO /
#    EXCLUDED_CATEGORIES）已收口到 rules.py（组 W），见上方 import。
# =========================================================================== #
# 已存在订阅的商户不重复发现（cancelled 除外，可重新发现）
_EXISTING_SQL = (
    "SELECT DISTINCT merchant FROM subscriptions "
    "WHERE user_id=%s AND merchant IS NOT NULL "
    "AND status IN ('active', 'pending', 'ignored')"
)

_TXN_SQL = (
    "SELECT t.`date`, t.amount, t.merchant, t.category "
    "FROM transactions t JOIN accounts a ON a.id = t.account_id "
    "WHERE a.user_id=%s AND t.type='expense' "
    "AND t.`date` >= DATE_SUB(CURDATE(), INTERVAL %s MONTH) "
    "ORDER BY t.merchant, t.`date` ASC"
)


def _estimate_next_due(last_date: date, interval_days: int, cycle: str) -> date:
    """估算下次扣款日：末笔 + 中位间隔；已经过了就再推一个周期。"""
    nxt = last_date + timedelta(days=interval_days)
    if nxt <= date.today():
        nxt = nxt + timedelta(days=365 if cycle == "年付" else 30)
    return nxt


def _classify(group: list[dict]) -> dict | None:
    """单个商户的流水组 → 候选 dict；不满足周期性/一致性返回 None。"""
    dates = [r["date"] for r in group]
    amounts = [Decimal(str(r["amount"])) for r in group]
    n = len(dates)
    if n < MIN_TXNS:
        return None

    # ---- 周期性：相邻间隔必须全部落在同一窗口内 ----
    intervals = [(dates[i] - dates[i - 1]).days for i in range(1, n)]
    if not intervals:
        return None
    if all(MONTHLY_MIN <= x <= MONTHLY_MAX for x in intervals):
        cycle = "月付"
    elif all(YEARLY_MIN <= x <= YEARLY_MAX for x in intervals):
        cycle = "年付"
    else:
        return None

    # ---- 金额一致性：std <= mean * 5% ----
    float_amounts = [float(a) for a in amounts]
    mean = statistics.fmean(float_amounts)
    std = statistics.pstdev(float_amounts)
    if mean <= 0 or std > mean * AMOUNT_STD_RATIO:
        return None

    interval_median = int(statistics.median(intervals))
    return {
        "merchant": group[0]["merchant"],
        "cycle": cycle,
        "count": n,
        "avg_amount": round(mean, 2),
        "amount_std": round(std, 2),
        "first_date": dates[0],
        "last_date": dates[-1],
        "interval_days": interval_median,
        # 置信度：笔数越多、金额越稳 → 越高（仅排序展示用，不下结论）
        "confidence": min(0.99, 0.55 + 0.08 * n + (0.1 if std == 0 else 0.0)),
        "estimated_next_due": _estimate_next_due(dates[-1], interval_median, cycle),
    }


def discover_candidates(user_id: int, months: int = SCAN_MONTHS) -> list[dict]:
    """扫描当前用户近 months 个月流水，返回疑似订阅扣款的候选列表。

    ★ 只查当前用户的账户（JOIN accounts 收敛 user_id）—— 多用户隔离铁律。
    """
    rows = db.fetch_all(_TXN_SQL, (user_id, months))
    existing = {r["merchant"] for r in db.fetch_all(_EXISTING_SQL, (user_id,))}

    groups: dict[str, list[dict]] = {}
    for r in rows:
        if r["category"] in EXCLUDED_CATEGORIES:
            continue
        groups.setdefault(r["merchant"], []).append(r)

    candidates: list[dict] = []
    for merchant, group in groups.items():
        if merchant in existing:
            continue
        hit = _classify(group)
        if hit:
            candidates.append(hit)

    candidates.sort(key=lambda c: (-c["count"], c["merchant"]))
    # 日期统一转 iso（路由层 json_safe 也兜底，这里先转省心）
    for c in candidates:
        c["first_date"] = c["first_date"].isoformat()
        c["last_date"] = c["last_date"].isoformat()
        c["estimated_next_due"] = c["estimated_next_due"].isoformat()
        c["confidence"] = round(c["confidence"], 2)
    return candidates


# =========================================================================== #
#  订阅路由（状态机：contract / confirm / cancel / ignore / update / delete）
# =========================================================================== #
router = APIRouter()


@router.get("/api/subscriptions")
def list_subscriptions(
    uid: int = Depends(current_uid),
    status: str | None = Query(None, description="active / cancelled / ignored，默认全部"),
):
    """订阅/会员列表，附带闲置天数、idle_over_60 与月度折算费用。

    v2 语义：
      * 惰性扣款：进本接口先跑一次扣款引擎 —— 到期未扣的订阅自动补扣、
        顺延 next_due，前端看到的永远是"扣完之后"的最新状态；
      * source 徽标：contracted=签约代扣（蓝色）/ identified=流水识别（紫色）；
      * 软删除：取消后的记录保留（status=cancelled），前端渲染删除线，
        不计入月度总支出与闲置聚合。年付按 /12 折算为月度成本。
    """
    try:
        # ---- 惰性触发：到期订阅先补扣（自己的事务，失败不影响列表） ----
        try:
            run_due_deductions(uid)
        except Exception:  # noqa: BLE001 - 扣款失败不阻断列表查询
            pass

        sql = (
            "SELECT id, user_id, service_name, fee, cycle, last_used, status, "
            "cancelled_at, source, merchant, next_due, confirmed "
            "FROM subscriptions WHERE user_id=%s"
        )
        args: list[Any] = [uid]
        if status:
            sql += " AND status=%s"
            args.append(status)
        sql += " ORDER BY fee DESC"
        rows = db.fetch_all(sql, tuple(args))

        today = date.today()
        result = []
        monthly_total = Decimal("0")
        idle_items: list[dict] = []
        for r in rows:
            last_used = r["last_used"]
            idle_days = (today - last_used).days if isinstance(last_used, date) else None
            item = json_safe(r)
            item["idle_days"] = idle_days
            item["idle_over_60"] = bool(idle_days is not None and idle_days > 60)
            item["due_days"] = (
                (r["next_due"] - today).days
                if isinstance(r.get("next_due"), date) else None
            )
            result.append(item)
            if r["status"] != "active":
                continue
            fee = Decimal(str(r["fee"]))
            if r["cycle"] == "月付":
                monthly_total += fee
            elif r["cycle"] == "年付":
                monthly_total += fee / Decimal("12")
            if item["idle_over_60"]:
                idle_items.append(item)
        return {
            "ok": True,
            "count": len(result),
            "active_count": sum(1 for x in result if x["status"] == "active"),
            "pending_count": sum(1 for x in result if x["status"] == "pending"),
            "monthly_total": round(float(monthly_total), 2),
            "idle": {
                "count": len(idle_items),
                "monthly_waste": round(
                    float(sum(Decimal(str(x["fee"])) if x["cycle"] == "月付"
                              else Decimal(str(x["fee"])) / Decimal("12")
                              for x in idle_items)), 2),
                "items": idle_items,
            },
            "subscriptions": result,
        }
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询订阅失败：{exc}")

#  订阅 CRUD
# =========================================================================== #
class SubscriptionContractReq(BaseModel):
    service_name: str = Field(min_length=1, max_length=64)
    fee: float = Field(gt=0)
    cycle: str = Field(default="月付", description="月付 / 年付")
    merchant: str | None = Field(default=None, max_length=100,
                                 description="扣款商户名，默认同服务名")
    account_id: int | None = Field(default=None, description="签约扣款账户，默认第一个账户")
    first_due: str | None = Field(default=None,
                                  description="首次扣款日 YYYY-MM-DD，默认下月同日")


class SubscriptionUpdateReq(BaseModel):
    fee: float | None = Field(default=None, gt=0)
    cycle: str | None = Field(default=None, description="月付 / 年付")


@router.post("/api/subscriptions/contract")
def contract_subscription(req: SubscriptionContractReq, uid: int = Depends(current_uid)):
    """签约代扣：用户在银行签约一项自动扣费服务。

    真实业务里"添加订阅"就是这个动作 —— 签了约，银行才会在 next_due 到期时
    自动扣款。与旧的"手动添加"（source='manual'，纯记账、永不扣款）语义完全
    不同，v2 已移除手动添加。

    一个事务里做四件事（同生共死）：
      1. 补录 2~3 笔历史流水（近几个周期、同金额、remark='签约代扣·历史补录'）
         —— 用户此前在别处扣的费，签约后账单才完整。补录不动余额：
         那些钱本来就不在这家银行；
      2. 写订阅：source='contracted' / confirmed=1 / next_due=首次扣款日；
      3. 留痕 operation_logs；
      4. 幂等守卫：同商户同金额同日期的流水已存在就不重复补录。
    """
    service_name = req.service_name.strip()
    if not service_name:
        return fail("服务名称不能为空", code=ERR.SUB_NAME_EMPTY, action="subscription_contract")
    if req.cycle not in VALID_CYCLES:
        return fail(f"计费周期非法：{req.cycle}（可选 {VALID_CYCLES}）",
                    code=ERR.SUB_CYCLE_INVALID, action="subscription_contract")
    try:
        fee = to_decimal(req.fee)
    except ValueError as exc:
        return fail(str(exc), code=ERR.SUB_AMOUNT_INVALID, action="subscription_contract")
    if fee <= 0:
        return fail("订阅费用必须大于 0", code=ERR.SUB_AMOUNT_INVALID, action="subscription_contract")

    merchant = (req.merchant or service_name).strip()
    today = date.today()
    if req.first_due:
        try:
            first_due = date.fromisoformat(req.first_due)
        except ValueError:
            return fail(f"首次扣款日格式非法：{req.first_due}（应为 YYYY-MM-DD）",
                        code=ERR.SUB_FIRST_DUE_INVALID, action="subscription_contract")
        if first_due <= today:
            return fail(f"首次扣款日必须晚于今天（收到 {first_due}）",
                        code=ERR.SUB_FIRST_DUE_INVALID, action="subscription_contract")
    else:
        first_due = add_months(today, 1)

    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                # ---- 签约账户：指定则校验归属，否则取名下第一个账户 ----
                if req.account_id is not None:
                    account = owned(cur, "accounts", req.account_id, uid, for_update=True)
                else:
                    cur.execute(
                        "SELECT id, name, balance FROM accounts "
                        "WHERE user_id=%s AND COALESCE(account_type,'debit') <> 'credit' "
                        "ORDER BY id ASC LIMIT 1 FOR UPDATE",
                        (uid,),
                    )
                    account = cur.fetchone()
                    if not account:
                        conn.rollback()
                        return fail("您名下没有可用账户，无法签约",
                                    code=ERR.SUB_NO_ACCOUNT, action="subscription_contract")

                # ---- 防重复签约：同商户已有生效订阅 → 拒绝 ----
                cur.execute(
                    "SELECT id, service_name FROM subscriptions "
                    "WHERE user_id=%s AND merchant=%s AND status IN ('active','pending')",
                    (uid, merchant),
                )
                dup = cur.fetchone()
                if dup:
                    conn.rollback()
                    return fail(f"商户「{merchant}」已有生效订阅「{dup['service_name']}」，请勿重复签约",
                                code=ERR.SUB_DUPLICATE, action="subscription_contract")

                # ---- 1) 补录 2~3 笔历史流水（幂等：同日同额已存在则跳过） ----
                backfilled = 0
                for k in (1, 2, 3):
                    if req.cycle == "年付":
                        try:
                            d = first_due.replace(year=first_due.year - k)
                        except ValueError:
                            d = first_due.replace(year=first_due.year - k, day=28)
                    else:
                        d = add_months(first_due, -k)
                    if d > today:
                        continue
                    cur.execute(
                        "SELECT 1 FROM transactions WHERE account_id=%s AND merchant=%s "
                        "AND amount=%s AND `date`=%s AND type='expense' LIMIT 1",
                        (account["id"], merchant, fee, d),
                    )
                    if cur.fetchone():
                        continue
                    cur.execute(
                        "INSERT INTO transactions (account_id, `date`, amount, category, "
                        "merchant, type, remark) VALUES (%s, %s, %s, '订阅', %s, 'expense', "
                        "'签约代扣·历史补录')",
                        (account["id"], d, fee, merchant),
                    )
                    backfilled += 1

                # ---- 2) 写订阅：source='contracted'，到期由扣款引擎接管 ----
                cur.execute(
                    "INSERT INTO subscriptions (user_id, service_name, fee, cycle, source, "
                    "merchant, next_due, confirmed, last_used, status) "
                    "VALUES (%s, %s, %s, %s, 'contracted', %s, %s, 1, CURDATE(), 'active')",
                    (uid, service_name, fee, req.cycle, merchant, first_due),
                )
                sub_id = int(cur.lastrowid)

                # ---- 3) 留痕（与业务同一事务） ----
                db.log_operation(
                    uid, "subscription_contract",
                    {"sub_id": sub_id, "service_name": service_name, "merchant": merchant,
                     "fee": float(fee), "cycle": req.cycle,
                     "account_id": account["id"], "next_due": first_due.isoformat(),
                     "backfilled": backfilled},
                    "success", cur=cur,
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "subscription_contract",
                                 {"service_name": service_name}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"签约代扣失败：{exc}", code=ERR.CONTRACT_FAILED,
                        action="subscription_contract")

    return {"ok": True, "sub_id": sub_id, "service_name": service_name,
            "merchant": merchant, "fee": float(fee), "cycle": req.cycle,
            "source": "contracted", "account_id": account["id"],
            "next_due": first_due.isoformat(), "backfilled": backfilled,
            "message": (f"已签约代扣「{service_name}」，首次扣款日 {first_due.isoformat()}，"
                        f"补录历史流水 {backfilled} 笔")}


@router.get("/api/subscriptions/discover")
def discover_subscriptions(uid: int = Depends(current_uid)):
    """订阅发现：扫描近 6 个月流水，挖出"疑似周期性扣款但还没登记"的商户。

    判定规则见本模块「订阅流水识别引擎（detect）」段（≥3 笔 + 间隔 26~35/350~380 天
    + 金额 std ≤ 均值 5%）。只发现、不自动入库 —— 用户确认后才转正。

    ★ 也是惰性扣款入口之一：先跑扣款引擎，保证返回的流水数据是最新的。
    """
    try:
        try:
            run_due_deductions(uid)
        except Exception:  # noqa: BLE001
            pass
        candidates = discover_candidates(uid)
        return {"ok": True, "count": len(candidates), "candidates": candidates}
    except Exception as exc:  # noqa: BLE001
        return fail(f"订阅发现失败：{exc}")


class DiscoverTargetReq(BaseModel):
    merchant: str = Field(min_length=1, max_length=128)


@router.post("/api/subscriptions/discover/confirm")
def confirm_discovered(req: DiscoverTargetReq, uid: int = Depends(current_uid)):
    """确认一个识别候选 → 转正为 active 订阅（confirmed=1）。

    ★ 金额/周期/next_due 全部以服务端重新识别的结果为准，
      不信任前端传值（防止把"每月给房东转 5000"录成 5 块的订阅）。
    """
    merchant = req.merchant.strip()
    try:
        candidates = discover_candidates(uid)
        hit = next((c for c in candidates if c["merchant"] == merchant), None)
        if not hit:
            return fail(f"「{merchant}」当前不是待确认的订阅候选（可能已登记或不再满足周期性）",
                        code=ERR.SUB_NOT_CANDIDATE, action="subscription_confirm")

        with db.get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM subscriptions WHERE user_id=%s AND merchant=%s "
                        "AND status IN ('active','pending') LIMIT 1",
                        (uid, merchant),
                    )
                    if cur.fetchone():
                        conn.rollback()
                        return fail(f"商户「{merchant}」已有生效订阅，无需重复确认",
                                    code=ERR.SUB_DUPLICATE, action="subscription_confirm")
                    cur.execute(
                        "INSERT INTO subscriptions (user_id, service_name, fee, cycle, source, "
                        "merchant, next_due, confirmed, last_used, status) "
                        "VALUES (%s, %s, %s, %s, 'identified', %s, %s, 1, CURDATE(), 'active')",
                        (uid, merchant, Decimal(str(hit["avg_amount"])), hit["cycle"],
                         merchant, hit["estimated_next_due"]),
                    )
                    sub_id = int(cur.lastrowid)
                    db.log_operation(
                        uid, "subscription_confirm",
                        {"sub_id": sub_id, "merchant": merchant,
                         "fee": hit["avg_amount"], "cycle": hit["cycle"],
                         "next_due": hit["estimated_next_due"], "count": hit["count"]},
                        "success", cur=cur,
                    )
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                try:
                    db.log_operation(uid, "subscription_confirm",
                                     {"merchant": merchant}, "failed")
                except Exception:  # noqa: BLE001
                    pass
                return fail(f"确认订阅失败：{exc}", code=ERR.CONFIRM_FAILED,
                            action="subscription_confirm")
        return {"ok": True, "sub_id": sub_id, "service_name": merchant,
                "fee": hit["avg_amount"], "cycle": hit["cycle"],
                "source": "identified", "next_due": hit["estimated_next_due"],
                "message": f"已确认订阅「{merchant}」（{hit['cycle']} ¥{hit['avg_amount']}）"}
    except Exception as exc:  # noqa: BLE001
        return fail(f"确认订阅失败：{exc}", code=ERR.CONFIRM_FAILED,
                    action="subscription_confirm")


@router.post("/api/subscriptions/discover/ignore")
def ignore_discovered(req: DiscoverTargetReq, uid: int = Depends(current_uid)):
    """忽略一个识别候选：落一条 status='ignored' 的记录，之后 discover 不再推荐。

    （不落库的话下次扫描还会冒出来 —— "每月给房东转账"这种就会被用户反复忽略。）
    """
    merchant = req.merchant.strip()
    try:
        candidates = discover_candidates(uid)
        hit = next((c for c in candidates if c["merchant"] == merchant), None)
        with db.get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, status FROM subscriptions "
                        "WHERE user_id=%s AND merchant=%s LIMIT 1 FOR UPDATE",
                        (uid, merchant),
                    )
                    row = cur.fetchone()
                    if row:
                        if row["status"] == "ignored":
                            conn.rollback()
                            return fail(f"「{merchant}」已忽略过，无需重复操作",
                                        code=ERR.SUB_ALREADY_IGNORED, action="subscription_ignore")
                        cur.execute(
                            "UPDATE subscriptions SET status='ignored' WHERE id=%s",
                            (row["id"],),
                        )
                        sub_id = int(row["id"])
                    else:
                        if not hit:
                            conn.rollback()
                            return fail(f"「{merchant}」当前不是订阅候选，无法忽略",
                                        code=ERR.SUB_NOT_CANDIDATE, action="subscription_ignore")
                        cur.execute(
                            "INSERT INTO subscriptions (user_id, service_name, fee, cycle, "
                            "source, merchant, next_due, confirmed, last_used, status) "
                            "VALUES (%s, %s, %s, %s, 'identified', %s, NULL, 0, CURDATE(), 'ignored')",
                            (uid, merchant, Decimal(str(hit["avg_amount"])), hit["cycle"], merchant),
                        )
                        sub_id = int(cur.lastrowid)
                    db.log_operation(
                        uid, "subscription_ignore",
                        {"sub_id": sub_id, "merchant": merchant}, "success", cur=cur,
                    )
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                try:
                    db.log_operation(uid, "subscription_ignore",
                                     {"merchant": merchant}, "failed")
                except Exception:  # noqa: BLE001
                    pass
                return fail(f"忽略订阅失败：{exc}", code=ERR.IGNORE_FAILED,
                            action="subscription_ignore")
        return {"ok": True, "sub_id": sub_id, "merchant": merchant,
                "message": f"已忽略「{merchant}」，后续发现不再推荐"}
    except Exception as exc:  # noqa: BLE001
        return fail(f"忽略订阅失败：{exc}", code=ERR.IGNORE_FAILED,
                    action="subscription_ignore")


@router.put("/api/subscriptions/{sub_id}")
def update_subscription(sub_id: int, req: SubscriptionUpdateReq, uid: int = Depends(current_uid)):
    """修改订阅的费用 / 计费周期：归属校验 → 校验 → 更新 → 留痕。"""
    if req.fee is None and req.cycle is None:
        return fail("没有需要修改的字段（fee / cycle 至少传一个）",
                    code=ERR.SUB_NO_FIELDS, action="update_subscription")
    if req.cycle is not None and req.cycle not in VALID_CYCLES:
        return fail(f"计费周期非法：{req.cycle}（可选 {VALID_CYCLES}）",
                    code=ERR.SUB_CYCLE_INVALID, action="update_subscription")

    fee: Decimal | None = None
    if req.fee is not None:
        try:
            fee = to_decimal(req.fee)
        except ValueError as exc:
            return fail(str(exc), code=ERR.SUB_AMOUNT_INVALID, action="update_subscription")
        if fee <= 0:
            return fail("订阅费用必须大于 0", code=ERR.SUB_AMOUNT_INVALID,
                        action="update_subscription")

    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                sub = owned(cur, "subscriptions", sub_id, uid, for_update=True)
                if sub["status"] == "cancelled":
                    conn.rollback()
                    return fail(f"订阅「{sub['service_name']}」已取消，无法修改",
                                code=ERR.SUB_CANCELLED, action="update_subscription")

                sets, args = [], []
                if fee is not None:
                    sets.append("fee=%s")
                    args.append(fee)
                if req.cycle is not None:
                    sets.append("cycle=%s")
                    args.append(req.cycle)
                args.append(sub_id)
                cur.execute(
                    "UPDATE subscriptions SET " + ", ".join(sets) + " WHERE id=%s", tuple(args)
                )
                db.log_operation(
                    uid, "subscription_update",
                    {"sub_id": sub_id, "service_name": sub["service_name"],
                     "fee": float(fee) if fee is not None else None, "cycle": req.cycle},
                    "success", cur=cur,
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "subscription_update", {"sub_id": sub_id}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"修改订阅失败：{exc}", code=ERR.UPDATE_FAILED,
                        action="update_subscription")

    return {"ok": True, "sub_id": sub_id, "service_name": sub["service_name"],
            "fee": float(fee) if fee is not None else float(sub["fee"]),
            "cycle": req.cycle or sub["cycle"],
            "message": f"订阅「{sub['service_name']}」已更新"}


@router.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: int, uid: int = Depends(current_uid)):
    """★ 彻底移除订阅（物理删除），与 cancel（软删除）语义不同，接口分开：

        cancel  POST /api/subscriptions/{id}/cancel
            取消续订 —— 记录保留、status='cancelled'，前端渲染删除线，
            历史账单仍可对账。真实业务里"取消订阅"就是这个语义。
        delete  DELETE /api/subscriptions/{id}
            彻底移除 —— 物理删除，不留痕迹。用于"用户录错了 / 重复录入"
            这类本来就不该存在的记录，而不是用于停掉一项真实订阅。
    """
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                sub = owned(cur, "subscriptions", sub_id, uid)
                cur.execute("DELETE FROM subscriptions WHERE id=%s AND user_id=%s", (sub_id, uid))
                db.log_operation(
                    uid, "subscription_delete",
                    {"sub_id": sub_id, "service_name": sub["service_name"],
                     "fee": float(sub["fee"]), "cycle": sub["cycle"]},
                    "success", cur=cur,
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "subscription_delete", {"sub_id": sub_id}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"删除订阅失败：{exc}", code=ERR.DELETE_FAILED,
                        action="delete_subscription")

    return {"ok": True, "sub_id": sub_id, "service_name": sub["service_name"],
            "message": f"已彻底移除订阅「{sub['service_name']}」"}


@router.post("/api/subscriptions/{sub_id}/cancel")
def cancel_subscription(sub_id: int, uid: int = Depends(current_uid)):
    """取消订阅：软删除（status 置 cancelled），记录保留供前端展示删除线，落操作日志。"""
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                sub = owned(cur, "subscriptions", sub_id, uid)
                if sub["status"] == "cancelled":
                    conn.rollback()
                    return fail(f"订阅「{sub['service_name']}」已取消，无需重复操作",
                                code=ERR.SUB_CANCELLED, action="cancel_subscription")
                cur.execute(
                    "UPDATE subscriptions SET status='cancelled', cancelled_at=NOW() "
                    "WHERE id=%s AND user_id=%s",
                    (sub_id, uid),
                )
                db.log_operation(
                    uid, "cancel_subscription",
                    {"sub_id": sub_id, "service_name": sub["service_name"]}, "success", cur=cur,
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "cancel_subscription", {"sub_id": sub_id}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"取消订阅失败：{exc}", code=ERR.CANCEL_FAILED,
                        action="cancel_subscription")
    return {"ok": True, "sub_id": sub_id, "message": f"已取消订阅「{sub['service_name']}」"}


@router.post("/api/subscriptions/{sub_id}/confirm")
def confirm_subscription(sub_id: int, uid: int = Depends(current_uid)):
    """确认订阅：pending → active，identified 未确认 → confirmed=1。

    取消（cancelled）/ 已忽略（ignored）的订阅不能走确认 —— 那是"复活"，
    应该重新签约或重新发现。
    """
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                sub = owned(cur, "subscriptions", sub_id, uid, for_update=True)
                if sub["status"] in ("cancelled", "ignored"):
                    conn.rollback()
                    return fail(f"订阅「{sub['service_name']}」当前状态为 {sub['status']}，不能确认",
                                code=ERR.SUB_CANCELLED, action="confirm_subscription")
                if sub["status"] == "active" and int(sub.get("confirmed") or 0) == 1:
                    conn.rollback()
                    return fail(f"订阅「{sub['service_name']}」已是确认状态，无需重复操作",
                                code=ERR.SUB_ALREADY_ACTIVE, action="confirm_subscription")
                cur.execute(
                    "UPDATE subscriptions SET status='active', confirmed=1 WHERE id=%s",
                    (sub_id,),
                )
                db.log_operation(
                    uid, "subscription_confirm",
                    {"sub_id": sub_id, "service_name": sub["service_name"],
                     "merchant": sub.get("merchant"), "via": "sub_id"},
                    "success", cur=cur,
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "subscription_confirm", {"sub_id": sub_id}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"确认订阅失败：{exc}", code=ERR.CONFIRM_FAILED,
                        action="confirm_subscription")
    return {"ok": True, "sub_id": sub_id,
            "message": f"已确认订阅「{sub['service_name']}」，将按周期自动扣款"}


@router.post("/api/subscriptions/{sub_id}/ignore")
def ignore_subscription(sub_id: int, uid: int = Depends(current_uid)):
    """忽略订阅：status='ignored'。与 cancel 语义不同 ——
    cancel 是"我退订了这项服务"（历史保留、可查账），
    ignore 是"这不是订阅 / 别再给我推荐"（识别引擎的降噪手段）。"""
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                sub = owned(cur, "subscriptions", sub_id, uid, for_update=True)
                if sub["status"] == "ignored":
                    conn.rollback()
                    return fail(f"订阅「{sub['service_name']}」已忽略，无需重复操作",
                                code=ERR.SUB_ALREADY_IGNORED, action="ignore_subscription")
                cur.execute(
                    "UPDATE subscriptions SET status='ignored' WHERE id=%s",
                    (sub_id,),
                )
                db.log_operation(
                    uid, "subscription_ignore",
                    {"sub_id": sub_id, "service_name": sub["service_name"],
                     "merchant": sub.get("merchant"), "via": "sub_id"},
                    "success", cur=cur,
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "subscription_ignore", {"sub_id": sub_id}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"忽略订阅失败：{exc}", code=ERR.IGNORE_FAILED,
                        action="ignore_subscription")
    return {"ok": True, "sub_id": sub_id,
            "message": f"已忽略「{sub['service_name']}」，不再计入订阅支出"}
