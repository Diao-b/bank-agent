"""
diff_captures.py —— 比较两个只读基线 JSON，报告是否逐字节一致（忽略已知 volatile）

compare 的是「已脱敏」的基线（capture_readonly.py 已把 token / 时间戳脱敏），
所以理论上重构前后应 IDENTICAL。出现 diff 即行为变化信号。

用法：
    .venv/Scripts/python.exe tests/baseline/diff_captures.py <base.json> <run.json>
"""
from __future__ import annotations

import json
import sys


def load(p: str):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def diff(a, b, path: str = ""):
    out = []
    if type(a) is not type(b):
        out.append((path or "<root>", f"type {type(a).__name__} != {type(b).__name__}"))
        return out
    if isinstance(a, dict):
        ka, kb = set(a), set(b)
        for k in ka - kb:
            out.append((path + "." + k, "only in base"))
        for k in kb - ka:
            out.append((path + "." + k, "only in run"))
        for k in ka & kb:
            out += diff(a[k], b[k], path + "." + k)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append((path, f"len {len(a)} != {len(b)}"))
        for i in range(min(len(a), len(b))):
            out += diff(a[i], b[i], f"{path}[{i}]")
    else:
        if a != b:
            out.append((path, f"{a!r} != {b!r}"))
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: diff_captures.py <base.json> <run.json> [--ignore k1,k2]")
        return 2
    ignore = set()
    args = sys.argv[1:3]
    rest = sys.argv[3:]
    for i, a in enumerate(rest):
        if a == "--ignore" and i + 1 < len(rest):
            ignore = set(rest[i + 1].split(","))
    base, run = load(args[0]), load(args[1])
    # 顶层忽略环境相关 / 元数据键（如 sse_chat_stream、_meta）
    for k in ignore:
        base.pop(k, None)
        run.pop(k, None)
    d = diff(base, run)
    # 子树忽略：append-only 日志 / 通知类端点会被「捕获过程自身」的 SSE·agent 动作污染
    # （每次登录/对话都会追加行，自增 id 与时间戳无关、不可复现）。这类端点的响应不是
    # 「DB 状态 → 响应」的纯函数，列为环境相关面，按路径子串跳过（与 sse_chat_stream 同处理）。
    if ignore:
        d = [x for x in d if not any(tok in x[0] for tok in ignore)]
    if not d:
        print(f"IDENTICAL  ({args[0]} == {args[1]})"
              + (f"  [ignored: {','.join(sorted(ignore))}]" if ignore else ""))
        return 0
    print(f"DIFFER ({len(d)} diffs)"
          + (f"  [ignored: {','.join(sorted(ignore))}]" if ignore else "") + ":")
    for p, m in d[:80]:
        print(f"  {p}: {m}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
