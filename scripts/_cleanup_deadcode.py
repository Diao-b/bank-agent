"""组 D：死代码清理（幂等；每项均经零引用证据核实）。行为零变化。"""
import os
import re

ROOT = r"D:\Users\Dwm\Desktop\agaent\bank-agent"


def rd(p):
    return open(os.path.join(ROOT, p), encoding="utf-8").read()


def wr(p, s):
    open(os.path.join(ROOT, p), "w", encoding="utf-8").write(s)


def sub(p, pairs):
    """幂等替换：old 存在则替换，否则视为已应用。"""
    s = rd(p)
    for old, new in pairs:
        if old in s:
            s = s.replace(old, new, 1)
    wr(p, s)


def collapse(s):
    return re.sub(r"\n{4,}", "\n\n\n", s)


def remove_top_func(s, name):
    m = re.search(r"\ndef " + re.escape(name) + r"\(", s)
    if not m:
        return s  # 已删
    start = m.start()
    nxt = re.search(r"\n(def |class |# -{5,})", s[m.end():])
    assert nxt, f"next anchor not found after {name}"
    end = m.end() + nxt.start()
    return s[:start] + "\n\n\n" + s[end + 1:]


# 1) 调用方直指新路径（幂等）
sub("scripts/seed_card_account.py", [
    ("    from utils import transfer_resolver", "    from utils.transfer import lookup_local_card"),
    ('transfer_resolver.lookup_local_card(p["card_no"], cur=cur)',
     'lookup_local_card(p["card_no"], cur=cur)'),
])
sub("utils/subscription.py", [
    ("from utils import deduction_engine", "from utils.settlement_engine import run_due_deductions"),
    ("deduction_engine.run_due_deductions(uid)", "run_due_deductions(uid)"),
])
assert "transfer_resolver" not in rd("scripts/seed_card_account.py")
assert "deduction_engine" not in rd("utils/subscription.py")

# 2) 删除三个兼容壳（零引用）
removed = []
for f in ("utils/transfer_resolver.py", "utils/subscription_detector.py", "utils/deduction_engine.py"):
    p = os.path.join(ROOT, f)
    if os.path.exists(p):
        n = rd(f).count("\n") + 1
        os.remove(p)
        removed.append((f, n))

# 3) 死常量
sub("agent/write_flow.py", [
    ('MARK_VERIFY_SMS = "[VERIFY:sms_code]"\nMARK_VERIFY_PWD = "[VERIFY:pay_password]"\n', ""),
])
sub("config.py", [
    ("# M1 阶段 read_agent 独立测试用的 sqlite checkpoint 文件（M3 嵌入主图后不再需要）\n"
     'READ_TEST_DB: str = str(BASE_DIR / "read_test.db")\n\n', ""),
])
sub("profile_db.py", [
    ("TITLE_TO_GENDER = {v: k for k, v in GENDER_TO_TITLE.items()}\n", ""),
])

# 4) 死函数（被取代 / 零引用）
s = collapse(remove_top_func(remove_top_func(rd("profile_db.py"), "save_greeting"), "touch_conversation"))
wr("profile_db.py", s)
wr("utils/transfer.py", collapse(remove_top_func(rd("utils/transfer.py"), "_bank_by_id")))

# 5) 死导入
sub("utils/mcp_client.py", [
    ("    from mcp.types import EmbeddedResource, ImageContent, TextContent",
     "    from mcp.types import TextContent"),
    ("    TextContent = ImageContent = EmbeddedResource = ()  # type: ignore[assignment]",
     "    TextContent = ()  # type: ignore[assignment]"),
])

# 6) 不可达分支
sub("run_server.py", [(" if True else []", "")])

print("removed shells:", removed)
for s in ("transfer_resolver", "subscription_detector", "deduction_engine", "MARK_VERIFY_SMS",
          "READ_TEST_DB", "TITLE_TO_GENDER", "save_greeting", "touch_conversation", "_bank_by_id",
          "if True else []", "EmbeddedResource", "ImageContent"):
    hits = []
    for base, _, files in os.walk(ROOT):
        if any(x in base for x in ("__pycache__", ".venv", "_bak_groupd")): continue
        for fn in files:
            if not fn.endswith(".py"): continue
            fp = os.path.join(base, fn)
            try: txt = open(fp, encoding="utf-8").read()
            except Exception: continue
            if s in txt: hits.append(os.path.relpath(fp, ROOT))
    print(f"  {s:22} residual in: {hits if hits else 'NONE'}")
print("done")
