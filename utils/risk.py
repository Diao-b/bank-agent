"""risk.py —— 支付风控监控（模块 A）核心引擎

双通道：
  通道 A（用户侧实时提醒）：user_facing=1 的规则命中 → 返回 user_facing 信号，
           由调用方（transfer / 充值）就地提醒用户本人。
  通道 B（后台告警队列）：所有命中 → risk_events（分级 + 最小披露）；
           展开完整明细需填理由并写 risk_event_views 审计。

★ 隐私铁律：只读取元数据（收款人ID/金额/时间/频率/余额）；
  严禁读取 chat_messages、transactions.remark 用于判定。
★ D1 修复：R-DUP / R-NEWLARGE / R-SCATTER 用结构化 payee_id 匹配（transactions.payee_id）。
★ D2 修复：所有统计统一过滤 settle_status IS NULL（排除跨行在途/退票）。
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

from mock_bank import db


# --------------------------------------------------------------------------- #
#  展示脱敏
# --------------------------------------------------------------------------- #
def mask_uid(uid: int) -> str:
    s = str(uid)
    if len(s) <= 3:
        return "u***" + s[-1]
    return "u***" + s[-3:]


def amount_bucket(amount: Decimal | float) -> str:
    a = float(amount)
    if a < 1000:
        return "0-1k"
    if a < 5000:
        return "1k-5k"
    if a < 20000:
        return "5k-20k"
    if a < 50000:
        return "20k-50k"
    return "50k+"


# --------------------------------------------------------------------------- #
#  规则配置（risk_rules 表）
# --------------------------------------------------------------------------- #
def get_rule_cfg(code: str) -> dict | None:
    return db.fetch_one("SELECT * FROM risk_rules WHERE rule_code=%s", (code,))


def list_rules() -> list[dict]:
    return [dict(r) for r in db.fetch_all("SELECT * FROM risk_rules ORDER BY rule_code")]


def _parse_threshold(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}


def update_rule(code: str, enabled: bool, threshold: dict, updated_by: int) -> dict:
    cfg = get_rule_cfg(code)
    if not cfg:
        raise KeyError(f"未知风控规则：{code}")
    old = {"enabled": bool(cfg["enabled"]),
           "threshold": _parse_threshold(cfg["threshold"])}
    db.execute(
        "UPDATE risk_rules SET enabled=%s, threshold=%s WHERE rule_code=%s",
        (1 if enabled else 0, json.dumps(threshold, ensure_ascii=False), code))
    _audit(updated_by, "risk_rule_update", "risk_rule", code,
           detail={"old": old, "new": {"enabled": enabled, "threshold": threshold}})
    return {"ok": True, "rule_code": code}


def _audit(admin_id: int, action: str, target_type: str, target_id: str,
           detail: dict) -> None:
    db.execute(
        "INSERT INTO admin_logs (admin_id, action, target_type, target_id, detail) "
        "VALUES (%s,%s,%s,%s,%s)",
        (int(admin_id), action, target_type, str(target_id),
         json.dumps(detail, ensure_ascii=False, default=str)))


# --------------------------------------------------------------------------- #
#  事件写入
# --------------------------------------------------------------------------- #
def create_event(uid: int, code: str, severity: str, payload: dict,
                 linked_txn_ids: list[int] | None = None,
                 user_facing: bool = False) -> int:
    eid = db.execute(
        "INSERT INTO risk_events (user_id, rule_code, severity, status, payload, "
        "linked_txn_ids) VALUES (%s,%s,%s,'open',%s,%s)",
        (int(uid), code, severity, json.dumps(payload, ensure_ascii=False, default=str),
         json.dumps(linked_txn_ids or [], ensure_ascii=False)))
    return int(eid)


# --------------------------------------------------------------------------- #
#  转移前/后评估（通道 A + B）
# --------------------------------------------------------------------------- #
def _u_txn_count(uid: int, extra_where: str, args: list) -> int:
    row = db.fetch_one(
        "SELECT COUNT(*) AS c FROM transactions t JOIN accounts a ON a.id=t.account_id "
        "WHERE a.user_id=%s AND t.category='转账' AND t.type='expense' "
        "AND t.settle_status IS NULL " + extra_where, [int(uid)] + args)
    return int((row or {}).get("c") or 0)


def eval_transfer_risk(uid: int, amount: Decimal, payee_id: int | None,
                       payee_masked: str, account_id: int | None, track: str,
                       txn_id: int, is_frequent: bool,
                       balance_after: Decimal) -> dict:
    """评估一笔已（或将）发生的转账。返回 {user_facing:[...], events:[...]}。

    ★ 调用方应在扣款前调用；命中 user_facing 规则即返回信号（通道 A）；
      所有命中同时落 risk_events（通道 B）。
    """
    amount = Decimal(str(amount))
    user_facing: list[dict] = []
    events: list[int] = []
    linked = [int(txn_id)] if txn_id else []

    def _hit(code: str) -> dict | None:
        cfg = get_rule_cfg(code)
        if not cfg or not cfg["enabled"]:
            return None
        return cfg

    # ---- R-DUP：同收款人 + 同额，窗口内重复（诈骗"再转一次"）----
    # 控制误报：常用收款人（is_frequent）的周期性同额转账视为正常 → 不触发。
    cfg = _hit("R-DUP")
    if cfg and payee_id and not is_frequent:
        th = _parse_threshold(cfg["threshold"])
        win = int(th.get("window_min", 30))
        same = int(th.get("amount_same", 1))
        c = _u_txn_count(uid,
                         "AND t.payee_id=%s AND t.amount=%s "
                         "AND t.created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)",
                         [int(payee_id), amount, win])
        if c + 1 >= same + 1:  # 含本次 >= amount_same+1
            ev = create_event(uid, "R-DUP", cfg["severity"],
                              {"amount_bucket": amount_bucket(amount),
                               "payee_masked": payee_masked, "window_min": win,
                               "count": c + 1}, linked, cfg["user_facing"])
            events.append(ev)
            if cfg["user_facing"]:
                user_facing.append({"rule": "R-DUP", "severity": cfg["severity"],
                                    "msg": f"短时间内向同一收款人重复转出相同金额 ¥{float(amount):,.2f}，"
                                           f"请确认是否为本人操作"})

    # ---- R-FREQ：高频转账 ----
    cfg = _hit("R-FREQ")
    if cfg:
        th = _parse_threshold(cfg["threshold"])
        win = int(th.get("window_min", 5))
        n = int(th.get("count", 3))
        c = _u_txn_count(uid,
                         "AND t.created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)",
                         [win])
        if c >= n:
            ev = create_event(uid, "R-FREQ", cfg["severity"],
                              {"window_min": win, "count": c}, linked, cfg["user_facing"])
            events.append(ev)
            if cfg["user_facing"]:
                user_facing.append({"rule": "R-FREQ", "severity": cfg["severity"],
                                    "msg": f"{win} 分钟内已转出 {c} 笔，频率异常，请确认"})

    # ---- R-NEWLARGE：新收款人大额（仅站内可结构化判定）----
    cfg = _hit("R-NEWLARGE")
    if cfg and payee_id:
        th = _parse_threshold(cfg["threshold"])
        thr = Decimal(str(th.get("amount", 3000)))
        c = _u_txn_count(uid, "AND t.payee_id=%s", [int(payee_id)])
        if c == 0 and amount > thr:
            ev = create_event(uid, "R-NEWLARGE", cfg["severity"],
                              {"amount_bucket": amount_bucket(amount),
                               "payee_masked": payee_masked, "threshold": str(thr)},
                              linked, cfg["user_facing"])
            events.append(ev)
            if cfg["user_facing"]:
                user_facing.append({"rule": "R-NEWLARGE", "severity": cfg["severity"],
                                    "msg": f"首次向该收款人转出大额 ¥{float(amount):,.2f}，请核实收款人"})

    # ---- R-AMTOUT：金额突变（超近30天均值 K 倍）----
    cfg = _hit("R-AMTOUT")
    if cfg:
        th = _parse_threshold(cfg["threshold"])
        k = Decimal(str(th.get("k", 3)))
        days = int(th.get("window_days", 30))
        row = db.fetch_one(
            "SELECT AVG(t.amount) AS a FROM transactions t "
            "JOIN accounts a ON a.id=t.account_id "
            "WHERE a.user_id=%s AND t.category='转账' AND t.type='expense' "
            "AND t.settle_status IS NULL AND t.id!=%s "
            "AND t.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)",
            (int(uid), int(txn_id) if txn_id else 0, days))
        avg = (row or {}).get("a")
        if avg is not None and amount > k * Decimal(str(avg)):
            ev = create_event(uid, "R-AMTOUT", cfg["severity"],
                              {"amount_bucket": amount_bucket(amount),
                               "avg": str(avg), "k": str(k)}, linked, cfg["user_facing"])
            events.append(ev)
            if cfg["user_facing"]:
                user_facing.append({"rule": "R-AMTOUT", "severity": cfg["severity"],
                                    "msg": f"本次转账金额 ¥{float(amount):,.2f} 远超近 {days} 天均值，"
                                           f"请确认"})

    # ---- R-SCATTER：分散转出 ----
    cfg = _hit("R-SCATTER")
    if cfg:
        th = _parse_threshold(cfg["threshold"])
        win = int(th.get("window_min", 10))
        dp = int(th.get("distinct_payees", 3))
        row = db.fetch_one(
            "SELECT COUNT(DISTINCT t.payee_id) AS d FROM transactions t "
            "JOIN accounts a ON a.id=t.account_id "
            "WHERE a.user_id=%s AND t.category='转账' AND t.type='expense' "
            "AND t.settle_status IS NULL AND t.payee_id IS NOT NULL "
            "AND t.created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)",
            (int(uid), win))
        d = int((row or {}).get("d") or 0)
        if d >= dp:
            ev = create_event(uid, "R-SCATTER", cfg["severity"],
                              {"window_min": win, "distinct_payees": d}, linked,
                              cfg["user_facing"])
            events.append(ev)
            if cfg["user_facing"]:
                user_facing.append({"rule": "R-SCATTER", "severity": cfg["severity"],
                                    "msg": f"{win} 分钟内向 {d} 个不同收款人转出，疑似分散转移"})

    # ---- R-DRAIN：余额清空 ----
    cfg = _hit("R-DRAIN")
    if cfg:
        th = _parse_threshold(cfg["threshold"])
        minb = Decimal(str(th.get("min_balance", 100)))
        if balance_after <= minb:
            ev = create_event(uid, "R-DRAIN", cfg["severity"],
                              {"balance_after": str(balance_after),
                               "min_balance": str(minb)}, linked, cfg["user_facing"])
            events.append(ev)
            if cfg["user_facing"]:
                user_facing.append({"rule": "R-DRAIN", "severity": cfg["severity"],
                                    "msg": f"转账后余额仅剩 ¥{float(balance_after):,.2f}，"
                                           f"近乎清空，请确认"})

    # ---- R-NIGHT：深夜大额 ----
    cfg = _hit("R-NIGHT")
    if cfg:
        th = _parse_threshold(cfg["threshold"])
        sh = int(th.get("start_hour", 0))
        eh = int(th.get("end_hour", 6))
        thr = Decimal(str(th.get("amount", 2000)))
        hr = datetime.now().hour
        if sh <= hr < eh and amount > thr:
            ev = create_event(uid, "R-NIGHT", cfg["severity"],
                              {"amount_bucket": amount_bucket(amount), "hour": hr},
                              linked, cfg["user_facing"])
            events.append(ev)
            if cfg["user_facing"]:
                user_facing.append({"rule": "R-NIGHT", "severity": cfg["severity"],
                                    "msg": f"凌晨 {hr}:00 大额转账 ¥{float(amount):,.2f}，请确认"})

    return {"user_facing": user_facing, "events": events}


# --------------------------------------------------------------------------- #
#  R-CHARGE：异常充值（模块 B 联动）
# --------------------------------------------------------------------------- #
def eval_charge_redeem(uid: int, amount: Decimal, txn_id: int | None) -> dict:
    cfg = get_rule_cfg("R-CHARGE")
    if not cfg or not cfg["enabled"]:
        return {"events": []}
    th = _parse_threshold(cfg["threshold"])
    win = int(th.get("window_min", 60))
    n = int(th.get("count", 3))
    daily = Decimal(str(th.get("daily_amount", 20000)))
    amount = Decimal(str(amount))

    cnt = db.fetch_one(
        "SELECT COUNT(*) AS c FROM card_keys WHERE used_by=%s AND status='used' "
        "AND used_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)", (int(uid), win))
    c = int((cnt or {}).get("c") or 0)
    drow = db.fetch_one(
        "SELECT COALESCE(SUM(amount),0) AS s FROM card_keys "
        "WHERE used_by=%s AND status='used' AND DATE(used_at)=CURDATE()", (int(uid),))
    daily_amt = Decimal(str((drow or {}).get("s") or 0))

    hits = []
    if c >= n:
        hits.append(("R-CHARGE", cfg["severity"],
                     {"window_min": win, "count": c}, cfg["user_facing"]))
    if daily_amt > daily:
        hits.append(("R-CHARGE", cfg["severity"],
                     {"daily_amount": str(daily_amt), "threshold": str(daily)},
                     cfg["user_facing"]))
    events = []
    for code, sev, payload, uf in hits:
        ev = create_event(uid, code, sev, payload,
                         [int(txn_id)] if txn_id else None, uf)
        events.append(ev)
    return {"events": events}


# --------------------------------------------------------------------------- #
#  后台查询 / 处理 / 披露
# --------------------------------------------------------------------------- #
def list_events(status: str = "", severity: str = "", page: int = 1,
                page_size: int = 50) -> dict:
    where = []
    args: list = []
    if status:
        where.append("e.status=%s")
        args.append(status)
    if severity:
        where.append("e.severity=%s")
        args.append(severity)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.fetch_one(
        "SELECT COUNT(*) AS n FROM risk_events e" + wsql, args)["n"]
    rows = db.fetch_all(
        "SELECT e.id, e.user_id, e.rule_code, e.severity, e.status, e.hit_at, "
        "e.payload, e.linked_txn_ids, e.handled_by, e.handled_at, e.note "
        "FROM risk_events e" + wsql +
        " ORDER BY e.hit_at DESC LIMIT %s OFFSET %s",
        args + [int(page_size), int((page - 1) * page_size)])
    out = []
    for r in rows:
        d = dict(r)
        d["user_masked"] = mask_uid(d["user_id"])
        d.pop("user_id", None)
        out.append(d)
    return {"total": int(total), "page": int(page), "page_size": int(page_size),
            "events": out}


def handle_event(event_id: int, status: str, note: str, admin_id: int) -> dict:
    if status not in ("confirmed", "dismissed", "closed"):
        raise ValueError("status 只能是 confirmed/dismissed/closed")
    cfg = db.fetch_one("SELECT id, status FROM risk_events WHERE id=%s", (int(event_id),))
    if not cfg:
        raise KeyError("事件不存在")
    db.execute(
        "UPDATE risk_events SET status=%s, note=%s, handled_by=%s, handled_at=NOW() "
        "WHERE id=%s", (status, (note or "")[:255], int(admin_id), int(event_id)))
    _audit(admin_id, "risk_event_handle", "risk_event", event_id,
           detail={"status": status, "note": note})
    return {"ok": True, "event_id": event_id, "status": status}


def purge_old_events(days: int = 90) -> int:
    """数据保留策略（TTL）：清理超过 N 天的已处理事件与明细查看记录。

    ★ 隐私 by design：告警明细不长期留存。建议由运维定时任务每日调用。
    仅清理 status != 'open' 的旧事件（未处理的不动）。
    """
    old = db.fetch_all(
        "SELECT id FROM risk_events WHERE hit_at < DATE_SUB(NOW(), INTERVAL %s DAY) "
        "AND status <> 'open'", (int(days),))
    ids = [int(r["id"]) for r in old]
    for eid in ids:
        db.execute("DELETE FROM risk_event_views WHERE event_id=%s", (eid,))
        db.execute("DELETE FROM risk_events WHERE id=%s", (eid,))
    return len(ids)


def disclose_event(event_id: int, reason: str, admin_id: int) -> dict:
    """展开完整明细（强制理由 + 审计）。返回事件全字段 + 关联流水最小信息。"""
    cfg = db.fetch_one("SELECT * FROM risk_events WHERE id=%s", (int(event_id),))
    if not cfg:
        raise KeyError("事件不存在")
    if not (reason or "").strip():
        raise ValueError("展开明细必须填写理由（留痕）")
    db.execute(
        "INSERT INTO risk_event_views (event_id, admin_id, reason, fields_disclosed) "
        "VALUES (%s,%s,%s,%s)",
        (int(event_id), int(admin_id), (reason or "")[:255], "payload,linked_txn_ids"))
    _audit(admin_id, "risk_event_disclose", "risk_event", event_id,
           detail={"reason": reason})
    # 关联流水最小信息（金额/时间/分类），不含 remark / chat
    txn_ids = []
    try:
        txn_ids = json.loads(cfg["linked_txn_ids"] or "[]")
    except Exception:  # noqa: BLE001
        txn_ids = []
    txns = []
    if txn_ids:
        ph = ",".join(["%s"] * len(txn_ids))
        rows = db.fetch_all(
            "SELECT id, account_id, amount, category, merchant, type, `date` "
            "FROM transactions WHERE id IN (" + ph + ")", txn_ids)
        txns = [dict(r) for r in rows]
    return {"ok": True, "event": dict(cfg), "linked_txns": txns}
