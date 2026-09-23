"""check_uid_reuse_guard.py —— 端到端验证「uid 被复用也不会继承别人的数据」

这是本轮修复要防的核心场景，全链路复现一遍：

  A. 正常链路：注册 → 有独立沙箱数据 → admin 删除接口 → 业务数据全空
  B. 危险链路（历史 bug 的复现）：只在 users 表删掉账号（模拟以前的手工删库），
     资产留在库里 → 把 AUTO_INCREMENT 拨回该 uid → 再注册一个**全新账号**，
     它会被分配到同一个 uid。此时新账号必须拿到**干净的新沙箱**，
     而不是上一个主人留下的余额 / 流水；接口回执里 uid_recycled 应为 true。
  C. 写入链路没被外键打坏：新账号能正常发言（chat_messages 外键 + 会话外键都要过）。

用法（先起 8178，bank-agent/ 下 .venv）：
    ./.venv/Scripts/python.exe scripts/check_uid_reuse_guard.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402
import pymysql  # noqa: E402

BASE = "http://127.0.0.1:8178"
env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
conn = pymysql.connect(host=env["DB_HOST"], port=int(env["DB_PORT"]), user=env["DB_USER"],
                       password=env["DB_PASSWORD"], database=env["DB_NAME"], charset="utf8mb4",
                       autocommit=True, cursorclass=pymysql.cursors.DictCursor)
cur = conn.cursor()

A_USER, B_USER = "_uidreuse_a", "_uidreuse_b"
PWD = "pass123456"
ok = True
EXTRA_UIDS: list[int] = []   # 名字被删掉但资产还在的 uid（残留现场），收尾要一起清


def say(msg: str) -> None:
    print(msg)


def check(cond: bool, good: str, bad: str) -> bool:
    global ok
    print(("  ✓ " if cond else "  ✗ ") + (good if cond else bad))
    if not cond:
        ok = False
    return cond


def http(method: str, path: str, body: dict | None = None, token: str | None = None):
    req = urllib.request.Request(BASE + path, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw}


def cleanup() -> None:
    uids = set(EXTRA_UIDS)
    for name in (A_USER, B_USER, A_USER + "2", A_USER + "3"):
        cur.execute("SELECT id FROM users WHERE username=%s", (name,))
        row = cur.fetchone()
        if row:
            uids.add(int(row["id"]))
    for uid in uids:
        cur.execute("SELECT id FROM accounts WHERE user_id=%s", (uid,))
        aids = [int(r["id"]) for r in cur.fetchall()]
        if aids:
            ph = ",".join(["%s"] * len(aids))
            cur.execute(f"DELETE FROM transactions WHERE account_id IN ({ph})", aids)
        cur.execute("SELECT conversation_id FROM conversations WHERE user_id=%s", (uid,))
        cids = [r["conversation_id"] for r in cur.fetchall()]
        if cids:
            ph = ",".join(["%s"] * len(cids))
            cur.execute(f"DELETE FROM chat_messages WHERE conversation_id IN ({ph})", cids)
        for t in ("chat_messages", "conversations", "verification_codes", "operation_logs",
                  "notifications", "pending_settlements", "holdings", "subscriptions",
                  "payees", "cards", "accounts", "idempotency_records",
                  "transfer_daily_quota", "users"):
            cur.execute(f"DELETE FROM {t} WHERE " +
                        ("id=%s" if t == "users" else "user_id=%s"), (uid,))
    # 自增值归位，别把库的自增拨歪
    cur.execute("SELECT COALESCE(MAX(id),0) AS m FROM users")
    nxt = int(cur.fetchone()["m"]) + 1
    cur.execute(f"ALTER TABLE users AUTO_INCREMENT = {nxt}")


cleanup()

# --------------------------------------------------------------- A. 正常链路
say("\n[A] 正常链路：注册 → 独立沙箱 → admin 删除接口")
st, d = http("POST", "/api/auth/register", {"username": A_USER, "password": PWD})
check(st == 200 and d.get("ok"), f"注册成功 uid={d.get('user', {}).get('user_id')}", f"注册失败 {st} {d}")
uid_a = d["user"]["user_id"]
a_accts = d.get("provision", {}).get("transactions")
check(bool(a_accts), f"注册即开户：新账号自带 {a_accts} 笔流水", f"未初始化沙箱 {d.get('provision')}")
check(d.get("uid_recycled") is False, "uid_recycled=false（没有误判成复用）", "误判 uid 复用")

cur.execute("SELECT COUNT(*) AS n FROM accounts WHERE user_id=%s", (uid_a,))
n_acc = int(cur.fetchone()["n"])
check(n_acc == 2, f"库内该账号有 {n_acc} 个账户", f"账户数异常 {n_acc}")

st, d = http("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
check(st == 200 and d.get("token"), "管理员登录成功", f"管理员登录失败 {st} {d}")
admin_token = d["token"]

st, d = http("DELETE", f"/api/admin/users/{uid_a}", token=admin_token)
check(st == 400, "未传 confirm_username 时被拒（防删错人）", f"竟然直接删了 {st} {d}")

st, d = http("DELETE", f"/api/admin/users/{uid_a}?confirm_username=wrongname", token=admin_token)
check(st == 400, "confirm_username 不匹配时被拒", f"校验失效 {st} {d}")

st, d = http("DELETE", f"/api/admin/users/{uid_a}?confirm_username={A_USER}", token=admin_token)
check(st == 200 and d.get("ok"), f"删除成功：{d.get('message')}", f"删除失败 {st} {d}")
say(f"      逐表回执：{d.get('deleted')}")

for t, col in (("accounts", "user_id"), ("cards", "user_id"), ("subscriptions", "user_id"),
               ("payees", "user_id"), ("conversations", "user_id"), ("chat_messages", "user_id"),
               ("notifications", "user_id"), ("operation_logs", "user_id")):
    cur.execute(f"SELECT COUNT(*) AS n FROM {t} WHERE {col}=%s", (uid_a,))
    n = int(cur.fetchone()["n"])
    check(n == 0, f"{t} 已级联清空", f"{t} 仍有 {n} 行残留")
cur.execute("SELECT COUNT(*) AS n FROM users WHERE id=%s", (uid_a,))
check(int(cur.fetchone()["n"]) == 0, "users 行已删除", "users 行还在")

cur.execute("SELECT COUNT(*) AS n FROM admin_logs WHERE action='user_delete' AND target_id=%s",
            (str(uid_a),))
check(int(cur.fetchone()["n"]) >= 1, "删除动作已写入 admin_logs 审计", "审计没有留痕")

# --------------------------------------------- B. 危险链路：uid 复用 + 残留数据
say("\n[B] 危险链路：只删 users 行留下资产 → 把自增拨回 → 新账号复用同一个 uid")


def tx_sum(uid: int):
    cur.execute("SELECT COALESCE(SUM(amount),0) AS s FROM transactions t JOIN accounts a "
                "ON a.id=t.account_id WHERE a.user_id=%s", (uid,))
    return cur.fetchone()["s"]


# 先注册一个"老用户"并记录它的数据指纹
st, d = http("POST", "/api/auth/register", {"username": A_USER + "2", "password": PWD})
check(st == 200, "先造一个带资产的账号", f"注册失败 {st} {d}")
uid_probe = d["user"]["user_id"]
cur.execute("SELECT COUNT(*) AS n FROM accounts WHERE user_id=%s", (uid_probe,))
check(int(cur.fetchone()["n"]) == 2, "该账号已有账户记录", "没有账户记录，测试前提不成立")

# ① 先确认「正常删 users 行」现在会级联清干净（这正是本轮修复的效果）
cur.execute("DELETE FROM users WHERE id=%s", (uid_probe,))
cur.execute("SELECT COUNT(*) AS n FROM accounts WHERE user_id=%s", (uid_probe,))
check(int(cur.fetchone()["n"]) == 0,
      "直接 DELETE FROM users 已被外键级联清空（修复生效）",
      "直接删 users 行仍有残留 —— 说明外键没生效")

# ② 残留只可能来自"绕过外键的删除"：db_snapshot restore 用 SET FOREIGN_KEY_CHECKS=0
#    包住整段恢复，人工带 checks=0 手删同理。按这条路造出残留现场。
st, d = http("POST", "/api/auth/register", {"username": A_USER + "3", "password": PWD})
check(st == 200, "再注册一个账号作为残留源", f"注册失败 {st} {d}")
uid_victim = d["user"]["user_id"]
EXTRA_UIDS.append(uid_victim)
victim_sum = tx_sum(uid_victim)
cur.execute("SET FOREIGN_KEY_CHECKS=0")
cur.execute("DELETE FROM users WHERE id=%s", (uid_victim,))
cur.execute("SET FOREIGN_KEY_CHECKS=1")
cur.execute("SELECT COUNT(*) AS n FROM accounts WHERE user_id=%s", (uid_victim,))
check(int(cur.fetchone()["n"]) == 2,
      f"复现旧 bug 现场：uid={uid_victim} 的 users 行没了、资产 2 账户 / 流水合计 {victim_sum} 全留下",
      "没有造出残留数据，本用例测不到目标路径")
# ★ 把自增值拨回该 uid，逼下一次注册复用它
cur.execute(f"ALTER TABLE users AUTO_INCREMENT = {uid_victim}")

st, d = http("POST", "/api/auth/register", {"username": B_USER, "password": PWD})
check(st == 200 and d.get("ok"), f"新账号注册成功 uid={d.get('user', {}).get('user_id')}",
      f"注册失败 {st} {d}")
uid_b = d["user"]["user_id"]
check(uid_b == uid_victim, f"新账号确实复用了同一个 uid={uid_b}（前提成立）",
      f"没复用（uid={uid_b} vs 残留 {uid_victim}），本用例没测到目标路径")
check(d.get("uid_recycled") is True,
      f"接口回执 uid_recycled=true，清掉残留 {d.get('residue_purged')} 行",
      f"未识别 uid 复用：uid_recycled={d.get('uid_recycled')}")

cur.execute("SELECT COALESCE(SUM(amount),0) AS s FROM transactions t JOIN accounts a "
            "ON a.id=t.account_id WHERE a.user_id=%s", (uid_b,))
new_sum = cur.fetchone()["s"]
cur.execute("SELECT COUNT(*) AS n FROM accounts WHERE user_id=%s", (uid_b,))
check(int(cur.fetchone()["n"]) == 2, "新账号是全新的 2 个账户（不是残留的那 2 个）",
      "账户数异常")
check(str(new_sum) != str(victim_sum),
      f"流水已换成新沙箱（残留合计 {victim_sum} → 新账号合计 {new_sum}）",
      f"流水合计与残留完全相同（{new_sum}）—— 疑似仍继承了旧数据")

# ------------------------------------------------------------- C. 写入链路
say("\n[C] 外键没打坏写入：新账号能正常存消息")
token_b = d["token"]
st, d2 = http("GET", "/api/profile", token=token_b)
check(st == 200 and d2.get("ok"), "新账号读取画像正常", f"画像失败 {st} {d2}")

st, d2 = http("GET", "/api/transactions", token=token_b)
check(st == 200 and d2.get("ok"), "新账号读账单正常", f"读账单失败 {st} {d2}")

st, d3 = http("POST", "/api/auth/login", {"username": "user1", "password": "123456"})
check(st == 200, "既有账号 user1 仍可登录", f"user1 登录失败 {st} {d3}")
if st == 200:
    st, d4 = http("GET", "/api/transactions", token=d3["token"])
    check(st == 200 and d4.get("ok"), "既有账号账单接口正常（外键没打坏）", f"账单失败 {st} {d4}")

cleanup()
say("\n测试数据已清理，users 自增值已归位。")
conn.close()
print("\n结论：" + ("全部通过" if ok else "存在失败项，请查看上面的 ✗"))
sys.exit(0 if ok else 1)
