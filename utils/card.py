"""
utils/card.py —— 卡域单文件（组 K 合并：卡号校验纯函数 + 卡片-账户一体化服务）

★ 合并来源：card_validator.py（卡号校验层，纯函数无 DB）+ card_service.py（开卡服务）。
组 K「合并式重构」把同域逻辑收口到单文件，旧 card_validator.py / card_service.py 仅保留
兼容 re-export 壳（零调用方改动）。

设计原则（与 sql/upgrade_card_account.sql 顶部注释同源）
  卡片必须挂靠真实账户 —— 卡是账户的入口，不是装饰记录。
  · 申请储蓄卡 = 开新账户（生成账号 + 卡号，Luhn 合法）+ 绑卡；
    或挂靠已有账户（同一账户最多 2 张卡，超了提示）。
  · 申请信用卡 = 挂信用账户（每人一个，共享额度；卡有独立卡号）。
    ★ 信用卡不参与 resolve_payee 收付轨道（余额语义 = 可用额度，不是存款），只展示不转账。
  · 没有账户的卡片不允许存在。
  · 防滥用：单用户储蓄卡 ≤3 张、信用卡 ≤2 张，超出拒绝并提示现有卡数。

转账的标的物是「账户」不是「姓名」：
  - 站内转账：收款账号在本系统内 → 后端实时校验账户存在 → 实时到账
  - 跨行转账：收款账号在其他银行 → 本行无法验证对方账户，仅做卡号格式（Luhn）与
    银行选择（BIN）校验，T+0 提交、2 小时内到账（演示口径）
本模块只解决「卡号长什么样、属于哪家银行」；账户是否真实存在（站内反查）由
transfer_resolver 结合 bank_directory / accounts 判定。

事务边界
  本模块所有函数只接收调用方的 cursor，自己绝不 commit / rollback。
  「开账户 + 建卡 + 留痕」必须同一事务。
"""
from __future__ import annotations

import random
import re
from decimal import Decimal
from utils.response_codes import ERR

# --------------------------------------------------------------------------- #
#  演示级 BIN 表（前 4 位近似匹配）
#
#  ★ 真实银联 BIN 远比这复杂（多段、6~8 位、且多家银行共享前缀段），
#    这里仅为「演示级」映射：按前 4 位把卡号映射到 bank_directory.bank_code。
#  ★ 工行 / 交行在真实 BIN 上可能重叠，演示里刻意分配不同前缀以避免歧义：
#      6222 = 工商银行 / 6226 = 交通银行
#    这样验收"选招商银行、填 6222 开头卡号 → 卡号与所选银行不符"才成立
#    （招商银行演示 BIN 固定为 6225）。
#  ★ 本行 = 微众银行（WEBANK，前缀 6102），站内账户都开在微众银行行别下。
# --------------------------------------------------------------------------- #
BIN_TABLE: list[tuple[str, str, str]] = [
    ("6222", "ICBC",   "中国工商银行"),
    ("6228", "ABC",    "中国农业银行"),
    ("6216", "BOC",    "中国银行"),
    ("6217", "CCB",    "中国建设银行"),
    ("6226", "BCM",    "交通银行"),
    ("6225", "CMBC",   "招商银行"),
    ("6223", "CITIC",  "中信银行"),
    ("6221", "CEB",    "中国光大银行"),
    ("6220", "HXB",    "华夏银行"),
    ("6227", "MSB",    "中国民生银行"),
    ("6229", "PAB",    "平安银行"),
    ("6224", "CIB",    "兴业银行"),
    ("6230", "SPDB",   "浦发银行"),
    ("6210", "PSBC",   "邮储银行"),
    ("6213", "BJB",    "北京银行"),
    ("6214", "SHB",    "上海银行"),
    ("6215", "JSB",    "江苏银行"),
    ("6218", "NBCB",   "宁波银行"),
    ("6102", "WEBANK", "微众银行"),   # ★ 本行（is_local = 1）
]
BIN_TO_BANK: dict[str, tuple[str, str]] = {p: (c, n) for p, c, n in BIN_TABLE}

# 本行 BIN 前缀（微众银行）
LOCAL_BANK_BIN = "6102"
LOCAL_BANK_CODE = "WEBANK"

# 卡号长度范围（演示：12~19 位均视为合法长度，主流储蓄卡 16~19 位）
_CARD_LEN_MIN = 12
_CARD_LEN_MAX = 19


def luhn_check(card_no: str) -> bool:
    """Luhn 算法校验（借贷两用卡号通用）。

    规则：从右往左，偶数位（第 2/4/6…位）数字乘 2，若 ≥10 则减 9；
    所有位求和，总和能被 10 整除即合法。
    """
    digits = re.sub(r"\D", "", card_no or "")
    if not (_CARD_LEN_MIN <= len(digits) <= _CARD_LEN_MAX):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = ord(ch) - 48
        if i % 2 == 1:          # i=0 是最右位（不乘），i=1 是右数第 2 位（乘）
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _luhn_digit(partial: str) -> str:
    """给定前 n 位（不含校验位），补出 Luhn 校验位字符，使整体合法。"""
    base = re.sub(r"\D", "", partial or "")
    # 把 base 当作「不含校验位」的卡号，按 Luhn 反推校验位：
    # 在 base 后追加 0 计算，再用 (10 - (sum % 10)) % 10 得到校验位
    total = 0
    rev = base + "0"
    for i, ch in enumerate(reversed(rev)):
        n = ord(ch) - 48
        if i % 2 == 0:          # 追加的 0 处于最右位（i=0，不乘）；
            pass                # 其后每一位交替乘 2
        else:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    check = (10 - (total % 10)) % 10
    return str(check)


def generate_card_no(bin: str = "61022620", seq: int | None = None) -> str:
    """生成一张 16 位、Luhn 合法的卡号。

    ★ 本行（微众）生成规则：bin = 6102 2620（银行 BIN + 子段），
       seq 补 7 位序号，末位 Luhn 校验位 → 共 16 位。
    其他银行演示卡号同理：把 bin 换成对应银行 BIN 前缀即可。

    返回卡号 100% 通过 luhn_check；但不保证账户真实存在
    （账户存在性由 transfer_resolver 站内反查 accounts.card_no 判定）。
    """
    bin = re.sub(r"\D", "", bin or "")
    if seq is None:
        seq = random.randint(0, 9_999_999)
    seq7 = str(int(seq) % 10_000_000).zfill(7)
    body = (bin + seq7)[:15]            # 前 15 位（不足补、超出截断）
    body = body.ljust(15, "0")[:15]
    return body + _luhn_digit(body)


def generate_card_no_with_tail(bin4: str, tail4: str,
                               seq: int | None = None) -> str | None:
    """生成「前 4 位 = 银行 BIN、后 4 位 = 指定尾号」的 16 位 Luhn 合法卡号。

    用于存量收款人迁移：保留用户熟悉的尾号（如 妈妈·招行 6688），
    同时让整张卡号 Luhn 合法、BIN 正确，可被前端/后端即时校验。

    ★ 已知坑：不能直接「bin4 + 中段 + tail4 + 校验位」——那样末位校验位会把
      tail4 挤掉一位，最终卡号的尾号变成 tail4 的后 3 位 + 校验位（如 6688 → 6889）。
      正确做法是让 tail4 的最后一位本身充当校验位：搜索中段 8 位，
      使 luhn_digit(bin4 + mid8 + tail4[:3]) == tail4[3]。期望约 10 次命中。
    """
    bin4 = re.sub(r"\D", "", bin4 or "")[:4].ljust(4, "0")[:4]
    tail4 = re.sub(r"\D", "", tail4 or "")[:4].rjust(4, "0")[:4]
    prefix3, want_check = tail4[:3], tail4[3]
    start = int(seq) % 100_000_000 if seq is not None else random.randint(0, 99_999_999)
    for i in range(100_000):
        mid8 = str((start + i) % 100_000_000).zfill(8)
        if _luhn_digit(bin4 + mid8 + prefix3) == want_check:
            return bin4 + mid8 + tail4
    return None   # 理论上不会发生（1/10 概率 × 十万次）


def bank_from_card(card_no: str) -> str | None:
    """按前 4 位 BIN 段匹配 bank_directory 的 bank_code（演示级近似）。

    返回 bank_code（如 'WEBANK' / 'CMBC'），未命中返回 None。
    ★ 仅看前 4 位，与 BIN_TABLE 注释里的"演示级"一致；真实生产应支持 6~8 位 BIN。
    """
    digits = re.sub(r"\D", "", card_no or "")
    if len(digits) < 4:
        return None
    return BIN_TO_BANK.get(digits[:4], (None, None))[0]


def bin_of_bank(bank_code: str) -> str | None:
    """bank_directory.bank_code → 该银行的演示 BIN 前缀（BIN_TABLE 反向查表）。

    ★ 坑：BIN_TO_BANK 是以「BIN 前缀」为键（'6225' -> ('CMBC','招商银行')），
      不要拿 bank_code（'CMBC'）去查它——查不到会静默走默认值，导致所有
      他行演示卡号都带上招商银行的 BIN，出现「bank_id=工商 但卡号 BIN=招商」
      的不一致。要反查就用本函数。
    """
    if not bank_code:
        return None
    for prefix, code, _name in BIN_TABLE:
        if code == bank_code:
            return prefix
    return None


def is_local_card(card_no: str) -> bool:
    """是否本行（微众银行）卡号：前缀命中 LOCAL_BANK_BIN 即视为本行。"""
    digits = re.sub(r"\D", "", card_no or "")
    return digits.startswith(LOCAL_BANK_BIN)


def mask_real_name(name: str | None) -> str | None:
    """实名脱敏：保留姓氏 + 星号（如「李哥」→「李*」）。

    ★ 统一在展示层做；库里（users.name / payees.real_name）存全名。
      单字姓名退化为「*」（避免泄露全名）。
    """
    if not name:
        return None
    name = str(name).strip()
    if len(name) <= 1:
        return "*"
    return name[0] + "*" * (len(name) - 1)


def normalize_card_input(value: str) -> str:
    """用户输入卡号清洗：去空格 / 连字符 / 全角，仅留数字。"""
    if not value:
        return ""
    s = str(value)
    s = s.replace(" ", "").replace("-", "").replace("—", "")
    # 全角数字 → 半角
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF10 <= ord(c) <= 0xFF19 else c for c in s)
    return re.sub(r"\D", "", s)


# --------------------------------------------------------------------------- #
#  卡片-账户一体化服务（申请开卡 / 卡号生成与查重）
# --------------------------------------------------------------------------- #
# 本行（微众银行）卡号前 8 位子段：6102（微众 BIN）+ 2620（子段），
# 与 generate_card_no 的默认 bin 保持一致。
CARD_BIN = "61022620"

# 业务常量单源见 rules.py（组 W）：数量上限 / 默认信用额度 / 卡类型枚举。
import rules  # C6：卡上限/额度运行时从 system_config 读取（get_rule）
from rules import VALID_CARD_TYPES  # noqa: E402 业务枚举，不随运营调整

# --------------------------------------------------------------------------- #
#  C6 兼容别名（仅供 card_service 壳 re-export / 旧测试引用）；
#  运行时判断一律 rules.get_rule(...) —— 管理端改参数即时生效。
# --------------------------------------------------------------------------- #
DEBIT_CARD_MAX = rules.get_rule("debit_card_max")
CREDIT_CARD_MAX = rules.get_rule("credit_card_max")
CARDS_PER_ACCOUNT_MAX = rules.get_rule("cards_per_account_max")
DEFAULT_CREDIT_LIMIT = rules.get_rule("card_default_credit_limit")

CREDIT_ACCOUNT_NAME = "信用账户"

# 本行名称（cards.bank 展示用）
LOCAL_BANK_NAME = "微众银行"

# 卡类型
TYPE_DEBIT = "debit"
TYPE_CREDIT = "credit"


class CardApplyError(Exception):
    """开卡业务校验失败（归属 / 上限 / 账户类型等）。端点转成 {"ok": false, error}。"""

    def __init__(self, message: str, code: str = "rejected"):
        super().__init__(message)
        self.message = message
        self.code = code


# --------------------------------------------------------------------------- #
#  卡号展示工具
# --------------------------------------------------------------------------- #
def group_card_no(card_no: str) -> str:
    """4-4-4-4 分组展示（申请成功后对本人展示一次完整卡号）。"""
    d = normalize_card_input(card_no or "")
    return " ".join(d[i:i + 4] for i in range(0, len(d), 4))


def mask_card_no(card_no: str) -> str:
    """掩码展示：前 4 + 后 4（如 6102 **** **** 0343）。"""
    d = normalize_card_input(card_no or "")
    if len(d) < 8:
        return "****"
    return f"{d[:4]} **** **** {d[-4:]}"


def tail_of(card_no: str) -> str:
    d = normalize_card_input(card_no or "")
    return d[-4:] if len(d) >= 4 else ""


# --------------------------------------------------------------------------- #
#  卡号生成与查重
# --------------------------------------------------------------------------- #
def card_no_taken(cur, card_no: str) -> bool:
    """卡号是否已被占用（accounts.card_no / cards.card_no_full / payees.card_no 三处查重）。

    ★ 只查 accounts 是不够的：挂靠模式的卡有自己的独立卡号，不在 accounts 里。
    ★ payees.card_no 也纳入查重：收款人簿里的卡号若与某个真实账户撞号，
      会出现"给张三转钱却进了李四账户"的灾难性错配，生成时必须避开。
    """
    if not card_no:
        return True
    cur.execute("SELECT 1 FROM accounts WHERE card_no=%s LIMIT 1", (card_no,))
    if cur.fetchone():
        return True
    cur.execute("SELECT 1 FROM cards WHERE card_no_full=%s LIMIT 1", (card_no,))
    if cur.fetchone():
        return True
    cur.execute("SELECT 1 FROM payees WHERE card_no=%s LIMIT 1", (card_no,))
    return bool(cur.fetchone())


def gen_unique_card_no(cur, bin_prefix: str = CARD_BIN, tries: int = 50) -> str:
    """生成 16 位、Luhn 合法且全局唯一的卡号（冲突重生成）。

    随机空间 10^7，冲突概率极低；仍按需求「generate 后查重，冲突重生成」实现，
    并给足重试次数，耗尽才报错（正常不可能发生）。
    """
    for _ in range(tries):
        no = generate_card_no(bin_prefix)
        if not card_no_taken(cur, no):
            return no
    raise CardApplyError("卡号生成失败（随机重试耗尽），请稍后再试", code="internal")


# --------------------------------------------------------------------------- #
#  卡片 / 账户查询
# --------------------------------------------------------------------------- #
def card_counts(cur, uid: int) -> dict:
    """当前用户持有的有效卡数（已注销不计）。"""
    cur.execute(
        "SELECT card_type, COUNT(*) AS n FROM cards "
        "WHERE user_id=%s AND status<>'deleted' GROUP BY card_type",
        (int(uid),),
    )
    out = {TYPE_DEBIT: 0, TYPE_CREDIT: 0}
    for r in (cur.fetchall() or []):
        out[str(r["card_type"])] = int(r["n"])
    return out


def account_card_count(cur, account_id: int) -> int:
    cur.execute(
        "SELECT COUNT(*) AS n FROM cards WHERE account_id=%s AND status<>'deleted'",
        (int(account_id),),
    )
    return int((cur.fetchone() or {}).get("n") or 0)


def primary_account_id(cur, uid: int) -> int | None:
    """主账户 id：优先 is_primary=1，缺省取该用户 id 最小的储蓄账户。

    ★ 兜底逻辑必须与 /api/transfer 的扣款账户口径一致（ORDER BY id ASC），
      否则「归集到主账户」归到 A 账户、转账却从 B 账户扣，用户会看到钱"不见了"。
    """
    cur.execute(
        "SELECT id FROM accounts WHERE user_id=%s AND is_primary=1 "
        "AND COALESCE(account_type,'debit') <> 'credit' ORDER BY id ASC LIMIT 1",
        (int(uid),),
    )
    row = cur.fetchone()
    if row:
        return int(row["id"])
    cur.execute(
        "SELECT id FROM accounts WHERE user_id=%s AND COALESCE(account_type,'debit') <> 'credit' "
        "ORDER BY id ASC LIMIT 1",
        (int(uid),),
    )
    row = cur.fetchone()
    return int(row["id"]) if row else None


def list_accounts(cur, uid: int) -> list[dict]:
    """用户名下账户（含掩码卡号、尾号、挂卡数、主账户标记），按 id 升序。"""
    cur.execute(
        "SELECT a.id, a.user_id, a.name, a.card_no, a.balance, a.account_type, "
        "a.is_primary, "
        "(SELECT COUNT(*) FROM cards c WHERE c.account_id=a.id AND c.status<>'deleted') AS card_count "
        "FROM accounts a WHERE a.user_id=%s ORDER BY a.id ASC",
        (int(uid),),
    )
    out = []
    for r in (cur.fetchall() or []):
        item = dict(r)
        item["is_primary"] = bool(r["is_primary"])
        item["card_no_masked"] = mask_card_no(r["card_no"]) if r["card_no"] else None
        item["card_tail"] = tail_of(r["card_no"]) if r["card_no"] else None
        item["card_count"] = int(r["card_count"] or 0)
        item["can_bind_more"] = item["card_count"] < rules.get_rule("cards_per_account_max")
        out.append(item)
    # ★ 兜底：is_primary 是存量迁移补的标记，历史路径建的用户可能一个都没打。
    #   响应层保证「至少有一个主账户」（取 id 最小者，与 /api/transfer 的
    #   ORDER BY id ASC LIMIT 1 口径一致），否则前端「归集到主账户」会没有目标。
    if out and not any(a["is_primary"] for a in out):
        out[0]["is_primary"] = True
    return out


def owned_account(cur, uid: int, account_id: int, for_update: bool = False) -> dict:
    """账户归属校验（★ 传别人的 account_id 必须拒绝，不是查到就用）。"""
    sql = ("SELECT id, user_id, name, card_no, balance, account_type, is_primary "
           "FROM accounts WHERE id=%s")
    if for_update:
        sql += " FOR UPDATE"
    cur.execute(sql, (int(account_id),))
    row = cur.fetchone()
    if not row or int(row["user_id"]) != int(uid):
        raise CardApplyError(f"账户 {account_id} 不存在或不属于当前用户", code="forbidden")
    return dict(row)


def ensure_credit_account(cur, uid: int, name: str | None = None) -> dict:
    """取当前用户的信用账户；没有就建一个（每人一个，额度默认 ¥5,000）。

    ★ 信用账户 card_no = NULL：它不是一个可收付的存款账户，
      卡号反查（resolve_payee / 新增收款人）必须找不到它 —— 信用卡只展示不转账。
    """
    cur.execute(
        "SELECT id, user_id, name, card_no, balance, account_type, is_primary "
        "FROM accounts WHERE user_id=%s AND account_type='credit' "
        "ORDER BY id ASC LIMIT 1 FOR UPDATE",
        (int(uid),),
    )
    row = cur.fetchone()
    if row:
        return dict(row)
    cur.execute(
        "INSERT INTO accounts (user_id, name, card_no, balance, account_type, is_primary) "
        "VALUES (%s, %s, NULL, %s, 'credit', 0)",
        (int(uid), name or CREDIT_ACCOUNT_NAME,
         rules.get_rule("card_default_credit_limit")),
    )
    return {
        "id": int(cur.lastrowid), "user_id": int(uid),
        "name": name or CREDIT_ACCOUNT_NAME, "card_no": None,
        "balance": rules.get_rule("card_default_credit_limit"),
        "account_type": "credit", "is_primary": 0,
    }


# --------------------------------------------------------------------------- #
#  主流程：申请新卡
# --------------------------------------------------------------------------- #
def apply_card(cur, uid: int, card_name: str | None, card_type: str,
               account_id: int | None = None) -> dict:
    """申请新卡（单事务内的全部写操作，调用方负责 commit / rollback）。

    card_type = debit：
        account_id 为空 → 开新账户（name = 卡面名，balance = 0，card_no = 新生成）
        account_id 非空 → 挂靠该账户（校归属 + 该账户 ≤2 张卡）
    card_type = credit：
        复用 / 创建信用账户（每人一个，共享额度），卡号独立，额度默认 ¥5,000
    """
    ctype = (card_type or "").strip().lower()
    if ctype not in VALID_CARD_TYPES:
        raise CardApplyError(f"卡类型非法：{card_type}（可选 {list(VALID_CARD_TYPES)}）")

    name = (card_name or "").strip() or (
        "微众信用卡" if ctype == TYPE_CREDIT else "微众储蓄卡")
    uid = int(uid)
    counts = card_counts(cur, uid)

    if ctype == TYPE_DEBIT:
        if counts[TYPE_DEBIT] >= rules.get_rule("debit_card_max"):
            raise CardApplyError(
                f"已达储蓄卡上限（{rules.get_rule('debit_card_max')} 张）：您当前持有 "
                f"{counts[TYPE_DEBIT]} 张储蓄卡、{counts[TYPE_CREDIT]} 张信用卡。"
                f"如需办理新卡，请先注销不用的卡", code="limit")
        if account_id is not None:
            # ---- 挂靠已有账户 ----
            acct = owned_account(cur, uid, account_id, for_update=True)
            if acct.get("account_type") == "credit":
                raise CardApplyError("信用账户只能绑定信用卡，请选择储蓄账户挂靠")
            bound = account_card_count(cur, acct["id"])
            if bound >= rules.get_rule("cards_per_account_max"):
                raise CardApplyError(
                    f"账户「{acct['name']}」已绑定 {bound} 张卡（单账户上限 "
                    f"{rules.get_rule('cards_per_account_max')} 张），请换一个账户或选择开新账户", code="limit")
            card_no = gen_unique_card_no(cur)
            mode = "linked"
        else:
            # ---- 开新账户 ----
            card_no = gen_unique_card_no(cur)
            cur.execute(
                "INSERT INTO accounts (user_id, name, card_no, balance, account_type, is_primary) "
                "VALUES (%s, %s, %s, 0.00, 'debit', 0)",
                (uid, name, card_no),
            )
            acct = {"id": int(cur.lastrowid), "user_id": uid, "name": name,
                    "card_no": card_no, "balance": Decimal("0.00"),
                    "account_type": "debit", "is_primary": 0}
            mode = "new_account"
        credit_limit = Decimal("0.00")
        available_limit = Decimal("0.00")
    else:
        # ---- 信用卡：挂信用账户 ----
        if counts[TYPE_CREDIT] >= rules.get_rule("credit_card_max"):
            raise CardApplyError(
                f"已达信用卡上限（{rules.get_rule('credit_card_max')} 张）：您当前持有 "
                f"{counts[TYPE_DEBIT]} 张储蓄卡、{counts[TYPE_CREDIT]} 张信用卡。"
                f"如需办理新卡，请先注销不用的卡", code="limit")
        acct = ensure_credit_account(cur, uid)
        card_no = gen_unique_card_no(cur)
        credit_limit = rules.get_rule("card_default_credit_limit")
        available_limit = credit_limit
        mode = "credit"

    # ---- 建卡：关联账户 + 独立卡号 + 激活态 ----
    # ★ status='normal' = 需求里的「active 激活态」（沿用既有枚举，冻结/解冻/注销
    #   全链路依赖它，见模块头注释）。
    cur.execute(
        "INSERT INTO cards (user_id, account_id, card_no, card_no_full, card_name, "
        "card_type, tail, bank, credit_limit, available_limit, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'normal')",
        (uid, acct["id"], mask_card_no(card_no), card_no, name, ctype,
         tail_of(card_no), LOCAL_BANK_NAME, credit_limit, available_limit),
    )
    card_id = int(cur.lastrowid)

    return {
        "card_id": card_id,
        "card_name": name,
        "card_type": ctype,
        "mode": mode,
        "card_no": card_no,                       # ★ 完整卡号：仅本次响应下发
        "card_no_grouped": group_card_no(card_no),  # 4-4-4-4 一次性展示
        "card_no_masked": mask_card_no(card_no),
        "tail": tail_of(card_no),
        "bank": LOCAL_BANK_NAME,
        "status": "normal",
        "account_id": int(acct["id"]),
        "account_name": acct["name"],
        "account_type": acct.get("account_type") or "debit",
        "account_balance": float(acct.get("balance") or 0),
        "credit_limit": float(credit_limit),
        "available_limit": float(available_limit),
        "debit_count": counts[TYPE_DEBIT] + (1 if ctype == TYPE_DEBIT else 0),
        "credit_count": counts[TYPE_CREDIT] + (1 if ctype == TYPE_CREDIT else 0),
        "debit_max": rules.get_rule("debit_card_max"),
        "credit_max": rules.get_rule("credit_card_max"),
    }


# =========================================================================== #
#  HTTP 路由（组 C+ 从 mock_bank.main 抽离 —— 路由与卡域同处）
#  ★ 顶层不 import mock_bank.main：共享 helper（owned / fail / json_safe / to_decimal）
#    在各路由函数内**惰性 import**（复用组 T 模式，避免循环导入）。
#  ★ current_uid 走 utils/deps.py（单一归属，security 保持 FastAPI-free）。
# =========================================================================== #
from fastapi import APIRouter, Depends, HTTPException, Query  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from mock_bank import db  # noqa: E402
from utils import settlement_engine  # noqa: E402
from utils.deps import current_uid  # noqa: E402
from utils.op_auth import RequireOpToken  # noqa: E402

router = APIRouter()


@router.get("/api/card/validate")
def validate_card(
    card_no: str = Query(..., description="待校验卡号（自动清洗空格 / 连字符）"),
    uid: int = Depends(current_uid),
):
    """卡号即时校验（前端新增收款人「4-4-4-4 分体输入」失焦时调用）。

    ★ 只做格式与银行归属校验（Luhn + BIN 识别银行），不校验账户是否存在
      —— 账户存在性由 /api/payees/create（微众反查）与 /api/payees/resolve 判定。
    ★ 校验逻辑复用 utils.card_validator，前端不重复实现 Luhn / BIN 表，
      避免前后端口径漂移（这是"卡号有效性"的唯一真相源）。
    """
    digits = normalize_card_input(card_no)
    if len(digits) < 12:
        return {"ok": True, "valid": False, "message": "请输入完整的 16 位卡号"}
    if not luhn_check(digits):
        return {"ok": True, "valid": False, "message": "卡号有误，请核对后重试"}
    code = bank_from_card(digits)
    if not code:
        return {"ok": True, "valid": False, "message": "无法识别该卡号所属银行"}
    row = db.fetch_one(
        "SELECT id, bank_name, bank_code, is_local FROM bank_directory WHERE bank_code=%s",
        (code,),
    )
    if not row:
        return {"ok": True, "valid": False, "message": "该卡号对应的银行不在银行目录中"}
    return {
        "ok": True,
        "valid": True,
        "bank_id": int(row["id"]),
        "bank_name": row["bank_name"],
        "bank_code": code,
        "is_local": bool(row["is_local"]),
        "message": f"卡号有效（{row['bank_name']}）",
    }




@router.get("/api/cards")
def list_cards(uid: int = Depends(current_uid)):
    """银行卡列表（已软删除的 status='deleted' 不返回）。

    ★ 卡片-账户一体化：每张卡都带出「关联账户名 + 账户余额」，
      前端卡片页据此展示「这张卡背后的账户有多少钱」（Amount 遮蔽体系适配）。
    ★ 完整卡号（card_no_full）绝不出现在列表里 —— 列表只看尾号与掩码，
      完整卡号仅在「申请成功」时的响应里对本人展示一次。
    ★ 同时返回该用户的持卡上限与已用数量，前端据此在达到上限时置灰按钮。
    """
    from mock_bank.main import json_safe, fail  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）
    try:
        # ★ 惰性 housekeeping：卡片页展示的账户余额要反映最新清算结果
        #   （在途扣款 / 退票退回），进本接口先触发一次。
        try:
            settlement_engine.process_housekeeping(uid)
        except Exception:  # noqa: BLE001
            pass
        rows = db.fetch_all(
            "SELECT c.id, c.user_id, c.account_id, c.card_no, c.card_name, c.card_type, "
            "c.tail, c.bank, c.credit_limit, c.available_limit, c.status, c.created_at, "
            "a.name AS account_name, a.balance AS account_balance, "
            "a.account_type AS account_type, a.is_primary AS account_is_primary, "
            "a.card_no AS account_card_no "
            "FROM cards c LEFT JOIN accounts a ON a.id = c.account_id "
            "WHERE c.user_id=%s AND c.status <> 'deleted' ORDER BY c.id",
            (uid,),
        )
        cards = []
        for r in rows:
            item = json_safe(r)
            # 掩码 / 尾号：账户卡号优先（卡与账户同号），否则用卡自身的掩码
            item["card_no_masked"] = mask_card_no(
                r["account_card_no"] or r["card_no"])
            item["account_card_tail"] = (tail_of(r["account_card_no"])
                                        if r["account_card_no"] else item.get("tail"))
            # ★ 完整卡号绝不出现在列表里（上面只借它算掩码/尾号，算完即删）
            item.pop("account_card_no", None)
            item["account_is_primary"] = bool(r.get("account_is_primary"))
            item["account_type"] = r.get("account_type") or "debit"
            if item.get("card_type") == "credit":
                limit = float(r["credit_limit"] or 0)
                avail = float(r["available_limit"] or 0)
                item["used_limit"] = round(limit - avail, 2)
                item["usage_ratio"] = round((limit - avail) / limit, 4) if limit else 0.0
                item["is_demo_only"] = True   # 信用卡仅展示不转账（前端注明"演示中"）
            else:
                item["used_limit"] = 0.0
                item["usage_ratio"] = 0.0
                item["is_demo_only"] = False
            cards.append(item)
        counts = {"debit": 0, "credit": 0}
        for c in cards:
            counts[str(c.get("card_type"))] = counts.get(str(c.get("card_type")), 0) + 1
        return {
            "ok": True,
            "count": len(cards),
            "cards": cards,
            "limits": {
                "debit": {"used": counts.get("debit", 0), "max": rules.get_rule("debit_card_max")},
                "credit": {"used": counts.get("credit", 0), "max": rules.get_rule("credit_card_max")},
            },
        }
    except Exception as exc:  # noqa: BLE001
        return fail(f"查询银行卡失败：{exc}")






class CardApplyReq(BaseModel):
    name: str | None = Field(default=None, description="卡面名称，缺省自动生成")
    type: str = Field(default="debit", description="debit=储蓄卡 credit=信用卡")
    account_id: int | None = Field(
        default=None,
        description="储蓄卡可选：挂靠已有账户（不传=开新账户）。信用卡忽略该字段。")


@router.post("/api/cards/apply")
def apply_card_endpoint(req: CardApplyReq, uid: int = Depends(current_uid)):
    """申请新卡：卡片必须挂靠真实账户，开出来的卡是「有账户、有卡号、能收付」的真卡。

    ★ 设计原则（见 utils/card.py 头注释）：
      卡是账户的入口，不是装饰记录。没有账户的卡片不允许存在。

    执行（单事务，全部写在 utils/card.apply_card 里）：
      1) 储蓄卡(debit)
         - 默认开新账户：accounts 新增（name=卡面名，balance=0，
           card_no=generate_card_no() 生成的合法卡号）→ cards 关联该账户；
         - 传了 account_id：挂靠指定账户（校验归属当前用户，
           同一账户最多绑 2 张卡，超了提示）。
      2) 信用卡(credit)
         - 复用 / 创建信用账户（每人一个 credit 账户，额度默认 ¥5,000，
           余额=可用额度）→ cards 独立卡号，limit_amount=5000；
         - ★ 信用卡不参与收付轨道（余额=可用额度，语义不同），只展示不转账。
      3) 卡号唯一性：生成后查重（accounts.card_no + cards.card_no_full），冲突重生成。
      4) 留痕：operation_logs(action='card_apply', detail=卡面名+类型+卡号尾号)。
      5) 防滥用：单用户储蓄卡 ≤3 张、信用卡 ≤2 张，超出拒绝并提示现有卡数。
      6) 返回完整卡片信息（含完整卡号 —— 仅申请人本人本次可见，
         列表接口 /api/cards 只下发尾号与掩码）。

    ★ 卡状态用 'normal' 表达「已激活 / 使用中」（= 需求里的 active 语义），
      与既有冻结/解冻/注销枚举保持一致，不另起 'active'。
    ★ 纯展示性开户操作（不涉及资金变动），前端可直接调；额度调整 / 冻结
      涉及资金与风险，仍走对话流程（见 write_flow）。
    """
    from mock_bank.main import fail  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                info = apply_card(
                    cur, uid, req.name, req.type, req.account_id,
                )
                # 留痕：卡面名 + 类型 + 卡号尾号（与需求口径一致）
                db.log_operation(
                    uid, "card_apply",
                    {"card_id": info["card_id"], "card_name": info["card_name"],
                     "type": info["card_type"], "card_tail": info["tail"],
                     "mode": info["mode"], "account_id": info["account_id"],
                     "account_name": info["account_name"],
                     "credit_limit": info["credit_limit"]},
                    "success", cur=cur,
                )
            conn.commit()
        except CardApplyError as exc:
            conn.rollback()
            # 被拒也要留痕（上限 / 归属 / 账户类型）：安全中心要能看到被拦下的申请，
            # 否则「不断尝试开第 4 张卡」这类行为在审计里完全隐身。
            try:
                db.log_operation(uid, "card_apply",
                                 {"card_name": (req.name or ""), "type": req.type,
                                  "account_id": req.account_id,
                                  "reason": exc.code, "error": exc.message}, "failed")
            except Exception:  # noqa: BLE001
                pass
            _apply_code = {
                "limit": ERR.CARD_LIMIT_REACHED,
                "forbidden": ERR.CARD_BELONGS_TO_OTHER,
            }.get(exc.code, ERR.APPLY_CARD_FAILED)
            return fail(exc.message, code=_apply_code, action="apply_card")
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "card_apply",
                                 {"card_name": (req.name or ""), "type": req.type}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"申请新卡失败：{exc}", code=ERR.APPLY_CARD_FAILED, action="apply_card")

    mode_txt = {
        "new_account": f"已开立新账户「{info['account_name']}」并绑定该卡",
        "linked": f"已挂靠到您的账户「{info['account_name']}」",
        "credit": f"已绑定信用账户「{info['account_name']}」",
    }.get(info["mode"], "")
    credit_txt = (f"，共享信用额度 ¥{info['credit_limit']:,.2f}"
                  f"（可用 ¥{info['available_limit']:,.2f}）"
                  if info["card_type"] == "credit" else "")
    left = (f"您当前持有 {info['debit_count']}/{info['debit_max']} 张储蓄卡、"
            f"{info['credit_count']}/{info['credit_max']} 张信用卡")

    return {
        "ok": True,
        **info,
        "message": f"已成功申请「{info['card_name']}」，{mode_txt}{credit_txt}。{left}。"
                   f"请妥善保管卡号。",
    }




class CardAdjustLimitReq(BaseModel):
    card_id: int
    limit_amount: float


@router.put("/api/cards/adjust_limit")
def adjust_card_limit(req: CardAdjustLimitReq, uid: int = Depends(current_uid)):
    """调整信用卡额度：归属校验 → 范围校验（500~50000）→ 更新 → 留痕。

    涉及资金风险，正常路径仍由对话工作流驱动；这里只做能力兜底 + 强制归属校验。
    """
    from mock_bank.main import owned, to_decimal, fail  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）
    try:
        limit = to_decimal(req.limit_amount)
    except ValueError as exc:
        return fail(str(exc), code=ERR.BAD_REQUEST, action="adjust_card_limit")
    limit_max = rules.get_rule("card_limit_max")
    if limit < rules.get_rule("card_default_credit_limit") or limit > limit_max:
        return fail(f"额度需在 {rules.get_rule('card_default_credit_limit'):.0f} ~ {limit_max:.0f} 元之间",
                    code=ERR.CARD_LIMIT_RANGE, action="adjust_card_limit")

    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                card = owned(cur, "cards", req.card_id, uid, for_update=True)
                if card["status"] == "deleted":
                    conn.rollback()
                    return fail("该卡已注销，无法调整额度",
                                code=ERR.CARD_DELETED, action="adjust_card_limit")
                if card["card_type"] != "credit":
                    conn.rollback()
                    return fail("储蓄卡没有信用额度，无法调整",
                                code=ERR.CARD_NOT_CREDIT, action="adjust_card_limit")
                if card["status"] == "frozen":
                    conn.rollback()
                    return fail("卡片已冻结，请先解冻再调整额度",
                                code=ERR.CARD_FROZEN, action="adjust_card_limit")

                old_limit = Decimal(str(card["credit_limit"] or 0))
                # 已用额度保持不变，可用额度随总额度同增同减
                used = old_limit - Decimal(str(card["available_limit"] or 0))
                new_available = limit - used
                if new_available < 0:
                    conn.rollback()
                    return fail(f"已用额度 {used:.2f} 元超过目标额度 {limit:.2f} 元，无法下调",
                                code=ERR.CARD_USED_LIMIT, action="adjust_card_limit")

                cur.execute(
                    "UPDATE cards SET credit_limit=%s, available_limit=%s WHERE id=%s",
                    (limit, new_available, req.card_id),
                )
                db.log_operation(
                    uid, "card_adjust_limit",
                    {"card_id": req.card_id, "card_name": card["card_name"],
                     "from": float(old_limit), "to": float(limit)},
                    "success", cur=cur,
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "card_adjust_limit", {"card_id": req.card_id}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"调整额度失败：{exc}", code=ERR.ADJUST_LIMIT_FAILED,
                        action="adjust_card_limit")

    return {
        "ok": True,
        "card_id": req.card_id,
        "card_name": card["card_name"],
        "credit_limit": float(limit),
        "available_limit": float(new_available),
        "message": f"「{card['card_name']}」额度已调整为 {limit:.2f} 元",
    }


class CardIdReq(BaseModel):
    card_id: int


@router.post("/api/cards/freeze")
def freeze_card(req: CardIdReq, uid: int = Depends(current_uid),
               _: None = Depends(RequireOpToken("freeze_card"))):
    """冻结银行卡：归属校验（非本人 403）→ status=frozen → 留痕。"""
    from mock_bank.main import owned, fail  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                card = owned(cur, "cards", req.card_id, uid)
                if card["status"] == "deleted":
                    conn.rollback()
                    return fail("该卡已注销，无法冻结", code=ERR.CARD_DELETED, action="freeze_card")
                if card["status"] == "frozen":
                    conn.rollback()
                    return {"ok": True, "card_id": req.card_id, "status": "frozen",
                            "message": f"银行卡（{card['bank']}）已处于冻结状态"}
                cur.execute("UPDATE cards SET status='frozen' WHERE id=%s", (req.card_id,))
                db.log_operation(uid, "freeze_card",
                                 {"card_id": req.card_id, "card_name": card["card_name"]},
                                 "success", cur=cur)
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "freeze_card", {"card_id": req.card_id}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"冻结失败：{exc}", code=ERR.CARD_OP_FAILED, action="freeze_card")
    return {"ok": True, "card_id": req.card_id, "status": "frozen",
            "message": f"银行卡（{card['bank']}）已冻结"}


@router.post("/api/cards/unfreeze")
def unfreeze_card(req: CardIdReq, uid: int = Depends(current_uid),
                 _: None = Depends(RequireOpToken("unfreeze_card"))):
    """解冻银行卡：归属校验（非本人 403）→ status=normal → 留痕。"""
    from mock_bank.main import owned, fail  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                card = owned(cur, "cards", req.card_id, uid)
                if card["status"] == "deleted":
                    conn.rollback()
                    return fail("该卡已注销，无法解冻", code=ERR.CARD_DELETED, action="unfreeze_card")
                if card["status"] == "normal":
                    conn.rollback()
                    return {"ok": True, "card_id": req.card_id, "status": "normal",
                            "message": f"银行卡（{card['bank']}）已处于正常状态"}
                cur.execute("UPDATE cards SET status='normal' WHERE id=%s", (req.card_id,))
                db.log_operation(uid, "unfreeze_card",
                                 {"card_id": req.card_id, "card_name": card["card_name"]},
                                 "success", cur=cur)
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "unfreeze_card", {"card_id": req.card_id}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"解冻失败：{exc}", code=ERR.CARD_OP_FAILED, action="unfreeze_card")
    return {"ok": True, "card_id": req.card_id, "status": "normal",
            "message": f"银行卡（{card['bank']}）已解冻"}


@router.delete("/api/cards/{card_id}")
def delete_card(card_id: int, uid: int = Depends(current_uid)):
    """注销银行卡（软删除）：归属校验 → 无在途业务才可删 → status='deleted' + deleted_at → 留痕。

    ★ 与"冻结"的区别：冻结是可逆的风控动作，注销是生命周期终结。
    ★ 不可删的"关联进行中业务"：
        - status='lost'（挂失中，有未结的挂失/补卡流程）
        - 信用卡且存在已用额度（available_limit < credit_limit，账单未结清）
    """
    from mock_bank.main import owned, fail  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                card = owned(cur, "cards", card_id, uid, for_update=True)
                if card["status"] == "deleted":
                    conn.rollback()
                    return {"ok": True, "card_id": card_id, "status": "deleted",
                            "message": "该卡已注销，无需重复操作",
                            "action": "delete_card", "code": "SUCCESS"}
                if card["status"] == "lost":
                    conn.rollback()
                    return fail("卡片处于挂失中，请先解除挂失再注销",
                                code=ERR.CARD_LOST, action="delete_card")
                used = Decimal(str(card["credit_limit"] or 0)) - Decimal(str(card["available_limit"] or 0))
                if used > 0:
                    conn.rollback()
                    return fail(f"该卡尚有 {used:.2f} 元已用额度未结清，无法注销",
                                code=ERR.CARD_USED_LIMIT, action="delete_card")

                cur.execute(
                    "UPDATE cards SET status='deleted', deleted_at=NOW() WHERE id=%s", (card_id,)
                )
                db.log_operation(uid, "card_delete",
                                 {"card_id": card_id, "card_name": card["card_name"]},
                                 "success", cur=cur)
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            try:
                db.log_operation(uid, "card_delete", {"card_id": card_id}, "failed")
            except Exception:  # noqa: BLE001
                pass
            return fail(f"注销卡片失败：{exc}", code=ERR.CARD_OP_FAILED, action="delete_card")

    return {"ok": True, "card_id": card_id, "status": "deleted",
            "message": f"银行卡（{card['card_name']}）已注销"}
