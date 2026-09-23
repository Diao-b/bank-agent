"""scripts/_op_test_auth.py —— 回归测试共用：按写端点签发一次性操作票据（P0.2）。

生产路径：8178 的 write_flow 在"核实 / 支付密码通过"后调用 utils.op_auth.issue_op_token，
并把票据随请求头 X-Op-Token 转发给 8003 的写端点（transfer / buy / redeem / freeze /
unfreeze / cancel / transfer-internal）。测试脚本直连写端点时，用本模块**等价地签发一张
真实票据**（与生产同一实现），从而在不引入任何 bypass 的前提下让基线可跑 —— 即
"测试走真实 ticket 路径"。

用法（各测试脚本的 HTTP helper 内）：
    from scripts._op_test_auth import op_header
    headers.update(op_header(uid, path))
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import op_auth  # noqa: E402

# 写端点（精确路径）→ 票据场景；须与 mock_bank/main.py、utils/{wealth,card,subscription}.py
# 里 RequireOpToken("<scene>") 的场景名一一对应。
_EXACT = {
    "/api/transfer": "transfer",
    "/api/accounts/transfer-internal": "internal_transfer",
    "/api/products/buy": "buy_product",
    "/api/redeem": "redeem",
    "/api/cards/freeze": "freeze_card",
    "/api/cards/unfreeze": "unfreeze_card",
}


def scene_for(path: str) -> str | None:
    p = (path or "").split("?")[0]
    if p in _EXACT:
        return _EXACT[p]
    if p.startswith("/api/subscriptions/") and p.endswith("/cancel"):
        return "cancel_subscription"
    return None


def op_header(uid, path: str) -> dict:
    """非写端点 → {}（不加头）；写端点 → {"X-Op-Token": <一次性票据>}。"""
    scene = scene_for(path)
    if scene is None or uid is None:
        return {}
    try:
        return {"X-Op-Token": op_auth.issue_op_token(int(uid), scene)["token"]}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 操作票据签发失败（{path} 将被网关拒绝）: {exc}")
        return {}
