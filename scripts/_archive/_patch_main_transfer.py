"""组 T：改写 mock_bank/main.py —— /api/transfer 变薄壳、移除已迁出的代码、修引用。

- 删除 @app.post("/api/transfer") 整个函数体（含 86 行 docstring），替换为薄壳调 execute_transfer；
- 删除 _sender_name（已迁入 utils/transfer.py）；
- 顶部队列：去掉 transfer_resolver，改为 from utils.transfer import ...；
- create_payee / resolve_payee_endpoint 的引用改直调。
"""
MAIN = r"D:\Users\Dwm\Desktop\agaent\bank-agent\mock_bank\main.py"
src = open(MAIN, encoding="utf-8").read()
lines = src.split("\n")

# ---- 1) 顶部队列 ----
old_imp = ["from utils import security",
           "from utils import inbox, transfer_resolver",
           "from utils import settlement_engine, card_validator, card_service"]
assert all(l in lines for l in old_imp), "top import block not found"
i0 = lines.index(old_imp[0])
lines[i0:i0 + 3] = [
    "from utils import security",
    "from utils import inbox",
    "from utils import settlement_engine, card_validator, card_service",
    "from utils.transfer import execute_transfer, resolve_payee, lookup_local_card",
]

# ---- 2) 替换 /api/transfer 函数（decorator .. 函数末行）----
dec = next(i for i, l in enumerate(lines) if l.strip() == '@app.post("/api/transfer")')
sender = next(i for i, l in enumerate(lines) if l.startswith("def _sender_name"))
assert lines[dec + 1].startswith("def transfer(req: TransferReq"), lines[dec + 1]

new_route = [
    '@app.post("/api/transfer")',
    "def transfer(req: TransferReq, request: Request, uid: int = Depends(current_uid)):",
    '    """转账执行（薄壳）—— 业务逻辑见 utils/transfer.execute_transfer。',
    "",
    "    站内轨实时到账（扣款+入账+双流水+双回执+双方留痕，同事务）；",
    "    跨行轨扣款即进在途（settle_status='pending' + pending_settlements 台账），",
    "    清算后到账或全额退回；详见该函数 docstring 的设计原则与事务边界。",
    '    """',
    "    return execute_transfer(",
    "        uid=uid,",
    "        from_account_id=req.from_account_id,",
    "        payee_id=req.payee_id,",
    "        amount_raw=req.amount,",
    "        payload=req.model_dump(),",
    '        inject=request.headers.get("X-Test-Inject"),',
    "    )",
]
# 保留函数后的分隔空行（原为 2 行空行 + _sender_name）
lines[dec:sender] = new_route + ["", ""]

# ---- 3) 删除 _sender_name（def .. return，共 5 行）----
s = next(i for i, l in enumerate(lines) if l.startswith("def _sender_name"))
assert lines[s + 4].startswith("    return (row or {})"), lines[s + 4]
del lines[s:s + 5]
# 把 s 附近窗口内的连续空行压到最多 2 行（仅排版，安全）
_lo = max(0, s - 3)
_hi = min(len(lines), s + 4)
_win = lines[_lo:_hi]
_outw: list[str] = []
for _l in _win:
    if _l == "" and len(_outw) >= 2 and _outw[-1] == "" and _outw[-2] == "":
        continue
    _outw.append(_l)
lines[_lo:_hi] = _outw

new_src = "\n".join(lines)

# ---- 4) 引用直调 ----
new_src = new_src.replace("transfer_resolver.lookup_local_card", "lookup_local_card")
new_src = new_src.replace("transfer_resolver.resolve_payee", "resolve_payee")
new_src = new_src.replace("utils/transfer_resolver.resolve_payee", "utils/transfer.resolve_payee")
assert "transfer_resolver" not in new_src, "残留 transfer_resolver 引用"

open(MAIN, "w", encoding="utf-8").write(new_src)
print("main.py 行数:", new_src.count("\n") + 1)
print("done")
