"""
run_regression.py —— 运行全部回归脚本并归档 stdout（行为基线黄金 / 重构后比对）

覆盖「转账全流程 / 订阅 / 卡片 / 账户体系 / 站内 / 跨行 / 实名 / 卡号校验」八大回归，
是行为零变化的权威证据（比只读形态捕捉更深的语义）。每套脚本自洽幂等（相对不变量 + 清理），
配合 db_snapshot restore 可逐次复现。

用法：
    .venv/Scripts/python.exe tests/baseline/run_regression.py [--out tests/baseline/captures/baseline_tests]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures", "baseline_tests")
# 每套脚本前先把库还原到快照 A：保证每套都在「原始状态」独立运行（避免跨套件状态污染，
# 例如跨行/站内转账会改变 uid1 余额，导致后续套件误报「余额不足」）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_snapshot  # noqa: E402

# (脚本路径, 简称) —— 顺序即验收顺序
SUITES = [
    ("scripts/test_crossbank_settlement.py", "crossbank"),
    ("scripts/test_insite_transfer.py", "insite"),
    ("scripts/test_account_system.py", "account_system"),
    ("scripts/test_card_apply.py", "card_apply"),
    ("scripts/test_internal_transfer_flow.py", "internal"),
    ("scripts/test_subscription_flow.py", "subscription"),
    ("scripts/test_realname_verified.py", "realname"),
    ("tests/test_card.py", "card"),
]


def main() -> int:
    os.makedirs(OUTDIR, exist_ok=True)
    summary: dict = {}
    all_ok = True
    for rel, name in SUITES:
        path = os.path.join(ROOT, rel)
        db_snapshot.restore()  # 还原到快照 A，保证独立可复现
        logp = os.path.join(OUTDIR, name + ".log")
        t0 = time.time()
        try:
            rc = subprocess.run([VENV, path], cwd=ROOT,
                                stdout=open(logp, "w", encoding="utf-8"),
                                stderr=subprocess.STDOUT, timeout=300).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        dur = round(time.time() - t0, 1)
        tail = ""
        with open(logp, encoding="utf-8") as lf:
            for ln in reversed(lf.read().splitlines()):
                if any(k in ln for k in ("PASS", "FAIL", "通过", "GREEN", "结果")):
                    tail = ln.strip()
                    break
        summary[name] = {"rc": rc, "log": os.path.relpath(logp, ROOT),
                         "duration_s": dur, "tail": tail}
        ok = rc == 0
        all_ok = all_ok and ok
        print(f"  [{'OK' if ok else 'NONZERO'}] {name} rc={rc} ({dur}s) {tail}")
    with open(os.path.join(OUTDIR, "_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"regression summary -> {os.path.join(OUTDIR, '_summary.json')}  "
          f"({'ALL GREEN' if all_ok else 'HAS FAILURE'})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
