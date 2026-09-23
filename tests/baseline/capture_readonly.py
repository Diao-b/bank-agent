"""
capture_readonly.py —— 录制「只读接口形态 + 关键表 count」行为基线（可复现）

只发：登录 POST（拿 token，不改业务数据）、GET 只读接口、SSE（事件 type 序列）。
绝不发写接口（转账/订阅变更等由回归脚本覆盖，见 run_regression.py）。

配合 db_snapshot restore 使用可得「逐字节一致」的基线：
  - 所有 token / authorization 字段脱敏为 <REDACTED>
  - 所有 *_at / created_at / updated_at 时间戳脱敏为 <TS>（now() 每次不同，必须脱敏）

映射（用户要求的基线面 → 本文件覆盖项）：
  登录        -> login_guest / login_lige / login_admin
  余额        -> balance
  订阅        -> subscriptions
  收件箱      -> inbox / inbox_unread_count
  后台        -> admin_overview / admin_users / admin_logs
  文件(资产)  -> products / holdings   （项目无独立 /api/files，资产即「文件」类只读面）
  SSE 事件序列 -> sse_chat_stream（chat/stream 事件 type 序列）
  关键表 count -> table_counts
  转账全流程   -> 由 run_regression.py 的 crossbank/insite/internal/account_system 覆盖

用法：
    .venv/Scripts/python.exe tests/baseline/capture_readonly.py [--out tests/baseline/captures/baseline_readonly.json]
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pymysql
import requests
import config

PROXY = "http://127.0.0.1:8178"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures", "baseline_readonly.json")

# 时间戳类字段：每次运行值不同，必须脱敏，否则 diff 误报
TS_KEYS = {
    "created_at", "updated_at", "time", "timestamp", "sent_at", "expire_at",
    "issued_at", "verified_at", "paid_at", "settled_at", "returned_at",
    "expected_at", "last_login", "last_active",
}


def redact(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("token", "authorization", "access_token"):
                out[k] = "<REDACTED>"
            elif k in TS_KEYS or (isinstance(k, str) and k.endswith("_at")):
                out[k] = "<TS>" if v is not None else None
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def _post(path, json_body):
    r = requests.post(PROXY + path, json=json_body, timeout=10)
    try:
        return r.status_code, (r.json() if r.content else {})
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def _get(token, path, params=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(PROXY + path, params=params, headers=h, timeout=15)
    try:
        return r.status_code, redact(r.json() if r.content else {})
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def login(username, password):
    code, body = _post("/api/auth/login", {"username": username, "password": password})
    token = (body.get("token") if isinstance(body, dict) else None)
    return code, body, token


def guest():
    code, body = _post("/api/auth/guest", {})
    token = (body.get("token") if isinstance(body, dict) else None)
    return code, body, token


def capture_sse(token, message="你好"):
    """POST /api/chat/stream，收集事件序列（去 volatile 内容）。

    注意：chat/stream 走 LangGraph 驱动，依赖 LLM + SQLite checkpoint，属「环境相关」面，
    不在本次解耦重构的改动域内（R~W 只动 mock_bank + utils）。故仅作记录，不计入硬 diff 门槛。
    当前环境若报 'readonly database' 为预存的 checkpoint 文件只读问题，与代码结构无关。
    """
    tid = str(uuid.uuid4())
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        with requests.post(PROXY + "/api/chat/stream",
                           json={"thread_id": tid, "message": message},
                           headers=h, timeout=90, stream=True) as r:
            events = []
            for line in r.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("data:"):
                    body = line[5:].strip()
                    try:
                        ev = json.loads(body)
                        events.append(redact(ev))
                    except Exception:
                        events.append({"type": "<parse-error>"})
            return {"thread_id": tid, "event_types": [e.get("type") for e in events],
                    "events": events}
    except Exception as e:  # noqa: BLE001
        return {"thread_id": tid, "error": str(e)[:200], "event_types": [], "events": []}


def table_counts():
    out = {}
    with pymysql.connect(**config.db_config()) as c:
        with c.cursor() as cur:
            cur.execute("SHOW TABLES")
            for row in cur.fetchall():
                t = list(row.values())[0]
                cur.execute(f"SELECT COUNT(*) AS n FROM `{t}`")
                out[t] = int((cur.fetchone() or {}).get("n") or 0)
    return out


def main() -> int:
    t0 = time.time()
    cap: dict = {}

    # ---- 登录（拿 token，不改业务数据） ----
    gc, gb, guest_token = guest()
    cap["login_guest"] = {"status": gc, "body": redact(gb)}
    lc, lb, lige_token = login("lige", "lige123")
    cap["login_lige"] = {"status": lc, "body": redact(lb)}
    ac, ab, admin_token = login("admin", "admin123")
    cap["login_admin"] = {"status": ac, "body": redact(ab)}

    # ---- 银行只读接口（guest = uid 1，演示富数据账户） ----
    bank_gets = [
        "/api/balance",
        "/api/transactions",
        "/api/transactions/summary",
        "/api/subscriptions",
        "/api/cards",
        "/api/payees",
        "/api/operation_logs",
        "/api/products",
        "/api/holdings",
        "/api/accounts",
        "/api/bank-directory",
        "/api/settlements/return-history?payee_id=1",
        "/api/overview?reveal=true",
        "/api/inbox",
        "/api/inbox/unread_count",
    ]
    cap["bank_readonly"] = {}
    for path in bank_gets:
        code, body = _get(guest_token, path)
        cap["bank_readonly"][path] = {"status": code, "body": body}

    # ---- 后台只读接口（admin token） ----
    admin_gets = [
        "/api/admin/overview",
        "/api/admin/users",
        "/api/admin/logs",
    ]
    cap["admin_readonly"] = {}
    for path in admin_gets:
        code, body = _get(admin_token, path)
        cap["admin_readonly"][path] = {"status": code, "body": body}

    # ---- SSE 事件序列（chat/stream） ----
    cap["sse_chat_stream"] = capture_sse(guest_token, "你好")

    # ---- 关键表 count ----
    cap["table_counts"] = table_counts()

    cap["_meta"] = {
        "proxy": PROXY,
        "bank_api_port": config.BANK_API_PORT,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(cap, f, ensure_ascii=False, indent=2, default=str)
    print(f"readonly capture -> {OUT}  ({time.time() - t0:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
