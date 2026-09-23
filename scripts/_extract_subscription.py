"""组 S：把 main.py 的订阅路由块抽离到 utils/subscription.py（行为零变化）。

用内容锚点定位两块订阅代码（不依赖硬编码行号），装饰器 @app. -> @router. 逐字保留。
"""
import re

MAIN = r"D:\Users\Dwm\Desktop\agaent\bank-agent\mock_bank\main.py"
OUT = r"D:\Users\Dwm\Desktop\agaent\bank-agent\utils\subscription.py"

src = open(MAIN, encoding="utf-8").read()
lines = src.split("\n")

# ---- 锚点定位 ----
def find_block(start_marker, end_marker, end_inclusive=True):
    s = next(i for i, l in enumerate(lines) if l.startswith(start_marker))
    e = next(i for i, l in enumerate(lines) if l.startswith(end_marker) and i > s)
    if end_inclusive:
        e = e  # 保留 end_marker 行
    return s, e

# list_subscriptions 块
list_s = next(i for i, l in enumerate(lines) if l.startswith('@app.get("/api/subscriptions")'))
list_e = next(i for i, l in enumerate(lines)
              if l.rstrip() == '        return fail(f"查询订阅失败：{exc}")' and i > list_s)
# 订阅 CRUD 段：从 `#  订阅 CRUD` 注释头 到 ignore_subscription 末行
crud_s = next(i for i, l in enumerate(lines) if l.startswith("#  订阅 CRUD"))
crud_e = next(i for i, l in enumerate(lines)
              if l.rstrip().endswith('不再计入订阅支出"}') and i > crud_s)

list_block_raw = lines[list_s: list_e + 1]
crud_block_raw = lines[crud_s: crud_e + 1]

assert list_block_raw[0].startswith('@app.get("/api/subscriptions")'), list_block_raw[0]
assert crud_block_raw[0].startswith("#  订阅 CRUD"), crud_block_raw[0]
assert crud_block_raw[-1].rstrip().endswith('不再计入订阅支出"}'), crud_block_raw[-1]

def to_router(block):
    text = "\n".join(block)
    return re.sub(r'@app\.(get|post|put|delete)\(', r'@router.\1(', text).split("\n")

list_block = to_router(list_block_raw)
crud_block = to_router(crud_block_raw)

header = '''"""utils/subscription.py —— 订阅域（detect + deduct + 状态机 confirm/cancel/ignore）。

原 main.py 订阅路由整段抽离而来（组 S 合并式重构，行为零变化）。
装饰器挂在 `router = APIRouter()` 上，由 mock_bank.main 在模块末尾 include_router。
共享 helper（owned / json_safe / to_decimal / fail / current_uid / VALID_CYCLES /
add_months）定义在 mock_bank.main，include 发生在 main 模块加载末尾、各 helper 均已
定义之后，故此处顶部 import 不触发循环导入崩溃。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel, Field

from mock_bank import db
from utils import deduction_engine, subscription_detector
from mock_bank.main import (
    owned,
    json_safe,
    to_decimal,
    fail,
    current_uid,
    VALID_CYCLES,
    add_months,
)

router = APIRouter()
'''

subscription_src = header + "\n".join(list_block) + "\n\n" + "\n".join(crud_block) + "\n"

# ---- 从 main.py 删除两块（先删靠后的 crud，避免行号漂移）----
new_lines = list(lines)
del new_lines[crud_s: crud_e + 1]
# list 块索引在新列表中不变（在 crud 之前）
del new_lines[list_s: list_e + 1]
new_src = "\n".join(new_lines)

# ---- 在 if __name__ 之前插入薄壳 include ----
marker = 'if __name__ == "__main__":'
idx = new_src.index(marker)
shell = (
    "# =========================================================================== #\n"
    "#  订阅路由（已抽离到 utils/subscription.py，组 S 合并重构）\n"
    "# =========================================================================== #\n"
    "from utils import subscription  # noqa: E402\n"
    "app.include_router(subscription.router)  # 订阅域路由\n\n\n"
)
new_src = new_src[:idx] + shell + new_src[idx:]

open(OUT, "w", encoding="utf-8").write(subscription_src)
open(MAIN, "w", encoding="utf-8").write(new_src)

print(f"subscription.py 行数: {subscription_src.count(chr(10)) + 1}")
print(f"main.py 删除行数: {(list_e - list_s + 1) + (crud_e - crud_s + 1)}")
print(f"main.py 剩余行数: {len(new_lines)}")
print("list_s,list_e =", list_s + 1, list_e + 1)
print("crud_s,crud_e =", crud_s + 1, crud_e + 1)
print("done")
