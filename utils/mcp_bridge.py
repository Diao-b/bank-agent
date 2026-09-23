"""
utils/mcp_bridge.py —— MCP 工具 <-> OpenAI function calling 转换层 + 同步调用桥

职责：
1. mcp_tools_to_openai(mcp_tools)：把 tools/list 结果转成 OpenAI function calling
   格式 —— 名称统一加 "mcp_" 前缀（防与本地银行工具重名），description 追加
   「外部实时金融数据」使用指引，inputSchema（JSON Schema）直接透传。
2. call_mcp_tool_sync(openai_name, args)：execute_tool 的同步入口。
   ★ 全程只读：只透传 MCP 的只读查询工具，绝不包装任何写操作。
   ★ 事件发射（配合 run_server 的 SSE 管道）：
       - 调用开始  -> emit({"type": "tool", "name": "mcp_xxx", ...})     前端加"实时"胶囊
       - 超过 8s   -> emit({"type": "status", "text": "正在查询外部实时数据..."})（仍在等才发）
       - 调用结束  -> emit({"type": "tool_result", "name": "mcp_xxx", ...}) 胶囊转✓、状态条消失
     没有订阅者时 emit 近乎零开销（utils/event_bus.py 快路径）。
3. get_openai_tools()：按当前 MCPClient 状态返回「本地之外的 MCP 工具清单」，
   不可用（未配置 key / 连接失败 / 熔断）时返回 [] —— 由 tools.py 动态降级。
"""
from __future__ import annotations

import itertools
import threading
import time
from typing import Any

import config
from utils import event_bus
from utils.mcp_client import client as mcp_client

MCP_PREFIX = "mcp_"
_UNIQ = itertools.count(1)

# description 统一追加的使用指引（提示 LLM 只在需要外部金融信息时调用）
_EXTRA_SUFFIX = (
    "（外部实时金融数据，可能较慢，仅在用户询问股票行情/市场资讯/基金等"
    "外部金融信息时调用）"
)


# --------------------------------------------------------------------------- #
#  转换：MCP tools/list -> OpenAI function calling
# --------------------------------------------------------------------------- #
def mcp_tools_to_openai(mcp_tools: list[dict]) -> list[dict]:
    """[{name, description, inputSchema}] -> OpenAI tools 列表。

    - name 统一加 mcp_ 前缀（★防与本地工具重名）
    - inputSchema 直接透传（两者同为 JSON Schema）；缺失/非法时补默认空 schema
    """
    out: list[dict] = []
    for t in mcp_tools or []:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        desc = (t.get("description") or "").strip()
        schema = t.get("inputSchema") or {}
        if not isinstance(schema, dict) or schema.get("type") != "object":
            schema = {"type": "object"}
        schema.setdefault("properties", {})
        schema.setdefault("additionalProperties", False)
        out.append({
            "type": "function",
            "function": {
                "name": MCP_PREFIX + name,
                "description": f"{desc}{_EXTRA_SUFFIX}".strip(),
                "parameters": schema,
            },
        })
    return out


def short_label(openai_name: str, description: str = "") -> str:
    """给前端工具胶囊用的短标签：优先用描述首句，兜底剥前缀的工具名。"""
    if description:
        first = description.strip().splitlines()[0].strip()[:12]
        if first:
            return first
    return openai_name[len(MCP_PREFIX):] if openai_name.startswith(MCP_PREFIX) else openai_name


# --------------------------------------------------------------------------- #
#  工具描述速查（tool 事件带 label 用；没查到就退回名字）
# --------------------------------------------------------------------------- #
def _find_raw_description(openai_name: str) -> str:
    if not openai_name.startswith(MCP_PREFIX):
        return ""
    raw = openai_name[len(MCP_PREFIX):]
    for t in mcp_client.get_tools_cache():
        if t.get("name") == raw:
            return t.get("description", "")
    return ""


# --------------------------------------------------------------------------- #
#  同步调用桥（execute_tool 入口）+ 慢调用事件
# --------------------------------------------------------------------------- #
def call_mcp_tool_sync(openai_name: str, args: Any = None) -> dict:
    """执行 mcp_ 前缀工具。永远返回 dict；失败 {"error": "..."}。

    ★ 慢调用体验：调用超过 MCP_SLOW_STATUS_AFTER 秒还没返回时，由独立定时线程
      推一条 status 事件（"正在查询外部实时数据，约需数秒..."），收到结果即消失。
    """
    if not openai_name.startswith(MCP_PREFIX):
        return {"error": f"非法工具名 {openai_name}：MCP 工具必须以 {MCP_PREFIX} 开头"}
    raw_name = openai_name[len(MCP_PREFIX):]

    if not mcp_client.available:
        return {"error": "外部数据源暂时不可用，请稍后再试"}

    call_id = f"{openai_name}-{next(_UNIQ)}"
    desc = _find_raw_description(openai_name)
    event_bus.emit({
        "type": "tool",
        "id": call_id,
        "name": openai_name,
        "label": short_label(openai_name, desc),
        "realtime": True,
    })

    done_flag = threading.Event()

    def _announce_slow() -> None:
        # 只在整个调用确实超过阈值且仍在进行时推一次，避免堆积 status 帧
        if not done_flag.wait(timeout=config.MCP_SLOW_STATUS_AFTER):
            event_bus.emit({"type": "status",
                            "text": "正在查询外部实时数据，约需数秒..."})

    timer = threading.Thread(target=_announce_slow, daemon=True,
                             name=f"mcp-slow-{raw_name}")
    timer.start()

    started = time.time()
    try:
        if isinstance(args, dict):
            clean_args = {k: v for k, v in args.items()
                          if v is not None and not (isinstance(v, str) and v == "")}
        else:
            clean_args = {}
        result = mcp_client.call_tool_sync(raw_name, clean_args)
        if not isinstance(result, dict):
            result = {"ok": True, "content": str(result)}
        return result
    except TimeoutError as exc:
        return {"error": f"外部数据源响应超时：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"外部数据源调用失败：{exc}"}
    finally:
        done_flag.set()
        event_bus.emit({
            "type": "tool_result",
            "id": call_id,
            "name": openai_name,
            "label": short_label(openai_name, desc),
            "elapsed_s": round(time.time() - started, 2),
        })


# --------------------------------------------------------------------------- #
#  动态工具清单（tools.py 用它做 MCP 段注册 / 降级）
# --------------------------------------------------------------------------- #
def get_openai_tools() -> list[dict]:
    """MCP 可用 → 转换后的 OpenAI 工具清单；不可用 → []（纯本地降级）。"""
    if not mcp_client.available:
        return []
    cache = mcp_client.get_tools_cache()
    if not cache:
        return []
    return mcp_tools_to_openai(cache)
