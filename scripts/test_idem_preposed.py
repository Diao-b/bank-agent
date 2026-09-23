"""
scripts/test_idem_preposed.py —— P1.5 幂等键前置 · 验收自测

背景：幂等键原先只在 `execute` 节点内 `uuid4()` 生成，且**直到 HTTP 返回后才随 state 回写**。
若 execute 在「8003 已提交、但状态还没回写」的窗口内被重放（进程被杀 / 异常 / SSE 中断重连），
会生成**新的 key** → 8003 视作新请求 → **重复扣款**。

修复（前置）：把键在 `risk_check`（execute 之前最后一个**不中断**节点）固定并随 checkpoint 持久化；
`fill_params` 在新一笔操作进入解析时清掉上一笔遗留的键。

覆盖：
  1. risk_check 固定并把 `_idem_key` 写进 state.params；
  2. 重放 risk_check（同 params）保持同一 key（setdefault，不换键）；
  3. fill_params 清掉旧 key（新操作 → 键会重新固定成新的）；
  4. execute **复用**前置键（monkeypatch `_http` 捕获 Idempotency-Key）；
  5. execute 无前置键时兜底生成（向后兼容）；
  6. 端到端：8003 对「同 key + 同 body」的回放（idempotent_replay）不重复扣款。

用法（bank-agent/ 下，.venv，需 8003；[6] 会真实转 ¥1，属预期副作用）：
    .venv/Scripts/python.exe scripts/test_idem_preposed.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import config
import profile_db
from agent import write_flow
from utils import op_auth

MOCK = "http://127.0.0.1:%d" % config.BANK_API_PORT
UID = 1
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _params(extra: dict | None = None) -> dict:
    p = {
        "payee_id": 1,
        "amount": 10.0,
        "payee_info": {"nickname": "妈妈", "bank": "招商银行", "real_name": None,
                       "track": "cross_bank", "is_frequent": 0},
    }
    if extra:
        p.update(extra)
    return p


def main() -> int:
    print("=" * 66)
    print(" P1.5 幂等键前置 · 验收自测")
    print("=" * 66)

    # ---------------- 1) risk_check 前置固定 ----------------
    print("\n[1] risk_check 固定 _idem_key（execute 之前、不中断节点）")
    out = write_flow.risk_check({"task": {"action": "transfer"}, "params": _params()})
    key = (out.get("params") or {}).get("_idem_key")
    record("risk_check 生成的键非空", bool(key), f"key={key}")
    record("键已随返回写进 state.params", "_idem_key" in (out.get("params") or {}))

    # ---------------- 2) 重放保持同一 key ----------------
    print("\n[2] 重放 risk_check 不换键（setdefault）")
    out2 = write_flow.risk_check({"task": {"action": "transfer"}, "params": (out.get("params") or {})})
    record("同一操作重放 → 同一 key", (out2.get("params") or {}).get("_idem_key") == key,
           f"{key} vs {(out2.get('params') or {}).get('_idem_key')}")

    # ---------------- 3) fill_params 清旧键 ----------------
    print("\n[3] fill_params 清掉上一笔遗留的键")
    fo = write_flow.fill_params({
        "task": {"action": "transfer", "source": "form",
                 "params": {"payee_id": 1, "amount": 10.0}},
        "params": _params({"_idem_key": "STALE_KEY_FROM_LAST_OP"}),
    })
    fp = fo.get("params") or {}
    record("旧 key 已被清除", not fp.get("_idem_key"), f"params._idem_key={fp.get('_idem_key')}")

    # ---------------- 4)/(5) execute 复用 / 兜底（monkeypatch _http）----------------
    print("\n[4][5] execute 使用前置键 / 无前置键兜底")
    captured: dict = {}
    real_http, real_notify = write_flow._http, write_flow._notify_execution

    def fake_http(method, path, json_body=None, params=None, extra_headers=None):
        captured["path"] = path
        captured["headers"] = dict(extra_headers or {})
        return {"ok": True, "message": "ok", "serial_no": "TRTEST"}

    write_flow._http = fake_http
    write_flow._notify_execution = lambda *a, **k: None
    try:
        write_flow.execute({"task": {"action": "transfer"},
                            "params": _params({"_idem_key": "PINNED_KEY_123"})})
        record("execute 复用前置键", captured["headers"].get("Idempotency-Key") == "PINNED_KEY_123",
               f"got={captured['headers'].get('Idempotency-Key')}")
        captured.clear()
        write_flow.execute({"task": {"action": "transfer"}, "params": _params()})
        gen = captured["headers"].get("Idempotency-Key")
        record("无前置键时兜底生成（向后兼容）", bool(gen) and gen != "PINNED_KEY_123",
               f"generated={gen}")
    finally:
        write_flow._http, write_flow._notify_execution = real_http, real_notify

    # ---------------- 6) 端到端：同 key 回放不重复扣款 ----------------
    print("\n[6] 端到端：8003 同 key + 同 body 回放")
    idem = f"IDEMTEST{int(time.time() * 1000)}"
    hdr = {"Authorization": f"Bearer {profile_db.issue_token(UID)}",
           "X-Op-Token": op_auth.issue_op_token(UID, "transfer")["token"],
           "Idempotency-Key": idem}
    body = {"payee_id": 1, "amount": 1.0}
    try:
        r1 = requests.post(MOCK + "/api/transfer", headers=hdr, json=body, timeout=10).json()
        # 复用同一 key（X-Op-Token 已消费也无妨：回放走中间件，不进入路由依赖）
        r2 = requests.post(MOCK + "/api/transfer", headers=hdr, json=body, timeout=10).json()
        record("首次请求成功", r1.get("ok") is True, f"serial={r1.get('serial_no')}")
        record("★ 同 key 回放被幂等拦截（idempotent_replay）",
               r2.get("ok") is True and r2.get("idempotent_replay") is True, f"r2={str(r2)[:90]}")
        record("★ 回放不重复扣款（余额回执一致）",
               r1.get("balance_after") is not None
               and r1.get("balance_after") == r2.get("balance_after"),
               f"{r1.get('balance_after')} vs {r2.get('balance_after')}")
    except Exception as exc:  # noqa: BLE001
        record("端到端幂等回放", False, f"8003 未启动？{exc}")

    print("\n" + "=" * 66)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f" 结果：{passed}/{total} 通过")
    for n, ok, _ in RESULTS:
        if not ok:
            print("   -", n)
    print("=" * 66)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
