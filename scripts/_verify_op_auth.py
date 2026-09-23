"""临时校验：op_auth 签发/消费 + RequireOpToken 依赖行为（跑完自清测试行）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymysql, config, hashlib
from mock_bank import db
from utils import op_auth
from fastapi import Request

# 1) 确保表存在
with db.get_conn() as c:
    with c.cursor() as cur:
        cur.execute(open(os.path.join("sql", "upgrade_op_auth.sql"), encoding="utf-8").read())
    c.commit()

uid = 1
ok = True
def check(name, cond):
    global ok
    ok = ok and cond
    print(("PASS" if cond else "FAIL"), name)

# 2) 签发
r = op_auth.issue_op_token(uid, "transfer")
check("issue returns token", r.get("ok") and r.get("token"))
tok = r["token"]

# 3) 消费（合法）→ ok
res = op_auth.consume_op_token(uid, "transfer", tok)
check("consume valid -> ok", res.get("ok") is True)

# 4) 重用 → used
res = op_auth.consume_op_token(uid, "transfer", tok)
check("reuse -> OP_TOKEN_USED", (not res.get("ok")) and res.get("code") == "OP_TOKEN_USED")

# 5) 错误 scene → mismatch
res = op_auth.consume_op_token(uid, "redeem", tok)
check("wrong scene -> MISMATCH", (not res.get("ok")) and res.get("code") == "OP_TOKEN_MISMATCH")

# 6) 缺失 → missing
res = op_auth.consume_op_token(uid, "transfer", None)
check("missing -> MISSING", res.get("code") == "OP_TOKEN_MISSING")

# 7) 伪造（篡改 sig）→ invalid
bad = tok[:-4] + "ffff"
res = op_auth.consume_op_token(uid, "transfer", bad)
check("forged sig -> INVALID", res.get("code") == "OP_TOKEN_INVALID")

# 8) 篡改 uid → mismatch
parts = tok.split(".")
forged_uid = parts[:]; forged_uid[0] = str(uid + 999)
bad2 = ".".join(forged_uid)
res = op_auth.consume_op_token(uid, "transfer", bad2)
check("tampered uid -> MISMATCH/INVALID", not res.get("ok"))

# 9) RequireOpToken 依赖：带合法新票 → 不抛；无票 → 401
r2 = op_auth.issue_op_token(uid, "transfer")
tok2 = r2["token"]
dep = op_auth.RequireOpToken("transfer")
hdr_ok = Request({"type": "http", "headers": [(b"x-op-token", tok2.encode()),
                                             (b"authorization", b"Bearer x")]})
try:
    dep(hdr_ok, uid); passed = True
except Exception as e:
    passed = False
    print("   [debug] with-token raised:", repr(e))
check("RequireOpToken with token passes", passed)
hdr_no = Request({"type": "http", "headers": [(b"authorization", b"Bearer x")]})
try:
    dep(hdr_no, uid); passed = False
except Exception as e:
    passed = (getattr(e, "status_code", None) == 401)
check("RequireOpToken without token -> 401", passed)

# 清理测试行
with db.get_conn() as cc:
    with cc.cursor() as cur:
        cur.execute("DELETE FROM op_tickets WHERE token_hash=%s OR token_hash=%s",
                    (hashlib.sha256(tok.encode()).hexdigest(),
                     hashlib.sha256(tok2.encode()).hexdigest()))
    cc.commit()

print("RESULT:", "ALL GREEN" if ok else "HAS FAILURE")
sys.exit(0 if ok else 1)
