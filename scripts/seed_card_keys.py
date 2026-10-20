"""seed_card_keys.py —— 生成演示卡密批次（供后台「卡密管理」Tab 展示）

幂等：批次号已存在则跳过。明文仅在此打印一次，库里只留 hash。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_bank import db
from utils import card_keys

SEED = [
    ("CKSEED001", Decimal("500"), 20, "演示批次：¥500 × 20"),
    ("CKSEED002", Decimal("1000"), 10, "演示批次：¥1000 × 10"),
]


def main() -> int:
    super_row = db.fetch_one("SELECT id FROM users WHERE username='superadmin'")
    if not super_row:
        print("未找到 superadmin 账号，请先运行 scripts/seed_roles.py")
        return 1
    super_uid = int(super_row["id"])

    for batch_no, amount, count, remark in SEED:
        exists = db.fetch_one(
            "SELECT batch_no FROM card_key_batches WHERE batch_no=%s", (batch_no,))
        if exists:
            print(f"  · 批次 {batch_no} 已存在，跳过")
            continue
        res = card_keys.generate_batch(amount, count, super_uid, remark,
                                       batch_no=batch_no)
        print(f"  · 已生成批次 {batch_no}：{count} 张 × ¥{amount}；"
              f"示例明文（仅此一次）：{res['codes'][0]} … {res['codes'][-1]}")
    print("完成。后台「卡密管理」Tab 可见批次（列表脱敏，明文不再出现）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
