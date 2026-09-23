"""utils/card_validator.py —— 组 K 合并后的兼容 re-export 壳。

真实实现（卡号校验纯函数：Luhn / BIN / 卡号生成）已合并到 utils.card。
保留本壳是为了零调用方改动：transfer_resolver / mock_bank.main / 多个 seed·test
脚本仍 `from utils import card_validator`，无需逐处改 import。
"""
from utils.card import (  # noqa: F401
    BIN_TABLE, BIN_TO_BANK, LOCAL_BANK_BIN, LOCAL_BANK_CODE,
    luhn_check, _luhn_digit, generate_card_no, generate_card_no_with_tail,
    bank_from_card, bin_of_bank, is_local_card, mask_real_name, normalize_card_input,
)
