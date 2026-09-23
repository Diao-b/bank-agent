"""rules.py —— 业务策略单源（C6 可运营后台升级：文件 → DB 表）

★ 单一来源变更（组 W 后续轮）：业务策略数值从"本文件常量"迁移到 MySQL
  `system_config` 表（key/value/label/范围/知识同步标记）。本文件是唯一读取入口：

    rules.get_rule(key)        —— 运行时读取（5s TTL 缓存，跨进程 ≤5s 收敛）
    rules.load_rules()         —— 启动/首次调用时把表里缺省的参数补种默认值
    rules.set_rule_value(...)  —— 管理端修改入口（校验范围 + 落库 + 刷新缓存）
    rules.all_rules()          —— 管理端参数列表（含 label / 允许范围 / 当前值）

  域文件（write_flow / verify_codes / paypass / card / settlement_engine /
  mock_bank）一律改走 get_rule 在调用点取值，禁止再 `from rules import 常量`
  做运行时判断 —— 那会在 import 时固化数值，管理端改参数不生效。

★ 转账风控阈值说明：真正生效的是 `agent/write_flow.py` 的 `_verify_queue()`
  （`risk_check` 用 `need = bool(queue)` 驱动 verify/confirm 路由），与
  docs/knowledge 保持一致。修改带 affects_knowledge 标记的参数时，管理端
  必须同步更新对应知识文档（admin_logs.pending_sync 强制化）。

边界说明：
  * 下方模块级常量仅作**默认值 + 向后兼容**保留（新代码请用 get_rule）；
  * 纯实现 / 识别类常量（BIN 表、订阅识别窗口等）不属"业务可调项"，
    留在各自域文件 / 本文件常量区，不入 system_config。
"""
from __future__ import annotations

import re
import threading
import time
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------- #
#  业务枚举（不随运营调整，不入 system_config）
# --------------------------------------------------------------------------- #
VALID_CYCLES = ("月付", "年付")
VALID_CARD_TYPES = ("debit", "credit")

# 登录防爆破（审计 R5 配套）：同账号连续失败 N 次锁 M 秒。
# 属安全策略数字 —— 单源在 rules.py（暂不入 system_config，进程内计数即可）。
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 900

# --------------------------------------------------------------------------- #
#  参数注册表：key → 默认值 / 元信息（管理端列表与 DB 补种均以此为准）
#  knowledge=True 的参数涉及 RAG 知识文档数字 —— 修改后强制"待同步"标记
# --------------------------------------------------------------------------- #
RULE_SPECS: dict[str, dict] = {
    "transfer_verify_threshold": {
        "default": 2000, "type": "int", "label": "转账验证阈值（超过需验证码+支付密码）",
        "min": 100, "max": 50000, "unit": "元", "knowledge": True, "docs": "transfer-rules.md",
    },
    "transfer_big_threshold": {
        "default": 20000, "type": "int", "label": "转账大额警示阈值",
        "min": 1000, "max": 200000, "unit": "元", "knowledge": True, "docs": "transfer-rules.md",
    },
    "transfer_big_unfrequent_threshold": {
        "default": 5000, "type": "int", "label": "非常用收款人大额警示阈值",
        "min": 1000, "max": 100000, "unit": "元", "knowledge": True, "docs": "transfer-rules.md",
    },
    "wealth_verify_threshold": {
        "default": 10000, "type": "int", "label": "理财大额核验阈值（超过需支付密码）",
        "min": 1000, "max": 200000, "unit": "元", "knowledge": True, "docs": "wealth-products.md",
    },
    "transfer_daily_limit": {
        "default": 50000, "type": "int", "label": "转账单笔 / 单日累计限额",
        "min": 1000, "max": 1000000, "unit": "元", "knowledge": True, "docs": "transfer-rules.md",
        # ★ 已落实强制校验（单笔与单日累计均取该值，默认 50000；
        #   改阈值走 set_rule_value，运行期即时生效）。口径与 docs/knowledge/transfer-rules.md 一致：
        #   已落账站内转账 / 已扣款跨行转账计入当日累计；退票当日返还；本人归集不计入。
        "enabled": True,
    },
    "paypass_max_attempts": {
        "default": 3, "type": "int", "label": "支付密码最大尝试次数（超过锁定）",
        "min": 1, "max": 10, "unit": "次", "knowledge": True, "docs": "account-security.md",
    },
    "verify_code_ttl_seconds": {
        "default": 300, "type": "int", "label": "验证码有效期",
        "min": 60, "max": 1800, "unit": "秒", "knowledge": True, "docs": "account-security.md",
    },
    "verify_code_max_attempts": {
        "default": 3, "type": "int", "label": "验证码最大尝试次数（超过作废）",
        "min": 1, "max": 10, "unit": "次", "knowledge": True, "docs": "account-security.md",
    },
    "debit_card_max": {
        "default": 3, "type": "int", "label": "储蓄卡持有上限",
        "min": 1, "max": 10, "unit": "张", "knowledge": False, "docs": "",
    },
    "credit_card_max": {
        "default": 2, "type": "int", "label": "信用卡持有上限",
        "min": 1, "max": 10, "unit": "张", "knowledge": False, "docs": "",
    },
    "cards_per_account_max": {
        "default": 2, "type": "int", "label": "单账户挂靠卡片上限",
        "min": 1, "max": 10, "unit": "张", "knowledge": False, "docs": "",
    },
    "card_default_credit_limit": {
        "default": Decimal("5000"), "type": "decimal", "label": "信用卡默认额度",
        "min": Decimal("500"), "max": Decimal("50000"), "unit": "元", "knowledge": False, "docs": "",
    },
    "card_limit_max": {
        "default": Decimal("50000"), "type": "decimal", "label": "信用卡调额上限",
        "min": Decimal("1000"), "max": Decimal("500000"), "unit": "元", "knowledge": False, "docs": "",
    },
    "settle_delay_minutes": {
        "default": 5, "type": "int", "label": "跨行清算演示时长",
        "min": 1, "max": 120, "unit": "分钟", "knowledge": False, "docs": "",
    },
}

# --------------------------------------------------------------------------- #
#  兼容别名：旧代码（及测试）仍可直接引用这些名字；运行时判断一律走 get_rule
# --------------------------------------------------------------------------- #
TRANSFER_VERIFY_THRESHOLD = 2000
TRANSFER_BIG_THRESHOLD = 20000
TRANSFER_BIG_UNFREQUENT_THRESHOLD = 5000
WEALTH_VERIFY_THRESHOLD = 10000
TRANSFER_DAILY_LIMIT = 50000       # ★ 已强制校验：单笔与单日累计均取此值（默认 50000），见 rules.RULE_SPECS
CARD_LIMIT_MIN = Decimal("500")
CARD_LIMIT_MAX = Decimal("50000")
CARD_DEFAULT_CREDIT_LIMIT = Decimal("5000.00")
DEBIT_CARD_MAX = 3
CREDIT_CARD_MAX = 2
CARDS_PER_ACCOUNT_MAX = 2
SETTLE_DELAY_MINUTES = 5
VERIFY_CODE_TTL_SECONDS = 300
VERIFY_CODE_MAX_ATTEMPTS = 3

# 订阅识别引擎（detect 窗口与阈值）—— 识别算法内部参数，非运营可调项
SCAN_MONTHS = 6
MIN_TXNS = 3
MONTHLY_MIN, MONTHLY_MAX = 26, 35
YEARLY_MIN, YEARLY_MAX = 350, 380
AMOUNT_STD_RATIO = 0.05
EXCLUDED_CATEGORIES = ("转账", "收入")

# --------------------------------------------------------------------------- #
#  DB 读取（懒加载 + TTL 缓存；DB 不可用时回退默认值，绝不阻断业务）
# --------------------------------------------------------------------------- #
_TTL_SECONDS = 5.0                      # 跨进程（run_server / mock_bank）收敛时延
_cache: dict[str, tuple[object, float]] = {}
_loaded = False
_lock = threading.Lock()


def _typed(spec: dict, raw):
    """按参数类型把 DB 字符串转成 int / Decimal；解析失败回退默认值。"""
    try:
        if spec["type"] == "decimal":
            return Decimal(str(raw))
        return int(Decimal(str(raw)))
    except (InvalidOperation, ValueError, TypeError):
        return spec["default"]


def _fetch_rule(key: str):
    """从 system_config 读单个参数（无行 → 补种默认值并返回默认）。"""
    spec = RULE_SPECS.get(key)
    if spec is None:
        return None
    try:
        from mock_bank import db  # 连接收口到单一源（组 R）
        row = db.fetch_one(
            "SELECT `value` FROM system_config WHERE `key`=%s", (key,)
        )
        if row is None:
            db.execute(
                "INSERT IGNORE INTO system_config (`key`, value, label, value_type,"
                " min_value, max_value, unit, affects_knowledge, docs_ref)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (key, str(spec["default"]), spec["label"], spec["type"],
                 str(spec["min"]), str(spec["max"]), spec["unit"],
                 1 if spec["knowledge"] else 0, spec["docs"]),
            )
            return spec["default"]
        return _typed(spec, row["value"])
    except Exception:  # noqa: BLE001 —— DB 不可用时业务不中断
        return spec["default"]


def load_rules() -> None:
    """启动 / 首次调用：补种缺省参数行并清空缓存（下一次 get_rule 全量重取）。"""
    global _loaded
    with _lock:
        _loaded = True
        _cache.clear()
        try:
            from mock_bank import db
            for key, spec in RULE_SPECS.items():
                db.execute(
                    "INSERT IGNORE INTO system_config (`key`, value, label, value_type,"
                    " min_value, max_value, unit, affects_knowledge, docs_ref)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (key, str(spec["default"]), spec["label"], spec["type"],
                     str(spec["min"]), str(spec["max"]), spec["unit"],
                     1 if spec["knowledge"] else 0, spec["docs"]),
                )
        except Exception:  # noqa: BLE001
            pass


def get_rule(key: str):
    """运行时读取业务参数（唯一入口）。未知 key 抛 KeyError。"""
    if key not in RULE_SPECS:
        raise KeyError(f"未知业务参数：{key}")
    if not _loaded:
        load_rules()
    hit = _cache.get(key)
    now = time.time()
    if hit is not None and now - hit[1] < _TTL_SECONDS:
        return hit[0]
    val = _fetch_rule(key)
    with _lock:
        _cache[key] = (val, now)
    return val


def refresh_rule(key: str) -> None:
    """管理端修改后强制刷新单参数缓存（本进程即时生效）。"""
    if key in RULE_SPECS:
        _cache[key] = (_fetch_rule(key), time.time())


def set_rule_value(key: str, raw_value, updated_by: int | None = None) -> dict:
    """管理端修改参数：越界拒绝 → 落库 → 刷新缓存。返回 old/new。

    ★ 越界即拒绝（每参数定义合理区间）；非法数值拒绝；未知 key 拒绝。
    """
    spec = RULE_SPECS.get(key)
    if spec is None:
        raise KeyError(f"未知业务参数：{key}")
    raw_value = str(raw_value).strip()
    if not re.fullmatch(r"\d+(\.\d+)?", raw_value):
        raise ValueError(f"参数 {key} 的值不是合法数值")
    new_val = _typed(spec, raw_value)
    lo, hi = Decimal(str(spec["min"])), Decimal(str(spec["max"]))
    nv = Decimal(str(new_val))
    if nv < lo or nv > hi:
        raise ValueError(
            f"「{spec['label']}」允许范围 {lo} ~ {hi} {spec['unit']}，{nv} 越界被拒绝"
        )
    old_val = get_rule(key)
    from mock_bank import db
    db.execute(
        "UPDATE system_config SET value=%s, updated_by=%s WHERE `key`=%s",
        (str(new_val), updated_by, key),
    )
    refresh_rule(key)
    return {"old": old_val, "new": new_val}


def all_rules() -> list[dict]:
    """管理端参数列表：注册表元信息 + DB 当前值 + 最后修改人/时间。"""
    if not _loaded:
        load_rules()
    out = []
    try:
        from mock_bank import db
        rows = {
            r["key"]: r for r in db.fetch_all(
                "SELECT `key`, value, updated_by, updated_at FROM system_config"
            )
        }
    except Exception:  # noqa: BLE001
        rows = {}
    for key, spec in RULE_SPECS.items():
        row = rows.get(key)
        value = _typed(spec, row["value"]) if row else spec["default"]
        out.append({
            "key": key,
            "label": spec["label"],
            "value": str(value),
            "type": spec["type"],
            "min": str(spec["min"]),
            "max": str(spec["max"]),
            "unit": spec["unit"],
            "affects_knowledge": bool(spec["knowledge"]),
            "enabled": bool(spec.get("enabled", True)),
            "docs_ref": spec["docs"],
            "updated_by": row.get("updated_by") if row else None,
            "updated_at": str(row.get("updated_at") or "") if row else "",
        })
    return out


def summary_for_prompt() -> str:
    """Agent 系统提示词用的规则要点（从 DB 实时取值 —— 管理端改完即生效）。"""
    try:
        return (
            "【当前系统风控参数（回答规则类问题时以这些数值为准，禁止凭记忆编造）】"
            f"转账单笔/单日累计限额 {get_rule('transfer_daily_limit')} 元；"
            f"单笔超过 {get_rule('transfer_verify_threshold')} 元（或非常用收款人）需验证码+支付密码双要素；"
            f"超过 {get_rule('transfer_big_threshold')} 元（非常用收款人超过 "
            f"{get_rule('transfer_big_unfrequent_threshold')} 元）附加大额警示；"
            f"理财购买/赎回超过 {get_rule('wealth_verify_threshold')} 元需支付密码；"
            f"支付密码错 {get_rule('paypass_max_attempts')} 次锁定至当日 23:59:59；"
            f"验证码 {get_rule('verify_code_ttl_seconds')} 秒内有效、"
            f"错 {get_rule('verify_code_max_attempts')} 次作废。"
        )
    except Exception:  # noqa: BLE001
        return ""
