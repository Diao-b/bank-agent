"""组 P：把理财路由从 mock_bank.main 抽到 utils/wealth.py（复用组 T 惰性模式）。"""
import re

MAIN = r"D:\Users\Dwm\Desktop\agaent\bank-agent\mock_bank\main.py"
WEALTH = r"D:\Users\Dwm\Desktop\agaent\bank-agent\utils\wealth.py"

lines = open(MAIN, encoding="utf-8").read().split("\n")


def find(pred, start=0):
    return next(i for i, l in enumerate(lines) if i >= start and pred(l))


P1 = (find(lambda l: l.startswith('@app.get("/api/products")')),
      find(lambda l: l.startswith('@app.get("/api/holdings")')))
P2 = (find(lambda l: l.startswith('@app.get("/api/holdings")')),
      find(lambda l: l.startswith('@app.get("/api/user")')))
P3 = (find(lambda l: l.startswith("#  写端点：理财产品购买")) - 1,
      find(lambda l: l.startswith("#  写端点：赎回")) - 1)
P4 = (find(lambda l: l.startswith("#  写端点：赎回")) - 1,
      find(lambda l: l.startswith("#  账户（卡片挂靠")) - 1)
CH = [P1, P2, P3, P4]
for s, e in CH:
    assert 0 <= s < e <= len(lines), (s, e)

picked: list[str] = []
for s, e in CH:
    picked.extend(lines[s:e])
    picked.extend(["", ""])

# 去掉 main 的 section 头/border
picked = [l for l in picked if not re.fullmatch(r"# =+ #", l.strip())
          and not l.strip().startswith("#  写端点：")]

text = re.sub(r"@app\.(get|post)\(", r"@router.\1(", "\n".join(picked))
block = text.split("\n")

LAZY = {
    "list_products": ["json_safe", "fail"],
    "list_holdings": ["json_safe", "fail"],
    "buy_product": ["to_decimal", "fail"],
    "redeem": ["to_decimal", "fail"],
}


def insert_lazy(blk: list[str], func: str, names: list[str]) -> list[str]:
    di = next(i for i, l in enumerate(blk) if l.startswith(f"def {func}("))
    si = di
    while not blk[si].rstrip().endswith(":"):
        si += 1
    j = si + 1
    ins = j
    st = blk[j].strip()
    if st.startswith('"""') or st.startswith("'''"):
        q = st[:3]
        if len(st) >= 6 and st.endswith(q):
            ins = j + 1
        else:
            k = j + 1
            while not blk[k].strip().endswith(q):
                k += 1
            ins = k + 1
    imp = ("    from mock_bank.main import " + ", ".join(names)
           + "  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）")
    return blk[:ins] + [imp] + blk[ins:]


for fn, names in LAZY.items():
    block = insert_lazy(block, fn, names)

header = '''"""utils/wealth.py —— 理财域（商品目录 + 持仓 + 申购 / 赎回）。

组 P（合并式重构，2026-09-11）：原内联于 mock_bank.main 的
  GET /api/products、GET /api/holdings、POST /api/products/buy、POST /api/redeem
整体抽入本文件，路由与理财域同处（`router = APIRouter()`），main 仅 include_router。

★ 依赖约定（复用组 T 惰性模式）：顶层不 import mock_bank.main；共享 helper
  （json_safe / fail / to_decimal）在各路由函数内惰性 import，避免循环导入。
★ products 是平台级商品目录（全局共享，不过滤 user_id）；用户资产在 holdings。
"""
from __future__ import annotations

import json
import random
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from mock_bank import db
from utils.deps import current_uid

router = APIRouter()


'''

open(WEALTH, "w", encoding="utf-8").write(header + "\n".join(block).strip("\n") + "\n")

# 从 main 删除（倒序）
nl = list(lines)
for s, e in sorted(CH, key=lambda t: t[0], reverse=True):
    del nl[s:e]
out: list[str] = []
for l in nl:
    if l == "" and len(out) >= 2 and out[-1] == "" and out[-2] == "":
        continue
    out.append(l)
new_src = "\n".join(out)

marker = "app.include_router(card_domain.router)  # 卡域路由（组 C+）"
assert marker in new_src
new_src = new_src.replace(
    marker,
    marker + "\n"
    "from utils import wealth as wealth_domain  # noqa: E402\n"
    "app.include_router(wealth_domain.router)  # 理财域路由（组 P）",
)
open(MAIN, "w", encoding="utf-8").write(new_src)
print("wealth.py 行数:", (header + "\n".join(block)).count("\n") + 1)
print("main.py 行数:", new_src.count("\n") + 1)
print("chunks:", CH)
print("done")
