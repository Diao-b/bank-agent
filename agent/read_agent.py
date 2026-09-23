"""
agent/read_agent.py —— 自由 Agent（只读子图）

图结构：
    START → llm（带 tools=TOOLS 调用 LLM）
              ├─ 有 tool_calls 且未超轮次预算 → tools（执行工具并追加 tool 消息）→ 回到 llm
              └─ 无 tool_calls / 已达轮次预算（config.READ_AGENT_MAX_TOOL_ROUNDS）→ END
    ★ 轮次预算是硬约束：超限后 llm 节点不再下发工具，强制出文本收尾，
      从根本上杜绝「LLM 连续调工具 → 前端永远正在输入」的死循环。

M1：单独配 SqliteSaver(read_test.db)，方便 run_cli 独立测多轮。
M3：把本图裸编译后作为节点嵌入主图（届时传入主图的 checkpointer 即可），
    因此这里不散落任何全局 graph / client 变量，一律通过 build_read_agent() 返回。
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from langgraph.graph import END, START, StateGraph
from openai import OpenAI

import config
from utils.security import get_current_user_id
from .prompts import read_agent_system_prompt
from .state import ReadState
from .tools import execute_tool, get_llm_tools, parse_tool_call

# --------------------------------------------------------------------------- #
#  LLM Client（可注入，便于离线自测）
# --------------------------------------------------------------------------- #
_LLM_CLIENT = None  # 由 build_read_agent(llm_client=...) 注入；None 则构造 DeepSeek


def build_client() -> OpenAI:
    # ★ 显式超时/重试：SDK 默认 timeout=600s、max_retries=2，
    #   上游卡住时对话会无限「正在输入」；收紧后失败能快速浮出。
    return OpenAI(
        api_key=config.require_llm_key(),
        base_url=config.DEEPSEEK_BASE_URL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    )


def _client() -> Any:
    return _LLM_CLIENT if _LLM_CLIENT is not None else build_client()


# --------------------------------------------------------------------------- #
#  消息格式转换：LangChain Message <-> OpenAI dict
# --------------------------------------------------------------------------- #
def _to_openai_messages(messages: list[Any]) -> list[dict]:
    """把 state 里的消息（LangChain 对象或 dict）转成 OpenAI chat 格式。"""
    out: list[dict] = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role", "user")
            content = m.get("content") or ""
            item: dict[str, Any] = {"role": role, "content": content}
            if m.get("tool_calls"):
                item["tool_calls"] = m["tool_calls"]
            if m.get("tool_call_id"):
                item["tool_call_id"] = m["tool_call_id"]
            if m.get("name"):
                item["name"] = m["name"]
            out.append(item)
            continue

        mtype = getattr(m, "type", "human")
        content = getattr(m, "content", "") or ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        role = {
            "human": "user",
            "ai": "assistant",
            "tool": "tool",
            "system": "system",
        }.get(mtype, mtype or "user")
        item = {"role": role, "content": content}

        if mtype == "ai":
            tool_calls = getattr(m, "tool_calls", None) or []
            if tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.get("id"),
                        "type": "function",
                        "function": {
                            "name": tc.get("name"),
                            "arguments": json.dumps(tc.get("args") or {}, ensure_ascii=False),
                        },
                    }
                    for tc in tool_calls
                ]
        elif mtype == "tool":
            item["tool_call_id"] = getattr(m, "tool_call_id", None)
            name = getattr(m, "name", None)
            if name:
                item["name"] = name
        out.append(item)
    return out


def _openai_tool_calls_to_dicts(tool_calls: list[Any]) -> list[dict]:
    """OpenAI SDK 返回的 tool_calls 对象 -> 纯 dict（带上 id，回写 state 用）。"""
    dicts = []
    for tc in tool_calls or []:
        dicts.append(
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
        )
    return dicts


# --------------------------------------------------------------------------- #
#  工具轮次预算（防死循环）
# --------------------------------------------------------------------------- #
def _role_of(m: Any) -> str:
    """统一取消息角色：state 里可能是 LangChain 对象（角色在 .type）或 OpenAI dict（.role）。"""
    if isinstance(m, dict):
        return m.get("role", "") or ""
    t = getattr(m, "type", "") or ""
    return {"human": "user", "ai": "assistant"}.get(t, t)


def _tool_rounds_this_turn(messages: list[Any]) -> int:
    """统计「本轮」已执行的工具轮数 = 最后一条用户消息之后产生的 tool 消息条数。

    ★ 只算本轮，不累加历史轮次 —— 否则多轮对话里总轮数很快触顶，
      后续轮次就再也调不动工具了。
    """
    last_user = -1
    for i, m in enumerate(messages):
        if _role_of(m) == "user":
            last_user = i
    return sum(1 for m in messages[last_user + 1:] if _role_of(m) == "tool")


# --------------------------------------------------------------------------- #
#  节点
# --------------------------------------------------------------------------- #
def _llm_node(state: ReadState) -> dict:
    """调用 LLM（tools=get_llm_tools(), temperature=0）。

    ★ 工具清单每轮动态拉取：启动自检连上百炼 MCP 后，本轮开始自动带上 mcp_ 外部
      金融工具；运行中 MCP 熔断/断连后，下一轮自动摘掉 —— 动态降级无需重启。
    每轮调 LLM 前，在 messages 头部插一条【用户画像】system 消息：
    - 只注入画像摘要（称呼/姓名/性别/账户/偏好/当前时间），不注入敏感字段
    - 这条 system 消息对用户不可见，仅用于约束称呼与推荐口径
    - 登录态每轮刷新，PUT profile 后即时生效
    """
    client = _client()
    system_prompt = read_agent_system_prompt(date.today())
    user_ctx_prompt = ""
    try:
        from profile_db import build_user_context_system_prompt

        # 当前登录用户（HTTP 由 token 解析；CLI 无上下文回落默认演示用户）。
        # 以前写死 DEFAULT_USER_ID：B 登录后 Agent 仍用 A 的画像称呼/偏好来应答。
        uid = get_current_user_id() or config.DEFAULT_USER_ID
        user_ctx_prompt = build_user_context_system_prompt(uid)
    except Exception:  # noqa: BLE001  DB 不可用时跳过画像注入，不影响主对话
        pass
    messages = [{"role": "system", "content": system_prompt}]
    if user_ctx_prompt:
        messages.insert(0, {"role": "system", "content": user_ctx_prompt})
    messages = messages + _to_openai_messages(state.get("messages", []))

    # C7+ 知识库 RAG 真接线：按本轮用户问题检索 docs/knowledge 片段，
    # 命中（余弦 ≥ 阈值）则作为第二条 system 注入 —— 风险揭示 / 安全引导 /
    # 业务规则原文从此对 Agent 可见，口径与知识文档一致。
    try:
        from utils import knowledge as _knowledge

        def _msg_role(m):
            # state 里是 LangChain 对象（角色在 .type='human'）或 dict（.role）
            if isinstance(m, dict):
                return m.get("role", "")
            return {"human": "user", "ai": "assistant"}.get(getattr(m, "type", ""), getattr(m, "type", ""))

        def _msg_content(m):
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            return c if isinstance(c, str) else ""

        _last_user = next((_msg_content(m) for m in reversed(state.get("messages", []))
                           if _msg_role(m) == "user" and _msg_content(m)), "")
        if _last_user:
            _snips = _knowledge.snippets_for_prompt(_last_user, top_k=3)
            if _snips:
                messages.insert(1, {"role": "system", "content": _snips})
                print(f"[knowledge] 注入 {len(_snips)} 字知识片段（query={_last_user[:30]}）")
    except Exception:  # noqa: BLE001  检索失败不影响主对话
        pass

    # ★ 轮次预算：已达上限则「摘掉工具」再问一次 —— 强制模型基于已有工具结果出文本，
    #   避免 llm→tools→llm 无限循环（前端表现就是永远「小微正在输入」）。
    over_budget = _tool_rounds_this_turn(
        state.get("messages", [])
    ) >= config.READ_AGENT_MAX_TOOL_ROUNDS
    req: dict[str, Any] = {
        "model": config.DEEPSEEK_MODEL,
        "messages": messages,
        # 技术约束：所有 LLM 调用 temperature=0，保证结果可复现。
        # config.LLM_TEMPERATURE 保留给后续确实需要非确定性的场景（如话术润色），
        # 但读操作 Agent 的每一次调用都强制为 0。
        "temperature": 0,
    }
    if not over_budget:
        req["tools"] = get_llm_tools()
        req["tool_choice"] = "auto"
    else:
        print(f"[read_agent] 工具轮次达上限 {config.READ_AGENT_MAX_TOOL_ROUNDS}，本轮强制收尾出文本")
    resp = client.chat.completions.create(**req)
    msg = resp.choices[0].message
    payload: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        payload["tool_calls"] = _openai_tool_calls_to_dicts(msg.tool_calls)
    return {"messages": [payload]}


def _tools_node(state: ReadState) -> dict:
    """执行上一轮 assistant 消息里的工具调用，追加 tool 消息。"""
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None)
    if not tool_calls and isinstance(last, dict):
        tool_calls = last.get("tool_calls")

    outputs: list[dict] = []
    for tc in tool_calls or []:
        tc_id, name, args = parse_tool_call(tc)
        result = execute_tool(name, args)
        outputs.append(
            {
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            }
        )
    return {"messages": outputs}


def _should_continue(state: ReadState) -> str:
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None)
    if not tool_calls and isinstance(last, dict):
        tool_calls = last.get("tool_calls")
    if not tool_calls:
        return END
    # ★ 硬兜底：即使模型无视「已摘掉工具」仍返回 tool_calls，也绝不再进 tools 节点。
    #   宁可这一轮回复偏短，也不能让图无限自转把前端挂死。
    if _tool_rounds_this_turn(state.get("messages", [])) >= config.READ_AGENT_MAX_TOOL_ROUNDS:
        return END
    return "tools"


# --------------------------------------------------------------------------- #
#  构图
# --------------------------------------------------------------------------- #
def build_read_agent(checkpointer=None, llm_client=None):
    """编译并返回自由 Agent（只读）子图。

    M3 重构：本函数只返回「裸编译子图」——不自带 checkpointer。
    独立测试时由调用方显式传入 SqliteSaver；嵌入主图时由主图统一配一份
    （SqliteSaver("bank.db")），避免 checkpoint 分裂、thread 语义错乱。

    input/output schema = ReadState（只有 messages 一个 channel），
    保证作为节点嵌入主图时只交换 messages，状态适配由 schema 自动完成。
    """
    global _LLM_CLIENT
    _LLM_CLIENT = llm_client

    graph = StateGraph(ReadState, input_schema=ReadState, output_schema=ReadState)
    graph.add_node("llm", _llm_node)
    graph.add_node("tools", _tools_node)

    graph.add_edge(START, "llm")
    graph.add_conditional_edges("llm", _should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "llm")

    # checkpointer=None 即「裸编译」，由上层（主图）统一持有持久化。
    return graph.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    # 结构自检：不需要 LLM / 数据库即可确认图能编译
    g = build_read_agent()
    print("read_agent 编译成功，节点：", list(g.get_graph().nodes))
