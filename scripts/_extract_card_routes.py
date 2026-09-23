"""组 C+：把卡片路由从 mock_bank.main 抽到 utils/card.py（路由与卡域同处）。

复用组 T 模式：card.py 顶层不 import mock_bank.main；共享 helper（owned/fail/json_safe/
to_decimal）在各路由函数内惰性 import。current_uid 走 utils/deps.py（单一归属）。
"""
import re

MAIN = r"D:\Users\Dwm\Desktop\agaent\bank-agent\mock_bank\main.py"
CARD = r"D:\Users\Dwm\Desktop\agaent\bank-agent\utils\card.py"

main_lines = open(MAIN, encoding="utf-8").read().split("\n")
card_src = open(CARD, encoding="utf-8").read()


def find(pred, start=0):
    return next(i for i, l in enumerate(main_lines) if i >= start and pred(l))


# ---- 定位 4 个 chunk（1-indexed 语义按 0-indexed 计算）----
V_S = find(lambda l: l.startswith('@app.get("/api/card/validate")'))
V_E = find(lambda l: l.startswith("class PayeeResolveReq(BaseModel):"), V_S)

L_S = find(lambda l: l.startswith('@app.get("/api/cards")'))
L_E = find(lambda l: l.startswith('@app.get("/api/operation_logs")'), L_S)

C_S = find(lambda l: l.startswith("#  银行卡 CRUD")) - 1          # 含上方 border
C_E = find(lambda l: l.startswith("#  账户（卡片挂靠")) - 1        # 不含下方 border

A_S = find(lambda l: l.startswith("class CardAdjustLimitReq(BaseModel):"))
A_E = find(lambda l: l.startswith("#  订阅路由（已抽离")) - 1      # 不含下方 border

CHUNKS = [(V_S, V_E), (L_S, L_E), (C_S, C_E), (A_S, A_E)]
for s, e in CHUNKS:
    assert 0 <= s < e <= len(main_lines), (s, e)

# 提取（用于 card.py）
picked: list[str] = []
for s, e in CHUNKS:
    picked.extend(main_lines[s:e])
    picked.extend(["", ""])

# 去掉 main 的 section 头/border 行
picked = [l for l in picked
          if not re.fullmatch(r"# =+ #", l.strip())
          and l.strip() != "#  银行卡 CRUD"]

text = "\n".join(picked)
# 长串优先替换，避免被通用前缀剥离破坏路径文案
text = text.replace("utils/card_service.apply_card", "utils/card.apply_card")
text = text.replace("utils/card_service.py", "utils/card.py")
# 路由函数 apply_card 与 card.py 内的服务 apply_card 同名 → 路由改名（URL 不变）
text = text.replace("def apply_card(req: CardApplyReq", "def apply_card_endpoint(req: CardApplyReq")
# 装饰器改名
text = re.sub(r"@app\.(get|post|put|delete)\(", r"@router.\1(", text)
# 本地化：card.py 即 card_validator / card_service 的源
text = text.replace("card_service.", "").replace("card_validator.", "")
picked = text.split("\n")

# 惰性 import 共享 helper（插到各路由函数 docstring 之后）
LAZY = {
    "list_cards": ["json_safe", "fail"],
    "apply_card_endpoint": ["fail"],
    "adjust_card_limit": ["owned", "to_decimal", "fail"],
    "freeze_card": ["owned", "fail"],
    "unfreeze_card": ["owned", "fail"],
    "delete_card": ["owned", "fail"],
}
assert "def list_cards(" in text
assert "def apply_card_endpoint(" in text


def insert_lazy(block: list[str], func: str, names: list[str]) -> list[str]:
    if not names:
        return block
    di = next(i for i, l in enumerate(block) if l.startswith(f"def {func}("))
    si = di
    while not block[si].rstrip().endswith(":"):
        si += 1
    j = si + 1
    ins = j
    st = block[j].strip()
    if st.startswith('"""') or st.startswith("'''"):
        q = st[:3]
        if len(st) >= 6 and st.endswith(q):
            ins = j + 1
        else:
            k = j + 1
            while not block[k].strip().endswith(q):
                k += 1
            ins = k + 1
    imp = ("    from mock_bank.main import " + ", ".join(names)
           + "  # noqa: PLC0415 惰性引用共享 helper（避免与 main 循环导入）")
    return block[:ins] + [imp] + block[ins:]


for fn, names in LAZY.items():
    picked = insert_lazy(picked, fn, names)

route_section = (
    "\n\n# =========================================================================== #\n"
    "#  HTTP 路由（组 C+ 从 mock_bank.main 抽离 —— 路由与卡域同处）\n"
    "#  ★ 顶层不 import mock_bank.main：共享 helper（owned / fail / json_safe / to_decimal）\n"
    "#    在各路由函数内**惰性 import**（复用组 T 模式，避免循环导入）。\n"
    "#  ★ current_uid 走 utils/deps.py（单一归属，security 保持 FastAPI-free）。\n"
    "# =========================================================================== #\n"
    "from fastapi import APIRouter, Depends, HTTPException, Query  # noqa: E402\n"
    "from pydantic import BaseModel, Field  # noqa: E402\n"
    "from mock_bank import db  # noqa: E402\n"
    "from utils import settlement_engine  # noqa: E402\n"
    "from utils.deps import current_uid  # noqa: E402\n"
    "from rules import CARD_LIMIT_MIN, CARD_LIMIT_MAX  # noqa: E402\n"
    "\n"
    "router = APIRouter()\n\n\n"
    + "\n".join(picked).strip("\n")
    + "\n"
)

open(CARD, "w", encoding="utf-8").write(card_src.rstrip("\n") + "\n" + route_section)

# ---- 从 main 删除 4 个 chunk（倒序删）----
new_lines = list(main_lines)
for s, e in sorted(CHUNKS, key=lambda t: t[0], reverse=True):
    del new_lines[s:e]
# 折叠 3+ 连续空行为 2
out: list[str] = []
for l in new_lines:
    if l == "" and len(out) >= 2 and out[-1] == "" and out[-2] == "":
        continue
    out.append(l)
new_lines = out

# ---- 末尾加 include_router ----
new_src = "\n".join(new_lines)
marker = "app.include_router(subscription.router)  # 订阅域路由"
assert marker in new_src
new_src = new_src.replace(
    marker,
    marker + "\n"
    "from utils import card as card_domain  # noqa: E402\n"
    "app.include_router(card_domain.router)  # 卡域路由（组 C+）",
)
open(MAIN, "w", encoding="utf-8").write(new_src)

print("card.py 行数:", (card_src.rstrip("\n") + "\n" + route_section).count("\n") + 1)
print("main.py 行数:", new_src.count("\n") + 1)
print("chunks:", [(s + 1, e) for s, e in CHUNKS])
print("done")
