"""
scripts/test_internal_transfer_flow.py —— 对话流·资金归集端到端验收（验收标准 #4）

场景（走 run_server 的真实对话链路，不是直连 mock_bank）：
    用户：「把零钱罐的钱都归到微众卡」
      → understand 识别 intent=write / action=internal_transfer
      → fill_params 解析账户名（零钱罐 → 账户 id，微众卡 → 主账户）+
        「都」→ 自动取零钱罐全部余额作为金额
      → risk_check：本人账户间划转 → 仅支付密码（★ 不发验证码）
      → verify：支付密码核验（/api/pay/password/verify + 固定占位符 resume）
      → confirm：确认卡显示「两个账户名 + 金额」
      → execute：POST /api/accounts/transfer-internal
    断言：钱真的动了（微众卡 +500 / 零钱罐归零）、双流水 remark=内部划转、
         双方收件箱通知、operation_logs 留痕 internal_transfer。

★ 端点契约备忘（踩过的坑）：
    POST /api/chat         → 普通 JSON（{reply, suspended, kind, ...}）
    POST /api/chat/resume  → ★ SSE 流（data: {...} 帧），必须按帧解析才能拿到
                             done.payload；直接 .json() 会得到整段 SSE 文本。
    因此本脚本对 resume 用 _sse() 解析，对 chat 用 _json()。

测试用户（itest_flow）每次运行重建其业务数据，可重复执行，不污染演示账号。
用法（bank-agent/ 下，.venv，且 8178 + 8003 都在跑）：
    .venv/Scripts/python.exe scripts/test_internal_transfer_flow.py
"""
from __future__ import annotations

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql
import config
import requests

BASE = "http://127.0.0.1:8178"
USERNAME = "itest_flow"
PASSWORD = "itest123456"
PAY_PWD = "246810"
THREAD = "itest-internal-transfer"
PWD_PLACEHOLDER = "[已输入支付密码]"

results: list[tuple[str, bool]] = []


def db():
    return pymysql.connect(host=config.DB_HOST, port=config.DB_PORT, user=config.DB_USER,
                           password=config.DB_PASSWORD, database=config.DB_NAME,
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)


def q1(sql: str, args: tuple = ()) -> dict:
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchone() or {}


def qall(sql: str, args: tuple = ()) -> list[dict]:
    with db() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall() or [])


def check(name: str, cond, detail: str = "") -> bool:
    ok = bool(cond)
    results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not ok else ""))
    return ok


def _headers(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _json(method: str, path: str, token: str | None = None, body: dict | None = None):
    r = requests.request(method, BASE + path, json=body, headers=_headers(token), timeout=180)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text[:400]}


def _sse(path: str, token: str, body: dict) -> tuple[list[dict], dict]:
    """调 SSE 端点（/api/chat/resume），返回 (node 进度事件, done.payload)。"""
    nodes: list[dict] = []
    done: dict = {}
    with requests.post(BASE + path, json=body, headers=_headers(token),
                       timeout=180, stream=True) as r:
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except ValueError:
                continue
            if ev.get("type") == "node":
                nodes.append(ev)
            elif ev.get("type") == "done":
                done = ev.get("payload") or {}
            elif ev.get("type") == "error":
                done = {"error": ev.get("detail")}
    return nodes, done


def _reply(payload: dict) -> str:
    return str((payload or {}).get("reply") or "")


# --------------------------------------------------------------------------- #
def prepare_user() -> tuple[int, str]:
    """注册（或复用）+ 重置数据 + 绑手机 + 设支付密码，返回 (uid, token)。"""
    from utils import user_provision
    st, d = _json("POST", "/api/auth/register", body={"username": USERNAME, "password": PASSWORD})
    if st != 200:
        st, d = _json("POST", "/api/auth/login", body={"username": USERNAME, "password": PASSWORD})
        assert st == 200, f"登录失败：{st} {d}"
    token = d["token"]
    uid = int(d["user"]["user_id"])

    with db() as c:
        with c.cursor() as cur:
            cur.execute("SELECT id FROM accounts WHERE user_id=%s", (uid,))
            acc = [int(r["id"]) for r in (cur.fetchall() or [])]
            if acc:
                fmt = ",".join(["%s"] * len(acc))
                cur.execute(f"DELETE FROM transactions WHERE account_id IN ({fmt})", tuple(acc))
            for t in ("cards", "payees", "notifications", "operation_logs", "accounts"):
                cur.execute(f"DELETE FROM {t} WHERE user_id=%s", (uid,))
            cur.execute("DELETE FROM verification_codes WHERE user_id=%s", (uid,))
            cur.execute("UPDATE users SET phone=NULL, phone_pending=NULL, pay_password_hash=NULL, "
                        "pay_pwd_attempts=0, pay_pwd_locked_until=NULL WHERE id=%s", (uid,))
        c.commit()
    user_provision.provision_new_user(uid, "归集流程测试")

    st, r = _json("POST", "/api/profile/phone/bind-request", token, {"phone": "13900001234"})
    assert r.get("ok"), f"发码失败：{r}"
    code = q1("SELECT code_plain FROM verification_codes WHERE user_id=%s AND scene='phone_bind' "
              "AND status='unused' ORDER BY id DESC LIMIT 1", (uid,)).get("code_plain")
    st, r = _json("POST", "/api/profile/phone/bind-confirm", token, {"code": str(code)})
    assert r.get("ok"), f"绑定失败：{r}"
    st, r = _json("POST", "/api/pay/password/set", token, {"password": PAY_PWD})
    assert r.get("ok"), f"设置支付密码失败：{r}"
    names = [a["name"] for a in qall("SELECT name FROM accounts WHERE user_id=%s ORDER BY id", (uid,))]
    print(f"  测试用户 uid={uid}（已绑手机 + 已设支付密码），账户：{names}")
    return uid, token


def main() -> int:
    print("[test_internal_transfer_flow] 对话流·资金归集 端到端验收")
    uid, token = prepare_user()

    accounts = qall("SELECT id, name, balance, is_primary FROM accounts WHERE user_id=%s ORDER BY id",
                    (uid,))
    main_acc = next(a for a in accounts if a["is_primary"])
    sub_acc = next(a for a in accounts if not a["is_primary"])
    sub_before = float(sub_acc["balance"])
    main_before = float(main_acc["balance"])
    print(f"  初始：{sub_acc['name']} ¥{sub_before:.2f} / {main_acc['name']} ¥{main_before:.2f}")

    # ======================================================================= #
    print("\n=== ① 对话：「把零钱罐的钱都归到微众卡」→ 风控分级=仅支付密码 ===")
    st, r1 = _json("POST", "/api/chat", token,
                   {"thread_id": THREAD, "message": f"把{sub_acc['name']}的钱都归到{main_acc['name']}"})
    reply1 = _reply(r1)
    print("  —— 回复 ——\n" + reply1)
    check("HTTP 200", st == 200, json.dumps(r1, ensure_ascii=False, default=str)[:300])
    check("流程挂起（等待安全验证）", bool((r1 or {}).get("suspended")))
    check("★ 仅支付密码：提示支付密码", "支付密码" in reply1)
    check("★ 免验证码：未提示验证码", "验证码已发送" not in reply1 and "收件箱" not in reply1)
    check("★ 未产生验证码通知（仅手机绑定那 1 条）",
          int(q1("SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s AND type='verify_code'",
                 (uid,))["n"]) == 1)

    # ======================================================================= #
    print("\n=== ② 支付密码核验 → 恢复 → 确认卡（两账户名 + 金额）===")
    st, pv = _json("POST", "/api/pay/password/verify", token, {"password": PAY_PWD})
    check("支付密码核验通过", pv.get("ok") is True, json.dumps(pv, ensure_ascii=False)[:200])
    nodes2, r2 = _sse("/api/chat/resume",
                      token, {"conversation_id": THREAD, "resume": PWD_PLACEHOLDER})
    reply2 = _reply(r2)
    print("  —— 回复 ——\n" + reply2)
    check("走到确认环节（confirm 节点执行过）",
          any(n.get("node") == "confirm" for n in nodes2), str([n.get("node") for n in nodes2]))
    check("确认卡含转出账户名", sub_acc["name"] in reply2, reply2[:200])
    check("确认卡含转入账户名", main_acc["name"] in reply2, reply2[:200])
    check("★「都」→ 金额自动取全部余额", f"{sub_before:.2f}" in reply2, f"expect {sub_before:.2f}")
    check("确认卡注明免验证码仅密码", "免验证码" in reply2 or "支付密码" in reply2)
    check("流程挂起（等待确认）", bool((r2 or {}).get("suspended")))
    check("密码不落对话记录（只落占位符）", "246810" not in reply2)

    # ======================================================================= #
    print("\n=== ③ 回复「确认」→ 钱真的动了 + 双流水 + 双通知 + 留痕 ===")
    nodes3, r3 = _sse("/api/chat/resume", token, {"conversation_id": THREAD, "resume": "确认"})
    reply3 = _reply(r3)
    print("  —— 回复 ——\n" + reply3)
    check("execute 节点已执行", any(n.get("node") == "execute" for n in nodes3),
          str([n.get("node") for n in nodes3]))
    check("执行成功（回复含归集成功/流水号 IT）", "归集" in reply3 and "IT" in reply3, reply3[:300])
    check("流程已结束（不再挂起）", not (r3 or {}).get("suspended"))
    after_sub = float(q1("SELECT balance FROM accounts WHERE id=%s", (sub_acc["id"],))["balance"])
    after_main = float(q1("SELECT balance FROM accounts WHERE id=%s", (main_acc["id"],))["balance"])
    check(f"转出账户归零（{sub_before:.2f} → {after_sub:.2f}）", abs(after_sub) < 0.001)
    check(f"转入账户 +{sub_before:.2f}（{main_before:.2f} → {after_main:.2f}）",
          abs(after_main - (main_before + sub_before)) < 0.001)
    itx = qall("SELECT * FROM transactions WHERE account_id IN (%s,%s) AND remark='内部划转'",
               (sub_acc["id"], main_acc["id"]))
    check("双流水（remark=内部划转，一出一入）",
          len(itx) == 2 and {t["type"] for t in itx} == {"expense", "income"}, str(len(itx)))
    its = qall("SELECT title, content FROM notifications WHERE user_id=%s AND type='internal_transfer'",
               (uid,))
    check("双方通知 2 条（转出 / 转入）", len(its) == 2, str(len(its)))
    check("通知含「资金归集」与金额",
          all("资金归集" in str(n.get("title")) for n in its)
          and any(f"{sub_before:.2f}" in str(n.get("content")) for n in its),
          json.dumps(its, ensure_ascii=False, default=str)[:200])
    logs = qall("SELECT action, detail, result FROM operation_logs WHERE user_id=%s "
                "AND action='internal_transfer'", (uid,))
    check("operation_logs 留痕 internal_transfer（success 1 条）",
          len(logs) == 1 and logs[0]["result"] == "success",
          json.dumps(logs, ensure_ascii=False, default=str)[:200])
    check("留痕含两账户",
          logs and sub_acc["name"] in str(logs[0]["detail"])
          and main_acc["name"] in str(logs[0]["detail"]))

    # ======================================================================= #
    print("\n=== ④ 归集后主账户可正常对外转账（钱真的能用了）===")
    st, plist = _json("GET", "/api/payees", token)
    payee = next((p for p in (plist.get("payees") or []) if p.get("card_no")), None)
    check("新用户沙箱收款人可用（带开户行 + 卡号）", payee is not None,
          json.dumps(plist, ensure_ascii=False, default=str)[:200])
    if payee:
        st, tr = _json("POST", "/api/chat", token,
                       {"thread_id": THREAD + "-tr", "message": f"给{payee['nickname']}转50"})
        rtxt = _reply(tr)
        print("  —— 回复 ——\n" + rtxt[:300])
        check("对外转账进入安全流程（未被归集动作抢走）",
              "支付密码" in rtxt or "验证码" in rtxt or "确认" in rtxt, rtxt[:200])

    # ======================================================================= #
    print("\n=== ⑤ 余额不足时不发密码卡、直接拦下 ===")
    st, r5 = _json("POST", "/api/chat", token,
                   {"thread_id": THREAD + "-insufficient",
                    "message": f"把{sub_acc['name']}的钱归到{main_acc['name']} 99999"})
    reply5 = _reply(r5)
    print("  —— 回复 ——\n" + reply5[:300])
    check("余额不足被预检拦下（不进验证环节）",
          "余额不足" in reply5 and not (r5 or {}).get("suspended"), reply5[:200])

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("\n" + "=" * 60)
    print(f"  {passed}/{total} PASSED" + ("  ALL GREEN" if passed == total else "  *** FAILED ***"))
    print("=" * 60)
    if passed != total:
        for name, ok in results:
            if not ok:
                print(f"  FAIL: {name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
