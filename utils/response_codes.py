"""utils/response_codes.py —— 统一结构化响应协议：机器可读错误码（稳定契约）。

★ 用途：写操作（转账 / 理财 / 卡 / 订阅）回执与失败统一携带 `code` 字段，
  前端与 Agent 共用同一套语义，无需再解析中文 `message` 文案来判断"为什么失败"。

★ 铁律：
  - code 是**稳定字符串常量**，一旦对外（前端 / Agent）使用，不得变更其值；
    文案（message）可以改，code 不能改。
  - 所有 code 取值 = 其常量名（ERR.RISK_MISMATCH == "RISK_MISMATCH"），
    便于跨端比对与日志检索。
  - 新增业务拒绝分支，先来这里加常量，再到调用点 `fail(message, code=ERR.XXX)`。
  - 成功回执统一 code="SUCCESS"（由响应包裹中间件或 ok_response 注入）。
  - 未显式打码的业务失败，包裹层兜底为 "BUSINESS_ERROR"。

★ 命名约定：<域>_<原因>，全大写蛇形。
"""
from __future__ import annotations


class _Codes:
    """错误码命名空间（属性值 == 属性名）。"""

    # ---- 通用 ----
    SUCCESS = "SUCCESS"
    BUSINESS_ERROR = "BUSINESS_ERROR"          # 未显式打码的业务失败兜底
    FORBIDDEN = "FORBIDDEN"                    # 资源不属于当前用户（越权 403）
    NOT_FOUND = "NOT_FOUND"                     # 目标资源不存在
    BAD_REQUEST = "BAD_REQUEST"                 # 入参非法（缺字段 / 类型错）

    # ---- 幂等（Phase 4）----
    # 同一 Idempotency-Key 的第一笔仍在执行中（并发/重试撞车），本次未执行任何写操作
    IDEMPOTENT_IN_FLIGHT = "IDEMPOTENT_IN_FLIGHT"
    # 同一 Idempotency-Key 被复用于**不同**请求体（method+path+body 哈希不符）→ 拒绝
    IDEMPOTENT_KEY_REUSED = "IDEMPOTENT_KEY_REUSED"

    # ---- 转账 transfer ----
    TRANSFER_AMOUNT_INVALID = "TRANSFER_AMOUNT_INVALID"   # 金额 ≤ 0 / 非数字
    TRANSFER_LIMIT_SINGLE = "TRANSFER_LIMIT_SINGLE"       # 单笔超过限额
    TRANSFER_LIMIT_DAILY = "TRANSFER_LIMIT_DAILY"         # 当日累计超过限额
    TRANSFER_INSUFFICIENT = "TRANSFER_INSUFFICIENT"       # 余额不足
    TRANSFER_FAILED = "TRANSFER_FAILED"                   # 转账事务异常兜底

    # ---- 理财 wealth ----
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"     # 产品不存在
    PRODUCT_OFF = "PRODUCT_OFF"                 # 产品已下架
    BELOW_MIN_AMOUNT = "BELOW_MIN_AMOUNT"       # 低于起投金额
    RISK_MISMATCH = "RISK_MISMATCH"             # 风险不适配（已设偏好但仍超档）
    RISK_UNSET = "RISK_UNSET"                   # 风险未测评，仅可购 R1
    BUY_INSUFFICIENT = "BUY_INSUFFICIENT"       # 申购余额不足
    BUY_FAILED = "BUY_FAILED"                    # 申购事务异常兜底
    REDEEM_FORBIDDEN = "REDEEM_FORBIDDEN"       # 赎回越权（他人持仓）
    REDEEM_ALREADY = "REDEEM_ALREADY"           # 持仓已赎回
    REDEEM_EXCEED = "REDEEM_EXCEED"             # 赎回金额超出持仓
    REDEEM_FAILED = "REDEEM_FAILED"              # 赎回事务异常兜底

    # ---- 卡 card ----
    CARD_TYPE_INVALID = "CARD_TYPE_INVALID"     # 卡类型非法
    CARD_LIMIT_REACHED = "CARD_LIMIT_REACHED"   # 储蓄卡/信用卡达上限
    CARD_BELONGS_TO_OTHER = "CARD_BELONGS_TO_OTHER"  # 非本人卡（归属 403）
    CARD_NOT_CREDIT = "CARD_NOT_CREDIT"         # 储蓄卡无信用额度
    CARD_FROZEN = "CARD_FROZEN"                 # 卡片已冻结
    CARD_DELETED = "CARD_DELETED"               # 卡片已注销
    CARD_LOST = "CARD_LOST"                     # 卡片挂失中
    CARD_USED_LIMIT = "CARD_USED_LIMIT"         # 信用卡已用额度超目标额度
    CARD_LIMIT_RANGE = "CARD_LIMIT_RANGE"       # 额度超出可调范围
    CARD_ACCOUNT_MISMATCH = "CARD_ACCOUNT_MISMATCH"  # 挂靠账户类型不符
    APPLY_CARD_FAILED = "APPLY_CARD_FAILED"     # 开卡事务异常兜底
    ADJUST_LIMIT_FAILED = "ADJUST_LIMIT_FAILED" # 调额事务异常兜底
    CARD_OP_FAILED = "CARD_OP_FAILED"           # 冻结/解冻/注销事务异常兜底

    # ---- 订阅 subscription ----
    SUB_NAME_EMPTY = "SUB_NAME_EMPTY"           # 服务名为空
    SUB_CYCLE_INVALID = "SUB_CYCLE_INVALID"     # 计费周期非法
    SUB_AMOUNT_INVALID = "SUB_AMOUNT_INVALID"   # 费用 ≤ 0 / 非数字
    SUB_FIRST_DUE_INVALID = "SUB_FIRST_DUE_INVALID"  # 首次扣款日非法 / 不晚于今天
    SUB_NO_ACCOUNT = "SUB_NO_ACCOUNT"           # 名下无可用账户签约
    SUB_DUPLICATE = "SUB_DUPLICATE"             # 同商户已有生效订阅
    SUB_NOT_CANDIDATE = "SUB_NOT_CANDIDATE"     # 不是待确认/可忽略候选
    SUB_ALREADY_IGNORED = "SUB_ALREADY_IGNORED" # 已忽略
    SUB_CANCELLED = "SUB_CANCELLED"             # 已取消，不可改/确认
    SUB_ALREADY_ACTIVE = "SUB_ALREADY_ACTIVE"   # 已确认，无需重复
    SUB_NO_FIELDS = "SUB_NO_FIELDS"             # 修改未传任何字段
    SUBSCRIPTION_FORBIDDEN = "SUBSCRIPTION_FORBIDDEN"  # 订阅越权（他人订阅 403）
    CONTRACT_FAILED = "CONTRACT_FAILED"         # 签约事务异常兜底
    CONFIRM_FAILED = "CONFIRM_FAILED"           # 确认事务异常兜底
    IGNORE_FAILED = "IGNORE_FAILED"             # 忽略事务异常兜底
    UPDATE_FAILED = "UPDATE_FAILED"             # 修改事务异常兜底
    DELETE_FAILED = "DELETE_FAILED"             # 删除事务异常兜底
    CANCEL_FAILED = "CANCEL_FAILED"             # 取消事务异常兜底


# 单例命名空间：ERR.RISK_MISMATCH == "RISK_MISMATCH"
ERR = _Codes()


def all_codes() -> set[str]:
    """返回所有已注册 code 值（自检 / 文档用）。"""
    return {
        v for k, v in vars(_Codes).items()
        if not k.startswith("_") and isinstance(v, str)
    }
