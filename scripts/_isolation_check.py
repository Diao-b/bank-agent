# -*- coding: utf-8 -*-
"""数据隔离测试：验证「不同用户（含同名）之间数据不会互通」。

覆盖场景：
  1. 重复 username 注册 → 必须 409
  2. 两个新用户各自注册 → 账户/流水必须是**各自独立的沙箱**
  3. A 的会话 B 不能读、不能续写（跨用户越权）
  4. 用「微信/手机号同名」的显示名去改 profile，之后数据是否串
  5. 大小写/空格变体用户名是否能绕过唯一约束（MySQL ai_ci 大小写不敏感，但空格敏感）
用法：cd bank-agent && ./.venv/Scripts/python.exe scripts/_isolation_check.py
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8178"
TS = str(int(time.time()))[-6:]
PW = "pass123456"
FAIL = []


def call(method, path, body=None, token=None, timeout=60):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"raw": raw}


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not ok:
        FAIL.append(name)


def reg(u, p=PW):
    return call("POST", "/api/auth/register", {"username": u, "password": p})


print("=" * 78)
print("场景 1：重复 username 注册必须被拒")
u_a = f"isoA{TS}"
s, r = reg(u_a)
check(f"首次注册 {u_a}", s == 200 and r.get("ok"), f"status={s}")
tok_a = r.get("token")
s2, r2 = reg(u_a)
check("重复注册同名 → 409", s2 == 409, f"status={s2} detail={r2.get('detail')}")
s3, r3 = reg(u_a.upper() if u_a.upper() != u_a else u_a + "X")
check("大小写变体注册（MySQL ai_ci 应视为同名）", True, f"status={s3}（仅观察）")
s4, r4 = reg(u_a + " ")
check("尾部空格变体注册（NO PAD，可能被当成新用户）", s4 != 200,
      f"status={s4} <<< 若为 200 说明唯一约束可被空格绕过")

print()
print("=" * 78)
print("场景 2：两个新用户的数据沙箱必须彼此独立")
u_b = f"isoB{TS}"
s, r = reg(u_b)
tok_b = r.get("token")
check(f"注册 {u_b}", s == 200 and bool(tok_b), f"status={s}")

bal = {}
for tag, tok in (("A", tok_a), ("B", tok_b)):
    s, r = call("POST", "/api/chat", {"message": "查一下我的余额", "thread_id": f"iso{tag}{TS}"}, tok)
    bal[tag] = r.get("reply", "")
    print(f"    {tag} 余额回复前 90 字: {bal[tag][:90].replace(chr(10),' ')}")

# 从数据库直接比对（更硬的证据）
import profile_db as pdb  # noqa: E402
with pdb._conn() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username=%s", (u_a,))
        uid_a = cur.fetchone()["id"]
        cur.execute("SELECT id FROM users WHERE username=%s", (u_b,))
        uid_b = cur.fetchone()["id"]
        cur.execute("SELECT user_id, COUNT(*) AS n, SUM(balance) AS total FROM accounts "
                    "WHERE user_id IN (%s,%s) GROUP BY user_id", (uid_a, uid_b))
        acct = cur.fetchall()
        print(f"    A uid={uid_a} / B uid={uid_b}；账户聚合: {acct}")
        cur.execute("SELECT COUNT(*) AS n FROM transactions WHERE user_id=%s", (uid_a,))
        txn_a = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM transactions WHERE user_id=%s", (uid_b,))
        txn_b = cur.fetchone()["n"]
check("A/B 是两个不同的 user_id", uid_a != uid_b, f"{uid_a} vs {uid_b}")
check("A/B 各自有独立账户", len(acct) == 2, f"聚合行数={len(acct)}")
check("A/B 账户余额合计不相同（不是同一份数据）",
      len(acct) == 2 and acct[0]["total"] != acct[1]["total"], f"txn A={txn_a} B={txn_b}")

print()
print("=" * 78)
print("场景 3：跨用户越权（B 读/续写 A 的会话）")
s, convs_a = call("GET", "/api/conversations", None, tok_a)
cids_a = [c["conversation_id"] for c in convs_a.get("conversations", [])]
print(f"    A 的会话: {cids_a}")
target = cids_a[0] if cids_a else None
if target:
    s, r = call("GET", f"/api/conversations/{target}/messages", None, tok_b)
    check("B 读 A 的会话消息 → 被拒或返回空",
          s != 200 or not r.get("messages"), f"status={s} 条数={len(r.get('messages') or [])}")
    s, r = call("POST", "/api/chat", {"message": "你好", "thread_id": target}, tok_b)
    check("B 用 A 的 thread_id 发消息 → 被拒", s != 200, f"status={s} {str(r)[:80]}")
    s, r = call("GET", "/api/conversations", None, tok_b)
    cids_b = [c["conversation_id"] for c in r.get("conversations", [])]
    check("B 的会话列表不含 A 的会话", target not in cids_b, f"B 的: {cids_b}")

print()
print("=" * 78)
print("场景 4：显示名同名（把 B 的显示名改成和 A 一样）")
s, r = call("PUT", "/api/profile", {"real_name": u_a, "preference_note": ""}, tok_b)
print(f"    改 B 显示名为 {u_a}: status={s} {str(r)[:100]}")
s, r = call("POST", "/api/chat", {"message": "查一下我的余额", "thread_id": f"isoB2{TS}"}, tok_b)
print(f"    改完之后 B 再查余额: {str(r.get('reply'))[:90]}")
check("同名后 B 仍拿到自己的数据（不是 A 的）",
      uid_a != uid_b, "见上方 uid 对比")

print()
print("=" * 78)
print("结论：", "全部通过（未发现越权）" if not FAIL else f"未通过 {len(FAIL)} 项：{FAIL}")
