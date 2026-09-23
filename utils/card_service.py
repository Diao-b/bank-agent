"""utils/card_service.py —— 组 K 合并后的兼容 re-export 壳。

真实实现（卡片-账户一体化服务：开卡 / 卡号生成查重 / 账户归属与查询）已合并到
utils.card。保留本壳是为了零调用方改动：mock_bank.main / user_provision /
seed_card_account 仍 `from utils import card_service`，无需逐处改 import。
"""
from utils.card import (  # noqa: F401
    CARD_BIN, DEBIT_CARD_MAX, CREDIT_CARD_MAX, CARDS_PER_ACCOUNT_MAX,
    DEFAULT_CREDIT_LIMIT, CREDIT_ACCOUNT_NAME, LOCAL_BANK_NAME,
    TYPE_DEBIT, TYPE_CREDIT, VALID_CARD_TYPES, CardApplyError,
    group_card_no, mask_card_no, tail_of, card_no_taken, gen_unique_card_no,
    card_counts, account_card_count, primary_account_id, list_accounts,
    owned_account, ensure_credit_account, apply_card,
)
