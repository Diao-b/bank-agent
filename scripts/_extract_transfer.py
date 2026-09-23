"""组 T：生成 utils/transfer.py（transfer_resolver 整体并入 + main 的 /api/transfer 执行逻辑抽为 execute_transfer）。

做法（行为零变化）：
- resolver 主体逐字复制（其只依赖 db / card_validator，import 由本文件头提供）；
- main 的 transfer 函数体逐字复制，仅做最小符号替换（req.x → 形参、model_dump → payload、请求头 → inject）；
- 抽出单一轨道谓词 is_insite_track()，resolver 与 execute 共用（标签各自保留）；
- execute_transfer 内部惰性 import 共享 helper（owned/to_decimal/fail）以规避循环导入。
"""
MAIN = r"D:\Users\Dwm\Desktop\agaent\bank-agent\mock_bank\main.py"
RES = r"D:\Users\Dwm\Desktop\agaent\bank-agent\utils\transfer_resolver.py"
OUT = r"D:\Users\Dwm\Desktop\agaent\bank-agent\utils\transfer.py"

main_lines = open(MAIN, encoding="utf-8").read().split("\n")
res_lines = open(RES, encoding="utf-8").read().split("\n")

# ---- 1) resolver 主体：从第一个 "# ---" 分隔线到 EOF ----
res_start = next(i for i, l in enumerate(res_lines) if l.startswith("# ---"))
resolver_body = "\n".join(res_lines[res_start:])

# ---- 2) main 的 transfer 函数体 + _sender_name ----
t_start = next(i for i, l in enumerate(main_lines) if l.startswith("def transfer(req: TransferReq"))
s_start = next(i for i, l in enumerate(main_lines) if l.startswith("def _sender_name"))
body_lines = main_lines[t_start + 1: s_start]          # 函数体（含 docstring），不含 def 行
sender_lines = main_lines[s_start: s_start + 5]        # def _sender_name ... return
body = "\n".join(body_lines)
sender = "\n".join(sender_lines)

assert 'def transfer(req: TransferReq' in main_lines[t_start]
assert sender_lines[0].startswith("def _sender_name")
assert "request.headers.get(\"X-Test-Inject\")" in body
assert "if insite_target is not None:" in body

# ---- 3) 符号最小替换 ----
body = body.replace("req.from_account_id", "from_account_id")
body = body.replace("req.payee_id", "payee_id")
body = body.replace("req.amount", "amount_raw")
body = body.replace("req.model_dump()", "payload")
body = body.replace('request.headers.get("X-Test-Inject")', "inject")

# 惰性 import 共享 helper（放在 docstring 之后、首个 try 之前）
old_try = "\n    try:\n        amount = to_decimal(amount_raw)"
assert old_try in body
body = body.replace(
    old_try,
    "\n    # ★ 惰性 import：避开 utils.transfer ↔ mock_bank.main 的循环导入"
    "（本模块顶层刻意不 import main）\n"
    "    from mock_bank.main import owned, to_decimal, fail  # noqa: PLC0415\n"
    "\n    try:\n        amount = to_decimal(amount_raw)",
    1,
)

# 轨道判定收口为单一谓词
old_track = (
    "                if insite_target is not None:\n"
    "                    track = \"insite\"\n"
    "                else:\n"
    "                    track = \"offsite\""
)
assert old_track in body
body = body.replace(
    old_track,
    "                track = (\"insite\" if is_insite_track(linked_uid, insite_target is not None)\n"
    "                         else \"offsite\")",
)

# resolver 的 track 也用同一谓词
resolver_body = resolver_body.replace(
    'track = "insite" if p.get("linked_user_id") else "cross_bank"',
    'track = "insite" if is_insite_track(p.get("linked_user_id")) else "cross_bank"',
)
assert 'is_insite_track(p.get("linked_user_id"))' in resolver_body

header = '''"""utils/transfer.py —— 转账域单文件（resolve 收款人 + execute 执行）。

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
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException

import config
from mock_bank import db
from utils import card_validator, inbox, settlement_engine


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


'''

exec_sig = (
    "def execute_transfer(\n"
    "    uid: int,\n"
    "    from_account_id: int | None,\n"
    "    payee_id: int,\n"
    "    amount_raw: float,\n"
    "    payload: dict,\n"
    "    inject: str | None = None,\n"
    ") -> dict:\n"
)

out = header + resolver_body + "\n\n\n" + exec_sig + body + "\n\n\n" + sender + "\n"
open(OUT, "w", encoding="utf-8").write(out)
print("transfer.py 行数:", out.count("\n") + 1)
print("body 首行:", body_lines[0][:60])
print("done")
