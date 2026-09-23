"""
utils/inbox.py —— 站内收件箱数据层（C 体系）

总原则（与 README 一致）：
  送达体系：验证码等通知真实写入站内收件箱（数据自洽、可追溯），
            外部通道（邮箱/推送）为可选并行增强。
  展示安全：验证码消息只有"本人未过期"才在列表里返回明文 code——
            收件箱就是"手机"，用户本人看自己的码天经地义。

设计要点：
- notifications.meta 用 JSON 存结构化数据（code / purpose / serial_no / amount），
  列表接口按 type 做脱敏规则：verify_code 且未过期且本人 → meta 原样带 code。
- 外部通道 send_external_channel()：.env 配了 SMTP/PUSH token 才尽力同发，
  失败只打日志，绝不影响收件箱必达（收件箱写入失败才让发码整体失败）。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pymysql

import config
from mock_bank import db  # 连接收口到单一源（组 R）


def _conn():
    return db.get_conn()


# --------------------------------------------------------------------------- #
#  写入（供验证码 / 转账回执 / 公告复用）
# --------------------------------------------------------------------------- #
def push_notification(
    user_id: int,
    ntype: str,
    title: str,
    content: str,
    meta: dict | None = None,
    expires_at: datetime | None = None,
    cur: Any = None,
) -> int:
    """写一条站内通知，返回 notification id。

    ★ 事务语义：验证码双写（验证表 + 收件箱）必须同一事务 ——
      调用方传入已开启事务的 cur 则复用；不传则自开连接独立提交。
      验证码路径必须传 cur，否则违反"双写同事务、失败不返回已发送"铁律。
    """
    sql = (
        "INSERT INTO notifications (user_id, type, title, content, meta, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)"
    )
    args = (
        int(user_id), ntype, (title or "")[:100], content or "",
        json.dumps(meta or {}, ensure_ascii=False), expires_at,
    )
    if cur is not None:
        cur.execute(sql, args)
        return int(cur.lastrowid)
    with _conn() as conn:
        with conn.cursor() as c:
            c.execute(sql, args)
        conn.commit()
        return int(c.lastrowid)


def push_transfer_receipt(
    user_id: int,
    amount: float,
    payee: str,
    serial_no: str,
    action_label: str = "转账",
) -> int:
    """转账 / 理财执行成功 → 写 transfer_receipt 通知（含金额/收款人/流水号）。"""
    title = f"{action_label}成功"
    content = (
        f"您的{action_label}已成功：金额 ¥{float(amount):,.2f}，"
        f"收款方 {payee}，流水号 {serial_no}。可在账单与安全中心查看明细。"
    )
    return push_notification(
        user_id, "transfer_receipt", title, content,
        meta={"amount": float(amount), "payee": payee, "serial_no": serial_no,
              "action": action_label},
    )


# --------------------------------------------------------------------------- #
#  外部通道（可选并行增强）
# --------------------------------------------------------------------------- #
def send_external_channel(user_id: int, title: str, content: str) -> dict:
    """外部通道尽力同发：.env 配置了通知 token 才发；失败不影响收件箱必达。

    当前支持：SMTP 邮件（NOTIFY_SMTP_HOST 等配置齐备时）。
    未配置 / 发送失败 → {"sent": False, "reason": ...}，调用方只打日志。
    """
    host = getattr(config, "NOTIFY_SMTP_HOST", "") or ""
    if not host:
        return {"sent": False, "reason": "未配置外部通知通道"}
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.header import Header
        addr = getattr(config, "NOTIFY_MAIL_TO", "") or ""
        if not addr:
            return {"sent": False, "reason": "未配置收件邮箱"}
        msg = MIMEText(content, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = getattr(config, "NOTIFY_SMTP_USER", "") or host
        msg["To"] = addr
        with smtplib.SMTP(host, int(getattr(config, "NOTIFY_SMTP_PORT", "465")), timeout=8) as s:
            if str(getattr(config, "NOTIFY_SMTP_SSL", "1")) == "1":
                s.starttls()
            s.login(getattr(config, "NOTIFY_SMTP_USER", ""), getattr(config, "NOTIFY_SMTP_PASS", ""))
            s.sendmail(msg["From"], [addr], msg.as_string())
        return {"sent": True}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 外部通知通道发送失败（不影响收件箱）: {exc}")
        return {"sent": False, "reason": str(exc)}


# --------------------------------------------------------------------------- #
#  读取（C2 收件箱 API）
# --------------------------------------------------------------------------- #
def list_notifications(user_id: int, unread_only: bool = False, limit: int = 50) -> list[dict]:
    """当前用户消息列表（倒序）。验证码消息按"未过期且本人"规则带出 meta.code。"""
    sql = (
        "SELECT id, type, title, content, meta, is_read, expires_at, created_at "
        "FROM notifications WHERE user_id=%s"
    )
    args: list[Any] = [int(user_id)]
    if unread_only:
        sql += " AND is_read=0"
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(int(limit))
    with _conn() as conn:
        with conn.cursor() as c:
            c.execute(sql, args)
            rows = c.fetchall()
    now = datetime.now()
    out: list[dict] = []
    for r in rows:
        meta = _as_dict(r.get("meta"))
        expired = bool(r.get("expires_at") and now > r["expires_at"])
        item = {
            "id": int(r["id"]),
            "type": r.get("type"),
            "title": r.get("title"),
            "content": r.get("content"),
            # content 摘要：列表态只给前 60 字，展开态前端直接用 content 全文
            "summary": (r.get("content") or "")[:60],
            "is_read": bool(r.get("is_read")),
            "expires_at": str(r["expires_at"]) if r.get("expires_at") else None,
            "created_at": str(r["created_at"]),
            "expired": expired,
        }
        # ★验证码脱敏规则：type=verify_code 且未过期且本人（user_id 已按认证过滤）
        #  → 返回 meta 中的 code；过期/已用一律不给明文。
        if r.get("type") == "verify_code" and not expired:
            item["meta"] = {"code": meta.get("code"), "purpose": meta.get("purpose")}
        elif meta:
            item["meta"] = meta
        out.append(item)
    return out


def unread_count(user_id: int) -> int:
    with _conn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s AND is_read=0",
                (int(user_id),),
            )
            return int((c.fetchone() or {}).get("n") or 0)


def mark_read(user_id: int, notification_id: int) -> bool:
    """标记已读。★已读过的消息再次标记仍返回 True（存在即命中）——
    不能用 UPDATE rowcount 判断：已读行的 UPDATE rowcount=0 会被误判为"不存在"。"""
    with _conn() as conn:
        with conn.cursor() as c:
            c.execute(
                "SELECT 1 FROM notifications WHERE id=%s AND user_id=%s",
                (int(notification_id), int(user_id)),
            )
            hit = bool(c.fetchone())
            if hit:
                c.execute(
                    "UPDATE notifications SET is_read=1 WHERE id=%s AND user_id=%s",
                    (int(notification_id), int(user_id)),
                )
        conn.commit()
    return hit


def mark_all_read(user_id: int) -> int:
    with _conn() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE notifications SET is_read=1 WHERE user_id=%s AND is_read=0",
                      (int(user_id),))
            n = c.rowcount
        conn.commit()
    return n


def _as_dict(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}
