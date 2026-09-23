# -*- coding: utf-8 -*-
"""知识库检索：性能 + 阈值选值核查（报告用，不改数据）。

跑法：cd bank-agent && ./.venv/Scripts/python.exe scripts/_knowledge_check.py

回答两个问题：
  1) 每次检索到底多慢？（含缓存命中后的对比）
  2) SCORE_FLOOR 取多少合适？—— 寒暄类应当「一个片段都不注入」，
     真规则类问题应当「命中且排在前面」。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils import knowledge as k  # noqa: E402

# (问题, 期望是否应该命中知识库)
CASES = [
    ("你好呀", False),
    ("在吗", False),
    ("谢谢", False),
    ("查一下我的余额", False),          # 余额靠工具查数，不需要知识库口径
    ("这个月钱都花哪了", False),
    ("转账限额是多少", True),
    ("跨行转账多久到账", True),
    ("转账要手续费吗", True),
    ("取消订阅会退款吗", True),
    ("遇到诈骗电话怎么办", True),
    ("隐私模式是什么", True),
    ("支付密码输错几次会锁定", True),
]

print("=" * 96)
print("按当前 SCORE_FLOOR =", k.SCORE_FLOOR, "（不看阈值，先打印原始 top3 分数）")
print("=" * 96)

rows = []
for q, should in CASES:
    t0 = time.perf_counter()
    hits_raw = k.search(q, top_k=3)
    ms1 = (time.perf_counter() - t0) * 1000
    t1 = time.perf_counter()
    k.search(q, top_k=3)                      # 第二次：应命中缓存
    ms2 = (time.perf_counter() - t1) * 1000
    top = hits_raw[0]["score"] if hits_raw else 0.0
    rows.append((q, should, top, len(hits_raw), ms1, ms2, hits_raw))

print(f"{'问题':<22}{'应命中':<7}{'top1分':<9}{'注入':<6}{'首次ms':<10}{'缓存ms':<9}命中片段")
for q, should, top, n, ms1, ms2, hits in rows:
    flag = "✓" if (n > 0) == should else "✗"
    heads = " / ".join(f"{h['heading'][:12]}({h['score']})" for h in hits[:3]) or "（无）"
    print(f"{q:<22}{str(should):<7}{top:<9}{n:<6}{ms1:<10.0f}{ms2:<9.1f}{flag} {heads}")

print()
firsts = [r[4] for r in rows]
print(f"首次检索耗时：中位 {sorted(firsts)[len(firsts)//2]:.0f} ms | 最大 {max(firsts):.0f} ms")
print(f"缓存命中耗时：中位 {sorted(r[5] for r in rows)[len(rows)//2]:.1f} ms")

# ---- 阈值扫描：看哪个 floor 能让「应命中/不应命中」同时成立 ----
print()
print("=" * 96)
print("阈值扫描（只看 top1 分数）")
print("=" * 96)
for floor in (0.30, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
    wrong = []
    for q, should, top, _, _, _, _ in rows:
        got = top >= floor
        if got != should:
            wrong.append(q)
    print(f"  floor={floor:<6} 误判 {len(wrong):>2} 条  {('：' + '、'.join(wrong)) if wrong else '（全部正确）'}")
