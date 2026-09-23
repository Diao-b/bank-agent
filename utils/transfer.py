"""utils/transfer.py —— 转账域单文件（resolve 收款人 + execute 执行）。

组 T（合并式重构）：
  * 原 `utils/transfer_resolver.py`（resolve_payee 唯一入口 + 卡号/账户反查）整体并入本文件；
  * 原 `mock_bank.main` 的 `POST /api/transfer` 内联执行逻辑抽为 `execute_transfer()`；
  * 轨道判定收口为单一谓词 `is_insite_track()`（A4：消除 resolver 与 route 的双份判定）。
行为零变化：SQL / 文案 / 返回 shape / 事务边界逐字保留。

★ 依赖约定：本模块**顶层刻意不 import mock_bank.main**（否则与之形成循环导入，
  因为 main 的 helper owned/to_decimal/fail 定义在本模块之后被 main 使用）。
  仅 `execute_transfer` 内部惰性 import 这三个共享 helper。
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pymysql
from fastapi import HTTPException

import config
import rules
from mock_bank import db
from utils import card_validator, inbox, settlement_engine
from utils.response_codes import ERR


# --------------------------------------------------------------------------- #
#  轨道判定（A4 收口：唯一谓词，标签由调用方各自决定）
# --------------------------------------------------------------------------- #
def is_insite_track(linked_user_id: Any, has_insite_target: bool = True) -> bool:
    """是否走「站内轨」的唯一判定。

    站内 = 收款人绑定了站内用户（linked_user_id 非空）且存在可入账账户。
    - resolve（/api/payees/resolve）不做实时用户校验，只按绑定判断（has_insite_target 默认 True）；
    - execute（/api/transfer）实时校验 linked 用户与目标账户存在后传入真实 has_insite_target。
    ★ 两端点的 track **标签不同**（resolve 用 cross_bank；execute 用 offsite），
      故只收口谓词、不统一标签（保持既有 API 契约与前端/测试断言）。
    """
    return linked_user_id is not None and has_insite_target


# --------------------------------------------------------------------------- #
#  基础查询
# --------------------------------------------------------------------------- #
def _bank_by_code(code: str) -> dict | None:
    return db.fetch_one(
        "SELECT id, bank_name, bank_code, is_local FROM bank_directory WHERE bank_code=%s",
        (code,),
    )


def _payee_row(uid: int, payee_id: int) -> dict | None:
    return db.fetch_one(
        "SELECT id, user_id, nickname, bank, account_tail, is_frequent, "
        "linked_user_id, real_name, card_no, bank_id, account_id "
        "FROM payees WHERE id=%s AND user_id=%s",
        (payee_id, uid),
    )


def _payee_by_card(uid: int, card_no: str) -> dict | None:
    return db.fetch_one(
        "SELECT id, user_id, nickname, bank, account_tail, is_frequent, "
        "linked_user_id, real_name, card_no, bank_id, account_id "
        "FROM payees WHERE user_id=%s AND card_no=%s LIMIT 1",
        (uid, card_no),
    )


def _account_holder_by_card(card_no: str) -> dict | None:
    """微众卡号反查账户实名持有者（站内反查核心）。

    返回 {user_id, account_id, real_name} 或 None（卡号不在本系统 / 是信用卡）。
    ★ 卡片-账户一体化后，卡号有两个来源（见 lookup_local_card）：
      - accounts.card_no：账户本号（申请储蓄卡开新账户时生成）；
      - cards.card_no_full：挂靠在账户下的卡（含同一账户的第 2 张卡）。
      两者都能定位到具体账户 —— 「同一人可有多卡多账户，resolve 按 card_no
      命中具体账户」，入账也进这个账户，而不是对方 id 最小的账户。
    ★ 信用卡被显式排除：信用卡余额语义是「可用额度」不是存款，
      不参与 resolve_payee 的收付轨道。
    """
    info = lookup_local_card(card_no)
    if not info or info.get("credit_card") or not info.get("account_id"):
        return None
    return {"user_id": int(info["user_id"]), "account_id": int(info["account_id"]),
            "real_name": info.get("real_name")}


def lookup_local_card(card_no: str, cur=None) -> dict | None:
    """卡号 → 站内账户（唯一口径，供 resolve_payee 与新增收款人共用）。

    查找顺序：
      1) accounts.card_no（账户编号；排除信用账户）
      2) cards.card_no_full（账户下挂靠的卡；排除信用卡）
    返回：
      命中储蓄账户 → {account_id, user_id, real_name, source, card_type='debit', ...}
      命中信用卡   → {credit_card: True, user_id}（调用方据此给出「不支持收款」提示）
      未命中       → None

    ★ cur 传入时复用调用方事务（新增收款人端点必须这样用 —— 反查与建记录
      必须看到同一个数据库视图）；不传则自开连接读。
    """
    def _one(sql: str, args: tuple):
        if cur is not None:
            cur.execute(sql, args)
            return cur.fetchone()
        return db.fetch_one(sql, args)

    card_no = card_validator.normalize_card_input(card_no)
    if not card_no:
        return None

    row = _one(
        "SELECT a.id AS account_id, a.user_id AS user_id, u.name AS real_name, "
        "       a.account_type AS account_type, a.name AS account_name "
        "FROM accounts a JOIN users u ON u.id = a.user_id "
        "WHERE a.card_no=%s AND COALESCE(a.account_type,'debit') <> 'credit' "
        "ORDER BY a.id ASC LIMIT 1",
        (card_no,),
    )
    if row:
        return {"account_id": int(row["account_id"]), "user_id": int(row["user_id"]),
                "real_name": row.get("real_name"), "source": "account",
                "card_type": "debit", "card_id": None,
                "account_name": row.get("account_name")}

    row = _one(
        "SELECT c.id AS card_id, c.account_id AS account_id, c.user_id AS user_id, "
        "       c.card_type AS card_type, c.card_name AS card_name, u.name AS real_name "
        "FROM cards c JOIN users u ON u.id = c.user_id "
        "WHERE c.card_no_full=%s AND c.status <> 'deleted' "
        "ORDER BY c.id ASC LIMIT 1",
        (card_no,),
    )
    if not row:
        return None
    if (row.get("card_type") or "debit") == "credit":
        return {"credit_card": True, "user_id": int(row["user_id"]),
                "card_name": row.get("card_name")}
    return {"account_id": int(row["account_id"]) if row.get("account_id") else None,
            "user_id": int(row["user_id"]), "real_name": row.get("real_name"),
            "source": "card", "card_type": "debit",
            "card_id": int(row["card_id"]) if row.get("card_id") else None,
            "card_name": row.get("card_name")}


def _account_holder_by_uid(user_id: int) -> dict | None:
    return db.fetch_one(
        "SELECT a.id AS account_id, a.user_id AS user_id, u.name AS real_name "
        "FROM accounts a JOIN users u ON u.id = a.user_id "
        "WHERE a.user_id=%s ORDER BY a.id ASC LIMIT 1",
        (user_id,),
    )


# --------------------------------------------------------------------------- #
#  结果构造
# --------------------------------------------------------------------------- #
def _rejected(message: str) -> dict:
    return {"ok": False, "status": "rejected", "error": message}


def _not_found(message: str) -> dict:
    return {"ok": False, "status": "not_found", "error": message, "suggest": "add_payee"}


def _validate_payee(p: dict, in_name: str | None = None) -> dict:
    """对一条已存在的收款人记录做完整卡号校验，返回统一结果。

    这是 resolve 的真正落点：所有命中路径最终都汇聚到这里。
    """
    card_no = (p.get("card_no") or "").strip()
    if not card_no or not card_validator.luhn_check(card_no):
        return _rejected("收款人卡号校验未通过，请重新添加该收款人后再转账")

    bank_code = card_validator.bank_from_card(card_no)
    if bank_code is None:
        return _rejected("无法识别该收款人卡号所属银行，请重新添加")
    bank = _bank_by_code(bank_code)
    if bank is None:
        return _rejected("该收款人卡号对应的银行不在银行目录中，请重新添加")

    track = "insite" if is_insite_track(p.get("linked_user_id")) else "cross_bank"

    # 实名来源：站内来自账户持有者（users.name），跨行来自填单 real_name
    if track == "insite":
        holder = _account_holder_by_uid(int(p["linked_user_id"]))
        real_full = (holder or {}).get("real_name") or p.get("real_name") or ""
    else:
        real_full = p.get("real_name") or ""

    masked = card_validator.mask_real_name(real_full)
    name_mismatch = bool(in_name) and in_name.strip() != (real_full or "").strip()

    return {
        "ok": True,
        "status": track,
        "track": track,
        "payee_id": int(p["id"]),
        "payee_name": p["nickname"],
        "bank_id": int(bank["id"]),
        "bank_name": bank["bank_name"],
        "bank_code": bank_code,
        "bank_is_local": bool(bank["is_local"]),
        "card_no": card_no,
        "card_tail": card_no[-4:],
        # 站内收款：目标账户（入账落点）。一人可有多账户多卡，
        # 转账必须进「这张卡对应的账户」，而不是对方 id 最小的账户。
        "account_id": (int(p["account_id"]) if p.get("account_id") else None),
        "is_frequent": bool(p.get("is_frequent")),
        "real_name": masked,            # 脱敏展示用
        "name_mismatch": name_mismatch,  # 输入姓名与系统实名不符（仅警告）
        "message": ("站内用户，资金实时到账" if track == "insite"
                    else "跨行账户，预计到账 2 小时内"),
    }


# --------------------------------------------------------------------------- #
#  转账限额（单笔 / 单日累计，默认均 = rules.transfer_daily_limit）
#  ★ 口径（与 docs/knowledge/transfer-rules.md 一致）：
#     - 单笔限额：单笔转账金额上限；
#     - 当日累计：本自然日「已成功转出」的金额上限；
#       已落账的站内转账（实时到账）与跨行转账（扣款即在途）均计入；
#       退票（settle_status='returned'，资金原路退回）当日返还额度
#       （回补见 utils/settlement_engine._settle_one 的退票分支）。
#     - 本人账户间归集（remark='内部划转'）不计入对外转出限额。
#  ★ 并发安全：以 transfer_daily_quota 的「(user_id, 当日)」行做 SELECT ... FOR UPDATE
#     当前读 + 计数器累加，后到者被行锁阻塞到前一笔提交后再读，
#     因此不会双双读到 0 而同时放行（绕过限额）。
# --------------------------------------------------------------------------- #
def _limit_reset_at() -> str:
    """额度重置时间（次日本地 00:00:00）。"""
    return (datetime.now() + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()


def _limit_info(limit: Decimal, used: Decimal, remaining: Decimal, reason: str) -> dict:
    """结构化额度信息（前端 / 对话回执复用，字段固定）。"""
    return {
        "limit": float(limit),
        "used": float(used),
        "remaining": float(remaining),
        "reset_at": _limit_reset_at(),
        "reason": reason,
    }


def check_transfer_limit(cur, uid: int, amount: Decimal) -> tuple[bool, dict]:
    """在事务游标上做限额校验，返回 (ok, info)。

    info 含 limit / used / remaining / reset_at / reason。
    仅当 limit<=0（参数被运营置为 0，等同关闭限额）时放行且不返回 info。
    """
    limit = Decimal(str(rules.get_rule("transfer_daily_limit")))
    if limit <= 0:
        return True, {}
    # 锁当天的限额锚点行（当前读）；行不存在则建（并发下另一事务已插入 → 忽略后重读）
    cur.execute(
        "SELECT used_amount FROM transfer_daily_quota "
        "WHERE user_id=%s AND quota_date=CURDATE() FOR UPDATE", (uid,))
    row = cur.fetchone()
    if row is None:
        try:
            cur.execute(
                "INSERT INTO transfer_daily_quota (user_id, quota_date, used_amount) "
                "VALUES (%s, CURDATE(), 0.00)", (uid,))
            used = Decimal("0")
        except pymysql.err.IntegrityError:
            cur.execute(
                "SELECT used_amount FROM transfer_daily_quota "
                "WHERE user_id=%s AND quota_date=CURDATE() FOR UPDATE", (uid,))
            used = Decimal(str((cur.fetchone() or {}).get("used_amount") or 0))
    else:
        used = Decimal(str(row["used_amount"]))
    if amount > limit:
        return False, _limit_info(limit, used, limit - used, "single_exceeded")
    if used + amount > limit:
        return False, _limit_info(limit, used, limit - used, "daily_exceeded")
    return True, _limit_info(limit, used, limit - used, "ok")


def apply_transfer_limit(cur, uid: int, amount: Decimal) -> None:
    """限额通过后累加当日已用额度（与扣款同事务；退票回补见 settlement_engine）。"""
    cur.execute(
        "UPDATE transfer_daily_quota SET used_amount = used_amount + %s "
        "WHERE user_id=%s AND quota_date=CURDATE()", (amount, uid))


# --------------------------------------------------------------------------- #
#  主入口
# --------------------------------------------------------------------------- #
def resolve_payee(input: dict, uid: int) -> dict:
    """统一解析转账收款人。返回 dict 含 ok / status 等字段，可直接序列化。

    输入字段（任意组合，互相兜底）：
      payee_id  直接给收款人 ID（前端选中 / 对话序号）
      nickname  昵称（模糊匹配当前用户名下收款人）
      card_tail 卡号后四位（防转错人核对）
      card_no   完整卡号（优先于昵称）
      real_name 用户输入的对方姓名（辅助核对，站内会比对实名）
    """
    payee_id = input.get("payee_id")
    nickname = (input.get("nickname") or "").strip()
    card_tail = (input.get("card_tail") or "").strip()
    card_no = card_validator.normalize_card_input(input.get("card_no") or "")
    in_name = (input.get("real_name") or "").strip()

    # ---- 1) 直接给 payee_id → 取该收款人 ----
    if payee_id is not None:
        try:
            pid = int(payee_id)
        except (TypeError, ValueError):
            return _not_found("收款人标识无效")
        p = _payee_row(uid, pid)
        if not p:
            return _not_found("收款人不存在或不属于当前用户，请先通过「新增收款人」完成验证")
        return _validate_payee(p, in_name)

    # ---- 2) 完整卡号优先 ----
    if card_no:
        if not card_validator.luhn_check(card_no):
            return _rejected("卡号有误，请核对后重试")
        bank_code = card_validator.bank_from_card(card_no)
        if bank_code is None:
            return _rejected("无法识别卡号所属银行，请核对卡号")
        bank = _bank_by_code(bank_code)
        if bank is None:
            return _rejected("该卡号对应的银行不在银行目录中，请核对卡号")

        # 微众（本行）→ 站内反查账户是否存在（核心拦截点）
        if bank["is_local"]:
            holder = _account_holder_by_card(card_no)
            if not holder:
                return _rejected("该微众卡号不存在，请核对卡号")  # ★ 随便填的卡号直接被拒
        # 是否已在收款人簿（转账必须选中已验证收款人）
        p = _payee_by_card(uid, card_no)
        if not p:
            if bank["is_local"]:
                return _not_found("该微众卡号对应的用户尚未加入您的收款人簿，请先通过「新增收款人」完成账户验证")
            return _not_found("该跨行卡号未在您的收款人簿中，请先通过「新增收款人」完成账户验证")
        return _validate_payee(p, in_name)

    # ---- 3) 仅 nickname（从收款人簿选）----
    if not nickname:
        return _not_found("未提供收款人信息，请在收款人簿中选择或新增")

    matches = db.fetch_all(
        "SELECT id, user_id, nickname, bank, account_tail, is_frequent, "
        "linked_user_id, real_name, card_no, bank_id, account_id "
        "FROM payees WHERE user_id=%s AND nickname LIKE %s "
        "ORDER BY is_frequent DESC, id ASC",
        (uid, f"%{nickname}%"),
    )
    if not matches:
        return _not_found(f"收款人簿中无「{nickname}」账户，请先通过「新增收款人」完成账户验证后转账")

    # 多候选：若同时给了尾号，用尾号过滤（双妈妈流程保留）
    if len(matches) > 1 and card_tail:
        filtered = [m for m in matches if str(m["account_tail"]).strip() == card_tail.strip()]
        if filtered:
            matches = filtered

    if len(matches) > 1:
        candidates = []
        for m in matches:
            bc = card_validator.bank_from_card(m["card_no"] or "")
            bk = _bank_by_code(bc) if bc else None
            candidates.append({
                "payee_id": int(m["id"]),
                "nickname": m["nickname"],
                "bank_name": (bk or {}).get("bank_name") or m["bank"],
                "card_tail": (m["card_no"] or "")[-4:],
                "is_frequent": bool(m["is_frequent"]),
                "is_insite": m.get("linked_user_id") is not None,
            })
        return {
            "ok": True,
            "status": "ambiguous",
            "candidates": candidates,
            "message": f"找到 {len(candidates)} 个叫「{nickname}」的收款人，请选择其中一个",
        }

    p = matches[0]
    # nickname + 尾号：尾号不符 → 提醒核对但保留现有提醒逻辑（不阻断）
    if card_tail and str(p["account_tail"]).strip() != card_tail.strip():
        res = _validate_payee(p, in_name)
        res["tail_mismatch"] = True
        res["message"] = f"尾号不符，该收款人账户尾号为 {p['account_tail']}，请核对后再转账"
        return res

    return _validate_payee(p, in_name)


def execute_transfer(
    uid: int,
    from_account_id: int | None,
    payee_id: int,
    amount_raw: float,
    payload: dict,
    inject: str | None = None,
) -> dict:
    """站内转账闭环：归属校验 → 余额校验 → 按收款人身份分两轨执行 → 同事务落库。

    设计原则（转账按收款人身份分两轨）：
    - 站内转账（payees.linked_user_id 非空且对应的站内用户存在且正常）：
        发送方扣款 + 支出流水 + 发送方「转账成功」回执；
        收款方主账户入账 + 收入流水（分类:转账，merchant=发送方姓名）+ 收款方「收到转账」回执；
        双方各写 operation_logs（transfer_out / transfer_in）。
        ★ 金额在系统内守恒：发送方 -amount 与收款方 +amount 之和为零。
    - 跨行转账（linked_user_id 为 NULL，或对应站内用户已不存在/被禁用）：
        ★ 真实银行业务约束：本行无法实时校验对方账户是否存在。
        故扣款后资金进入「在途」状态 —— 支出流水 settle_status='pending' +
        登记 pending_settlements（expected_at = 创建 + 5 分钟，演示压缩；生产为 +2 小时）
        + 只发「已受理，清算中」通知，★不写成功回执。
        清算结果由 utils/settlement_engine 惰性触发：
          对方账户在 external_accounts 库 → 到账（settled）+ 到账通知；
          不在库（对方行退票）→ 全额退回主账户 + 退款流水 + 退票通知（returned）。
        ★ 账实一致：在途资金已离开余额，退票全额退回，失败资金不悬空。
    - 尾号校验（/api/payees/check）只服务于"防转错人"，不改变本流程；
      无论尾号是否匹配，风控分级 / 验证 / 确认照常执行。

    ★ 事务边界（已知坑：双方流水的事务必须与发送方扣款同事务，别拆两次连接；
      收件箱通知也进同一事务，通知丢失=收款方不知道进账，闭环破损）：
      扣款 / 入账 / 双流水 / 双回执 / 双方留痕，一个连接一个事务，
      任一失败整体回滚（绝不能"扣了款没收到的通知"或"通知了没入账"）。
    """
    # ★ 惰性 import：避开 utils.transfer ↔ mock_bank.main 的循环导入（本模块顶层刻意不 import main）
    from mock_bank.main import owned, to_decimal, fail  # noqa: PLC0415

    try:
        amount = to_decimal(amount_raw)
    except ValueError as exc:
        return fail(str(exc), code=ERR.TRANSFER_AMOUNT_INVALID, action="transfer")
    if amount <= 0:
        return fail("转账金额必须大于 0", code=ERR.TRANSFER_AMOUNT_INVALID, action="transfer")

    serial_no = None
    track = "offsite"
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                # ---- 1) 发送方账户（归属 + FOR UPDATE 行锁）----
                if from_account_id is not None:
                    account = owned(cur, "accounts", from_account_id, uid, for_update=True)
                else:
                    cur.execute(
                        "SELECT id, name, balance FROM accounts WHERE user_id=%s "
                        "AND COALESCE(account_type,'debit') <> 'credit' "
                        "ORDER BY id ASC LIMIT 1 FOR UPDATE",
                        (uid,),
                    )
                    account = cur.fetchone()
                    if not account:
                        raise HTTPException(status_code=400, detail="当前用户没有可用账户")

                payee = owned(cur, "payees", payee_id, uid)

                # ---- 2) 站内 / 行外判定 ----
                #  ★ 已知坑：linked_user_id 的用户被删除/禁用 → 转账前校验该用户存在且正常，
                #     否则降级为行外轨道 + 提示（资金仍离开本行，只是不再给"不存在的人"入账）。
                linked_uid = payee.get("linked_user_id")
                insite_target = None
                receiver_uid = None
                if linked_uid is not None:
                    cur.execute("SELECT id, name FROM users WHERE id=%s", (int(linked_uid),))
                    lu = cur.fetchone()
                    if lu:
                        # ★ 入账落点 = 收款人（账户）对应的具体账户（payees.account_id）。
                        #   卡片-账户一体化后，一个用户可以有多个账户、多张卡，
                        #   「转到哪张卡的卡号」就必须入账到那张卡挂靠的账户 ——
                        #   以前一律入账到对方 id 最小的账户，新卡收不到钱。
                        #   归属再校验一次（account_id 必须属于 linked_user_id），
                        #   防止脏数据把别人的钱打进第三方账户。
                        if payee.get("account_id"):
                            cur.execute(
                                "SELECT id, name, balance FROM accounts "
                                "WHERE id=%s AND user_id=%s FOR UPDATE",
                                (int(payee["account_id"]), int(linked_uid)),
                            )
                            insite_target = cur.fetchone()
                        if insite_target is None:
                            # 兜底：老收款人（account_id 为空）或指向已注销账户
                            # → 落回对方主账户（id 最小的那个），行为与升级前一致
                            cur.execute(
                                "SELECT id, name, balance FROM accounts WHERE user_id=%s "
                                "AND COALESCE(account_type,'debit') <> 'credit' "
                                "ORDER BY id ASC LIMIT 1 FOR UPDATE",
                                (int(linked_uid),),
                            )
                            insite_target = cur.fetchone()
                        receiver_uid = int(linked_uid)
                track = ("insite" if is_insite_track(linked_uid, insite_target is not None)
                         else "offsite")

                # ---- 2.5) 转账限额（单笔 / 单日累计）----
                #   ★ 权威校验点：在扣款事务内、持当天限额锚点行锁，计数器当前读。
                #     先查后扣，任一超限立即失败回滚（不累加）。
                _ok_limit, _lim_check = check_transfer_limit(cur, uid, amount)
                if not _ok_limit:
                    _lim = Decimal(str(_lim_check["limit"]))
                    _used = Decimal(str(_lim_check["used"]))
                    if _lim_check["reason"] == "single_exceeded":
                        _lim_msg = (
                            f"单笔转账超过限额：本行单笔最高 {float(_lim):,.0f} 元，"
                            f"本次 {float(amount):,.2f} 元无法转出。")
                    else:
                        _lim_msg = (
                            f"当日累计转出已超过限额：今日已转出 {float(_used):,.2f} 元，"
                            f"剩余额度 {float(_lim - _used):,.2f} 元，"
                            f"本次 {float(amount):,.2f} 元无法转出。")
                    db.log_operation(uid, "transfer", json.dumps(payload), "failed", cur=cur)
                    conn.commit()
                    _code = (ERR.TRANSFER_LIMIT_SINGLE if _lim_check["reason"] == "single_exceeded"
                             else ERR.TRANSFER_LIMIT_DAILY)
                    return fail(_lim_msg, code=_code, action="transfer", limit_info=_lim_check)

                # ---- 3) 发送方余额校验 ----
                if Decimal(str(account["balance"])) < amount:
                    db.log_operation(uid, "transfer", json.dumps(payload), "failed", cur=cur)
                    conn.commit()
                    return fail(
                        f"余额不足：账户「{account['name']}」当前余额 "
                        f"{float(account['balance']):.2f} 元，需转账 {float(amount):.2f} 元",
                        code=ERR.TRANSFER_INSUFFICIENT, action="transfer",
                    )

                sender_name = _sender_name(cur, uid)
                new_balance = Decimal(str(account["balance"])) - amount
                # ---- 3.5) 累加当日已用额度（与扣款同事务；退票回补见 settlement_engine）----
                apply_transfer_limit(cur, uid, amount)
                cur.execute("UPDATE accounts SET balance=%s WHERE id=%s", (new_balance, account["id"]))

                # 发送方支出流水（站内转账 remark 标"站内转账"，便于对账）
                merchant = f"{payee['nickname']}（{payee['bank']} {payee['account_tail']}）"
                # ★ 跨行轨：扣款即进入「在途」，流水标记 settle_status='pending'；
                #   清算结果（到账 settled / 退票 returned）由 utils/settlement_engine
                #   惰性触发时回写 —— 这是"失败资金不悬空"的账务锚点。
                settle_status = "pending" if track == "offsite" else None
                cur.execute(
                    "INSERT INTO transactions (account_id, `date`, amount, category, merchant, "
                    "type, remark, settle_status) "
                    "VALUES (%s, CURDATE(), %s, %s, %s, %s, %s, %s)",
                    (account["id"], amount, "转账", merchant, "expense",
                     "站内转账" if track == "insite" else
                     ("跨行转账（清算中）" if track == "offsite" else None),
                     settle_status),
                )
                sender_txn_id = cur.lastrowid
                serial_no = "TR" + datetime.now().strftime("%Y%m%d%H%M%S") + f"{random.randint(1000, 9999)}"

                # ★ 跨行轨：登记在途清算台账。
                #   expected_at = 创建 + 5 分钟（演示压缩；生产为 +2 小时）。
                #   在途期间用户余额已扣（与真实一致），资金不可用；
                #   到期后由清算引擎决定到账或退票。
                if track == "offsite":
                    cur.execute(
                        "INSERT INTO pending_settlements (txn_id, user_id, amount, "
                        "target_card_no, target_bank_id, serial_no, expected_at, status) "
                        "VALUES (%s, %s, %s, %s, %s, %s, "
                        "DATE_ADD(NOW(), INTERVAL %s MINUTE), 'pending')",
                        (sender_txn_id, uid, amount, payee.get("card_no"),
                         payee.get("bank_id"), serial_no,
                         rules.get_rule('settle_delay_minutes')),
                    )

                # ---- 4) 站内轨：收款方入账 + 收入流水 + 收款方回执 + 收款方留痕 ----
                if track == "insite":
                    # ★ 开发态滚回测试注入：仅当 BANK_ALLOW_ANON=1（dev）时生效，
                    #   通过请求头 X-Test-Inject: receiver-fail 触发，模拟"收款方入账失败"，
                    #   用于验证整笔转账回滚（生产态 BANK_ALLOW_ANON=0 时完全失效）。
                    if config.BANK_ALLOW_ANON and inject == "receiver-fail":
                        raise RuntimeError("【测试注入】模拟收款方入账失败")

                    recv_new = Decimal(str(insite_target["balance"])) + amount
                    cur.execute(
                        "UPDATE accounts SET balance=%s WHERE id=%s",
                        (recv_new, insite_target["id"]),
                    )
                    # 收入流水：分类=转账，merchant=发送方姓名（账单分析/流水明细可溯源）
                    cur.execute(
                        "INSERT INTO transactions (account_id, `date`, amount, category, merchant, type) "
                        "VALUES (%s, CURDATE(), %s, %s, %s, %s)",
                        (insite_target["id"], amount, "转账", sender_name, "income"),
                    )
                    # 收款方收件箱回执：含金额/付款人/流水号/入账后余额（资金闭环凭证）
                    # ★ 明确写出「入账账户」：一人多卡时，收款人要能看出钱进了哪张卡
                    #   （新开的储蓄卡收款 → 通知里就是这张卡挂靠的账户名）。
                    inbox.push_notification(
                        receiver_uid, "transfer_receipt", "收到转账",
                        f"您收到来自 {sender_name} 的转账 ¥{float(amount):,.2f}，"
                        f"已存入账户「{insite_target['name']}」（尾号 "
                        f"{payee.get('account_tail') or ''}）。"
                        f"流水号 {serial_no}，入账后余额 ¥{float(recv_new):,.2f}。",
                        meta={"amount": float(amount), "payer": sender_name,
                              "serial_no": serial_no, "balance_after": float(recv_new),
                              "track": "insite", "account_id": int(insite_target["id"]),
                              "account_name": insite_target["name"]},
                        cur=cur,
                    )
                    # 收款方留痕（安全中心可见"收到一笔站内转账"）
                    db.log_operation(
                        receiver_uid, "transfer_in",
                        {"from_account_id": account["id"], "payee_id": payee_id,
                         "amount": float(amount), "serial_no": serial_no},
                        "success", cur=cur,
                    )

                # ---- 5) 发送方收件箱回执（双轨文案不同）----
                if track == "insite":
                    send_title = "转账成功"
                    send_content = (
                        f"您向站内用户 {payee['nickname']}（{payee['bank']} 尾号 {payee['account_tail']}）"
                        f"转账 ¥{float(amount):,.2f} 已成功，资金实时到账。"
                        f"流水号 {serial_no}，扣款后余额 ¥{float(new_balance):,.2f}。"
                    )
                else:
                    # ★ 跨行：不写"成功回执" —— 成功与否由清算决定。
                    #   只发"已受理，清算中"，到账 / 退票由 settlement_engine 各发一条后续通知。
                    send_title = "跨行转账已受理"
                    send_content = (
                        f"您向 {payee['nickname']}（{payee['bank']} 尾号 {payee['account_tail']}）"
                        f"发起的跨行转账 ¥{float(amount):,.2f} 已受理，资金清算中，"
                        f"预计 5 分钟内完成（演示加速）；若收款账户异常将全额退回。"
                        f"流水号 {serial_no}，扣款后余额 ¥{float(new_balance):,.2f}。"
                    )
                inbox.push_notification(
                    uid, "transfer_receipt", send_title, send_content,
                    meta={"amount": float(amount), "payee": merchant, "serial_no": serial_no,
                          "balance_after": float(new_balance), "track": track,
                          "settle_status": settle_status},
                    cur=cur,
                )

                # ---- 6) 发送方留痕（站内记 transfer_out，行外沿用 transfer，便于安全中心统计）----
                db.log_operation(
                    uid, "transfer_out" if track == "insite" else "transfer",
                    {"from_account_id": account["id"], "payee_id": payee_id,
                     "amount": float(amount), "serial_no": serial_no, "track": track},
                    "success", cur=cur,
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "transfer", json.dumps(payload), "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"转账失败：{exc}", code=ERR.TRANSFER_FAILED, action="transfer")

    # 成功回执附结构化额度信息（若限额生效）：已用含本次，剩余为扣减后余额口径
    _success_limit = None
    if _lim_check:
        _lim_total = Decimal(str(_lim_check["limit"]))
        _used_after = Decimal(str(_lim_check["used"])) + amount
        _success_limit = _limit_info(_lim_total, _used_after, _lim_total - _used_after, "ok")

    return {
        "ok": True,
        "serial_no": serial_no,
        "amount": float(amount),
        "track": track,
        "settle_status": settle_status,
        "payee": f"{payee['nickname']}（{payee['bank']} {payee['account_tail']}）",
        "balance_after": float(new_balance),
        "limit_info": _success_limit,
        "message": (
            f"已向站内用户 {payee['nickname']} 转账 {float(amount):.2f} 元，资金实时到账，"
            f"流水号 {serial_no}"
            if track == "insite" else
            f"已受理向 {payee['nickname']} 的跨行转账 {float(amount):.2f} 元，资金清算中"
            f"（预计 5 分钟内完成，演示加速），若收款账户异常将全额退回，流水号 {serial_no}"
        ),
    }


def _sender_name(cur, uid: int) -> str:
    """取发送方展示姓名（transactions.merchant 用，收件箱回执也用）。"""
    cur.execute("SELECT name FROM users WHERE id=%s", (int(uid),))
    row = cur.fetchone()
    return (row or {}).get("name") or f"用户{uid}"
