"""
scripts/test_mcp.py —— 百炼 MCP（东方财富妙想）连通性直连验证

不经 Agent：连接 → 打印工具清单 → 挑一个行情类工具调用一次 → 打印结果。
填好 bank-agent/.env 的 DASHSCOPE_API_KEY 后 30 秒内可验证。

用法（在 bank-agent/ 目录执行）：
    .venv/Scripts/python.exe scripts/test_mcp.py                 # 默认流程（最多 2 次调用）
    .venv/Scripts/python.exe scripts/test_mcp.py --list-only     # 只打印清单，不调用
    .venv/Scripts/python.exe scripts/test_mcp.py --tool get_stock_quote --args '{"code":"600519"}'
    .venv/Scripts/python.exe scripts/test_mcp.py --timeout 45

★ 计费提醒：调用产生真实费用 —— 默认只挑 1 个工具、最多自动尝试 2 组参数，
  绝不循环调用全部 11 个工具。--list-only 完全不产生费用。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from utils import mcp_client  # noqa: E402


# 参数猜测用的候选值（覆盖常见行情工具入参命名）
_KEY_POOLS: list[tuple[tuple[str, ...], list[str]]] = [
    (("code", "symbol", "stockcode", "stock_code", "scode", "secid", "ts_code", "marketcode", "stock"),  # noqa: E501
     ["600519", "sh600519", "600519.SH"]),
    (("keyword", "name", "stockname", "q", "query", "text", "search"),
     ["贵州茅台", "600519"]),
    (("date", "startdate", "enddate", "start", "end", "datefrom", "dateto"),
     [date.today().isoformat(), f"{date.today().year}-01-01"]),
]


def _match_key(key: str) -> tuple[str, list[str]] | None:
    k = (key or "").lower().replace("_", "").replace("-", "")
    for keys, pool in _KEY_POOLS:
        for cand in keys:
            if cand.lower().replace("_", "") in k or k in cand.lower().replace("_", ""):
                return cand, pool
    return None


def _guess_args(schema: dict, tool_name: str, description: str) -> list[dict]:
    """按 inputSchema 的 required 字段猜参数，返回最多 2 组候选参数。"""
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    if not required and not props:
        return [{}]

    def _fill(variant: int) -> dict:
        args: dict = {}
        for key, meta in props.items():
            if required and key not in required:
                continue  # 非必填不填，降低猜错概率
            m = _match_key(key)
            hint = f"{key} {(meta or {}).get('description', '')}".lower()
            if m:
                pool = m[1]
                args[key] = pool[variant % len(pool)]
            elif "茅台" in hint or "贵州茅台" in hint or "股票" in hint or "行情" in hint:
                args[key] = "600519"
            elif (meta or {}).get("type") in ("integer", "number"):
                args[key] = 1
            else:
                args[key] = "贵州茅台" if variant else "600519"
        return args

    out = [_fill(0)]
    if len(required) > 1:  # 第一组全填主候选；第二组替换成另一候选组合
        out.append(_fill(1))
    return out


def _pick_target(tools: list[dict], explicit: str | None) -> dict:
    if explicit:
        for t in tools:
            if t["name"] == explicit:
                return t
        print(f"!! 工具 {explicit} 不在清单里，改取首个工具")
    # 行情/股价类工具的常见关键词
    KEY = ("行情", "quote", "price", "stock", "股票", "market", "realtime", "实时")
    for t in tools:
        blob = f"{t['name']} {t.get('description', '')}".lower()
        if any(k.lower() in blob for k in KEY):
            return t
    return tools[0]


def _print_tool(t: dict) -> None:
    desc = (t.get("description") or "").replace("\n", " ").strip()
    schema = t.get("inputSchema") or {}
    req = schema.get("required") or []
    req_txt = f" 必填: {', '.join(req)}" if req else ""
    print(f"    - {t['name']}{req_txt}")
    if desc:
        print(f"        {desc[:96]}{'…' if len(desc) > 96 else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description="百炼 MCP（东方财富妙想）连通性直连验证")
    ap.add_argument("--list-only", action="store_true", help="只打印工具清单，不调用（零费用）")
    ap.add_argument("--tool", default=None, help="指定要调用的工具原名（不带 mcp_ 前缀）")
    ap.add_argument("--args", default=None, help='调用参数 JSON，如 \'{"code":"600519"}\'')
    ap.add_argument("--timeout", type=float, default=None, help="单次调用超时（默认取 .env MCP_TOOL_TIMEOUT）")
    args = ap.parse_args()

    print("=" * 70)
    print(" 阿里云百炼 MCP · 东方财富妙想 —— 连通性验证")
    print(f" endpoint : {config.MCP_ENDPOINT}")
    print(f" api_key  : {'已配置（前8位 ' + config.DASHSCOPE_API_KEY[:8] + '***）' if config.DASHSCOPE_API_KEY else '未配置（.env 里填 DASHSCOPE_API_KEY）'}")
    print("=" * 70)

    if not config.DASHSCOPE_API_KEY:
        print("!! 请先在 bank-agent/.env 填写 DASHSCOPE_API_KEY=sk-... 后重试")
        sys.exit(2)

    # 1) 连接 + 发现工具
    print("\n[1/3] 正在连接并握手（失败自动重试 2 次，间隔 3s）…")
    result = mcp_client.ensure_connected_sync()
    status = result["status"]
    if not result.get("ok"):
        print(f"!! 连接失败：reason={status.get('reason')} error={status.get('last_error')}")
        sys.exit(1)

    tools = result["tools"]
    print(f"✓ 已连接。tools/list 返回 {len(tools)} 个工具：\n")
    for t in tools:
        _print_tool(t)
    print()

    if args.list_only:
        print("(--list-only：未产生任何调用费用)")
        return

    # 2) 挑目标工具（默认行情类）
    target = _pick_target(tools, args.tool)
    print(f"[2/3] 目标工具：{target['name']}")

    # 3) 调用（默认 ≤2 组参数；显式 --args 只调一次）
    print("[3/3] 调用 tools/call …")
    if args.args is not None:
        arg_candidates: list[dict] = [json.loads(args.args)]
    else:
        arg_candidates = _guess_args(target.get("inputSchema") or {},
                                     target["name"], target.get("description", ""))

    last: dict = {}
    for i, a in enumerate(arg_candidates, start=1):
        print(f"     尝试 {i}/{len(arg_candidates)}：{target['name']}({json.dumps(a, ensure_ascii=False)})")
        try:
            last = mcp_client.client.call_tool_sync(
                target["name"], a, timeout=args.timeout or config.MCP_TOOL_TIMEOUT + 5)
        except Exception as exc:  # noqa: BLE001
            last = {"error": f"调用异常：{exc}"}
        print(f"     → {json.dumps(last, ensure_ascii=False, default=str)[:800]}")
        if last.get("ok"):
            break

    print()
    if last.get("ok"):
        print("✓ PASS：至少一次调用成功，返回真实外部数据（内容见上，含来源与时间时以工具返回为准）")
    else:
        print("✗ FAIL：未能拿到成功结果 —— 若工具名/参数不同，用 --tool/--args 显式指定重试；")
        print("  常见行情代码格式：600519 / sh600519 / 600519.SH（贵州茅台）")


if __name__ == "__main__":
    main()
