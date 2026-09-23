"""
run_regression.py —— 运行全部回归脚本并归档 stdout（行为基线黄金 / 重构后比对）

覆盖「转账全流程 / 订阅 / 卡片 / 账户体系 / 站内 / 跨行 / 实名 / 卡号校验」八大回归，
是行为零变化的权威证据（比只读形态捕捉更深的语义）。每套脚本自洽幂等（相对不变量 + 清理），
配合 db_snapshot restore 可逐次复现。

用法：
    .venv/Scripts/python.exe tests/baseline/run_regression.py [--out tests/baseline/captures/baseline_tests]
    .venv/Scripts/python.exe tests/baseline/run_regression.py --no-restore     # 不还原（只跑，调试用）
    .venv/Scripts/python.exe tests/baseline/run_regression.py --force-restore  # ★危险：跳过代际校验强行还原

★ 安全护栏（P2.2）
----------------
每套前 `db_snapshot.restore()` 是 **DELETE + INSERT 全表覆盖**。若快照与当前库**不是同一代**
（真实事故：9-11 录的 53-user 快照 vs 治理后的 13-user 库），会把治理成果整体抹掉。
现在 `restore()` 带代际指纹校验，**不同代一律拒绝**，本脚本据此**开跑前**就中止（rc=2）并提示重录快照。
"""
from __future__ import annotations

import argparse
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
    ap = argparse.ArgumentParser(description="运行全部回归脚本")
    ap.add_argument("--out", default=OUTDIR)
    ap.add_argument("--no-restore", action="store_true", help="每套前不还原快照（调试用）")
    ap.add_argument("--force-restore", action="store_true",
                    help="★危险：跳过快照代际校验强行还原（会把当前库覆盖成快照内容）")
    args = ap.parse_args()

    outdir = args.out
    os.makedirs(outdir, exist_ok=True)

    if not args.no_restore:
        try:
            db_snapshot.check()   # 只读预检：不同代就别开跑（避免跑一半才炸）
        except db_snapshot.SnapshotMismatch as exc:
            print("=" * 72)
            print("[中止] 快照与当前库不同代 —— 拒绝开始回归（避免把整库回滚）。")
            print(exc)
            print("=" * 72)
            return 2

    summary: dict = {}
    all_ok = True
    for rel, name in SUITES:
        path = os.path.join(ROOT, rel)
        if not args.no_restore:
            try:
                db_snapshot.restore(force=args.force_restore)  # 还原到快照 A，保证独立可复现
            except db_snapshot.SnapshotMismatch as exc:
                print(f"  [中止] {name} 前还原被拒：{exc}")
                return 2
        logp = os.path.join(outdir, name + ".log")
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
    with open(os.path.join(outdir, "_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"regression summary -> {os.path.join(outdir, '_summary.json')}  "
          f"({'ALL GREEN' if all_ok else 'HAS FAILURE'})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
