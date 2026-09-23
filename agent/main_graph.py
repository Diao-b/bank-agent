"""
agent/main_graph.py —— M3 主图：自由规划 + 确定执行 的跨场景联动

设计核心（违反任何一条都算实现错误）：
1. 流程走向 100% 由代码条件边决定（route_after_*），LLM 只做：
   - understand：意图理解 + 单任务要素抽取（JSON）
   - planner：联动任务链拆解（JSON）
   - respond：联动汇总（本实现用确定性模板，数字只来自 task_results）
   LLM 永远不决定「下一步走哪」。
2. 两个子图（read_agent / write_flow）以「裸编译子图」直接作为节点：
   g.add_node("read_agent", read_graph) / g.add_node("write_flow", write_graph)
   ★禁止用函数包一层 sub.invoke() 适配状态——会打断 interrupt 的
     挂起 / 冒泡 / Command(resume) 穿透链。状态适配用 input/output schema 完成。
3. 子图一律不配 checkpointer；主图 compile 时统一给一份 SqliteSaver("bank.db")，
   否则 checkpoint 分裂、thread 语义错乱。
4. 三个写中断（clarify/verify/confirm）对主图完全透明：主图路由只认
   task_plan 是否为空，从不感知子图内部层级。
5. 安全双保险：planner 给的 params 仅是「建议值」，write_flow 的 confirm 仍向用户
   复述金额/收款人、risk_check 仍按硬规则算验证码，任何注入都过不了这两关。

MainState / 节点 / 路由 / build_main_graph 都在这一个文件里。
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from openai import OpenAI

import config
from .prompts import PLANNER_SYSTEM_PROMPT, UNDERSTAND_SYSTEM_PROMPT
from .read_agent import build_read_agent
from .state import WriteState  # noqa: F401  (仅用于类型自检)
from .tools import READ_TOOL_NAMES
from .write_flow import ACTION_SPECS, build_write_flow

# --------------------------------------------------------------------------- #
#  LLM Client（可注入，便于离线自测）
# --------------------------------------------------------------------------- #
_LLM_CLIENT = None  # 由 build_main_graph(llm_client=...) 注入；None 则构造 DeepSeek


def build_client() -> OpenAI:
    # ★ 显式超时/重试（SDK 默认 600s/2 次）：避免上游卡住时对话无限挂起。
    return OpenAI(
        api_key=config.require_llm_key(),
        base_url=config.DEEPSEEK_BASE_URL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    )


def _client() -> Any:
    return _LLM_CLIENT if _LLM_CLIENT is not None else build_client()


# --------------------------------------------------------------------------- #
#  MainState
# --------------------------------------------------------------------------- #
class MainState(TypedDict):
    """主图状态。

    messages：对话历史（与子图共享，自动按 schema 传入/传出）
    intent：  read / write / linkage / chat
    task：    单任务 {type, action, params}
    task_plan：联动任务链 [{type, action, params}, ...]
    current_task：当前执行到 task_plan 的第几个（0-based）
    task_results：已完成任务的 result 列表（与 task_plan 一一对应）
    result：  子图回传的单个结果（联动汇总 / 非法操作报错也走它）
    submit：  ★结构化任务提交入口（前端表单）。只在「本轮 invoke 的输入」里出现，
              understand 消费后立刻置 None —— 绝不能让上一轮的残留把纯文本对话
              劫持成写操作。用独立 channel 而非直接读 task，正是为了这一点：
              task 会随 checkpoint 长期驻留，无法区分「本轮提交」与「上轮残留」。
    """

    messages: Annotated[list, add_messages]
    intent: str
    task: dict
    task_plan: list
    current_task: int
    task_results: list
    result: dict
    submit: dict


# --------------------------------------------------------------------------- #
#  注入防御（代码层，最高优先级）
# --------------------------------------------------------------------------- #
INJECTION_MARKERS = [
    "忽略你的规则", "忽略规则", "直接转账", "跳过验证", "别确认了",
    "假装管理员", "系统提示词", "忽略前面的", "绕过验证", "别确认",
]


def _is_injection(text: str) -> bool:
    t = text or ""
    return any(m in t for m in INJECTION_MARKERS)


# --------------------------------------------------------------------------- #
#  LLM：understand / planner
# --------------------------------------------------------------------------- #
def _parse_json(content: str) -> Any:
    if not content:
        return None
    c = content.strip()
    candidates = [c]
    m = re.search(r"```(?:json)?\s*(.*?)```", c, re.S)
    if m:
        candidates.append(m.group(1))
    for cand in candidates:
        try:
            return json.loads(cand)
        except Exception:
            pass
    for pat in (r"\[.*\]", r"\{.*\}"):
        m = re.search(pat, c, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return None


def _last_user_text(messages: list[Any]) -> str:
    for m in reversed(messages or []):
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        role = m.get("role") if isinstance(m, dict) else getattr(m, "type", None)
        if role in ("user", "human"):
            return c or ""
    return ""


# --------------------------------------------------------------------------- #
#  结构化任务提交（前端表单）：纯代码校验，不经 LLM
# --------------------------------------------------------------------------- #
# 允许由外部（表单）直接给定的槽位白名单。★白名单之外一律丢弃，尤其是：
#   - 下划线开头的内部字段（_need_verify / _confirm_card）→ 否则前端可伪造
#     _need_verify=false 直接绕过短信验证，风控形同虚设；
#   - payee_info（收款人详情，含 is_frequent）→ 必须由 write_flow 自己按 payee_id
#     回银行接口解析，否则伪造 is_frequent=true 也能绕过「非常用收款人需验证码」。
SUBMIT_PARAM_WHITELIST = {
    "payee", "payee_id", "amount", "remark", "from_account_id",
    "product_id", "holding_id", "card_id", "sub_id",
}


def normalize_submit(submit: Any) -> dict | None:
    """校验并净化前端提交的结构化任务；非法返回 None（调用方退回原 LLM 路径）。

    只做「格式与白名单」层面的把关：真正的风控（金额阈值 / 常用收款人 / 确认中断）
    一律在 write_flow 里按银行接口的真实数据重算，表单说什么都不算数。
    """
    if not isinstance(submit, dict):
        return None
    if submit.get("type") != "write":
        return None
    action = submit.get("action")
    if action not in ACTION_SPECS:
        return None
    raw = submit.get("params")
    raw = raw if isinstance(raw, dict) else {}
    params: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or k.startswith("_") or k not in SUBMIT_PARAM_WHITELIST:
            continue
        if v is None or v == "":
            continue
        params[k] = v
    # 金额：表单可能传字符串，这里统一成 float；非法直接丢弃（后续 clarify 会追问）
    if "amount" in params:
        try:
            amt = float(params["amount"])
        except (TypeError, ValueError):
            amt = None
        if amt is None or amt <= 0:
            params.pop("amount", None)
        else:
            params["amount"] = amt
    # remark 只用于确认卡展示，长度设硬上限，避免把大段文本灌进确认摘要
    if isinstance(params.get("remark"), str):
        params["remark"] = params["remark"].strip()[:40]
        if not params["remark"]:
            params.pop("remark")
    # source=form：给 write_flow.fill_params 的信号——参数已由表单给齐，跳过 LLM 抽取
    return {"type": "write", "action": action, "params": params, "source": "form"}


def _call_understand(text: str) -> tuple[str, Any]:
    """返回 (intent, task)。代码层注入防御优先；失败兜底 chat。"""
    if _is_injection(text):
        return "chat", None
    try:
        resp = _client().chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": UNDERSTAND_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )
        data = _parse_json(resp.choices[0].message.content)
    except Exception:  # noqa: BLE001
        data = None
    if not isinstance(data, dict):
        return "chat", None
    intent = data.get("intent")
    if intent not in ("read", "write", "linkage", "chat"):
        intent = "chat"
    task = data.get("task") if intent in ("read", "write") else None
    return intent, task


def _call_planner(text: str, retry: bool = False) -> list | None:
    """返回任务链 list；解析/校验失败返回 None（触发重试）。"""
    try:
        sys = PLANNER_SYSTEM_PROMPT
        if retry:
            sys += "\n上一次输出无法解析或含非法 action，请只输出合法 JSON 数组，不要任何解释。"
        resp = _client().chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": text}],
            temperature=0,
        )
        data = _parse_json(resp.choices[0].message.content)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, list):
        return None
    whitelist = set(ACTION_SPECS.keys()) | set(READ_TOOL_NAMES)
    out: list = []
    for item in data:
        if not isinstance(item, dict):
            return None
        if item.get("type") not in ("read", "write"):
            return None
        if item.get("action") not in whitelist:
            return None
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
#  节点
# --------------------------------------------------------------------------- #
def understand(state: MainState) -> dict:
    """意图理解。★返回值先清空旧状态再填新值，防止上一轮残留 task_plan 误入联动。

    两条入口在这里合流：
    - 结构化提交（前端表单 → state["submit"]）：纯代码校验后直接 intent='write'，
      不调 LLM（省一次调用，也避免 LLM 把「¥2000」抽错）；
    - 纯文本对话：走原 LLM 抽取。
    往后的安全流程（risk_check / verify / confirm / execute）两条路完全一致。
    """
    submit = normalize_submit(state.get("submit"))
    if submit is not None:
        return {
            "intent": "write",
            "task": submit,
            "submit": None,  # ★消费即清空：不让本轮的提交泄漏到下一轮纯文本对话
            "task_plan": [],
            "current_task": 0,
            "task_results": [],
            "result": None,
        }
    text = _last_user_text(state.get("messages", []))
    intent, task = _call_understand(text)
    return {
        "intent": intent,
        "task": task,
        "submit": None,
        "task_plan": [],
        "current_task": 0,
        "task_results": [],
        "result": None,
    }


def planner(state: MainState) -> dict:
    """联动拆解。失败重试 1 次。"""
    text = _last_user_text(state.get("messages", []))
    plan = _call_planner(text)
    if plan is None:
        plan = _call_planner(text, retry=True)
    return {"task_plan": plan or []}


def plan_check(state: MainState) -> dict:
    """纯代码：每个 action 必须在白名单内（= ACTION_SPECS 键 ∪ 只读工具名）。"""
    plan = state.get("task_plan") or []
    if not plan:  # planner 两次都失败，没有可执行的任务链
        return {"result": {"error": "无法规划任务链，已终止"}}
    whitelist = set(ACTION_SPECS.keys()) | set(READ_TOOL_NAMES)
    for t in plan:
        action = (t or {}).get("action")
        if action not in whitelist:
            return {"result": {"error": f"包含无法代办的操作（{action}），已终止"}}
    return {}


def _describe_task(task: dict | None) -> str:
    if not isinstance(task, dict):
        return "未知任务"
    action = task.get("action")
    t = task.get("type")
    name = {
        "get_balance": "查询余额", "get_transactions": "查询流水", "list_payees": "查询收款人",
        "list_products": "查询理财产品", "list_subscriptions": "查询订阅列表",
        "discover_subscriptions": "发现疑似订阅扣款",
        "transfer": "转账", "buy_product": "购买理财", "redeem": "赎回持仓",
        "internal_transfer": "资金归集",
        "freeze_card": "冻结银行卡", "unfreeze_card": "解冻银行卡", "cancel_subscription": "取消订阅",
    }.get(action, str(action or "任务"))
    params = task.get("params") or {}
    extra = ""
    if action == "cancel_subscription" and params.get("sub_id") is not None:
        extra = f"（订阅#{params['sub_id']}）"
    elif action == "buy_product" and params.get("product_id") is not None:
        extra = f"（产品#{params['product_id']}）"
    elif action == "transfer" and params.get("payee"):
        extra = f"（{params['payee']}）"
    return f"{name}{extra}"


def task_dispatch(state: MainState) -> dict:
    """取当前任务（联动：task_plan[current_task]），写入 [系统] 消息供子图 LLM 看见。"""
    plan = state.get("task_plan") or []
    idx = state.get("current_task", 0)
    task = plan[idx]
    sys_msg = f"[系统] 当前任务：{_describe_task(task)}"
    return {"task": task, "messages": [{"role": "system", "content": sys_msg}]}


def collect(state: MainState) -> dict:
    """子图 result 入 task_results，current_task+1，生成进度消息。"""
    plan = state.get("task_plan") or []
    idx = state.get("current_task", 0)
    results = list(state.get("task_results") or [])
    result = state.get("result")
    results.append(result)
    # ★ 写子任务被「未绑定手机号」拦截 → 中止后续写任务，立即汇报（已知坑）
    if isinstance(result, dict) and result.get("blocked"):
        progress = (f"任务 {idx + 1}/{len(plan)}：{_describe_task(plan[idx])} "
                    f"已拦截 —— {result.get('message') or '需先绑定手机号'}")
        return {
            "task_results": results,
            "current_task": len(plan),  # 终止循环，进入 respond
            "result": result,
            "messages": [{"role": "system", "content": progress}],
        }
    progress = f"已完成 {idx + 1}/{len(plan)}：{_describe_task(plan[idx])}"
    return {
        "task_results": results,
        "current_task": idx + 1,
        "result": None,  # 清掉，避免冒泡到 respond
        "messages": [{"role": "system", "content": progress}],
    }


def _restate_result(res: Any) -> str:
    if not isinstance(res, dict):
        return str(res)
    if res.get("blocked"):
        # 写操作被「未绑定手机号」拦截：直接回可读提示（不用符号，保持 0 emoji 观感）
        return res.get("message") or "请先绑定手机号后再办理资金业务"
    if res.get("ok"):
        data = res.get("data") or {}
        return str(data.get("message") or res.get("message") or "操作成功")
    if res.get("message") and not res.get("error"):
        # 优雅退出（用户取消/放弃澄清）：如实回显退出话术，不渲染成"操作失败"
        return res["message"]
    return str(res.get("error") or "操作失败")


def _linkage_summary(plan: list, results: list) -> str:
    lines = ["本次联动已完成，汇总如下："]
    for t, r in zip(plan, results):
        label = _describe_task(t)
        if isinstance(r, dict) and r.get("ok"):
            data = r.get("data") or {}
            detail = data.get("message") or data.get("serial_no") or "成功"
            lines.append(f"  已完成 {label} → {detail}")
        else:
            if isinstance(r, dict) and r.get("blocked"):
                err = r.get("message") or "需先绑定手机号后才能办理"
            else:
                err = (r or {}).get("error") if isinstance(r, dict) else str(r)
            lines.append(f"  未完成 {label} → {err or '未完成'}")
    return "\n".join(lines)


def respond(state: MainState) -> dict:
    """汇总。联动模式用模板（数字只来自 task_results）；单任务转述 result；read/chat 不改。"""
    plan = state.get("task_plan") or []
    if plan:
        return {"messages": [{"role": "assistant", "content": _linkage_summary(plan, state.get("task_results") or [])}]}
    res = state.get("result")
    if res:
        return {"messages": [{"role": "assistant", "content": _restate_result(res)}]}
    return {}


# --------------------------------------------------------------------------- #
#  条件边（全部代码判断，禁止 LLM 参与路由）
# --------------------------------------------------------------------------- #
def route_after_understand(state: MainState) -> str:
    intent = state.get("intent")
    if intent == "linkage":
        return "planner"
    if intent == "write":
        return "write_flow"
    return "read_agent"


def route_after_plan_check(state: MainState) -> str:
    return "respond" if state.get("result") else "task_dispatch"


def route_after_task_dispatch(state: MainState) -> str:
    task = state.get("task") or {}
    return "read_agent" if task.get("type") == "read" else "write_flow"


def route_after_subgraph(state: MainState) -> str:
    # 子图（read_agent / write_flow）出口后：联动（task_plan 非空）→ collect；否则 → respond
    return "collect" if state.get("task_plan") else "respond"


def route_after_collect(state: MainState) -> str:
    plan = state.get("task_plan") or []
    return "task_dispatch" if state.get("current_task", 0) < len(plan) else "respond"


# --------------------------------------------------------------------------- #
#  构图
# --------------------------------------------------------------------------- #
def build_main_graph(checkpointer=None, llm_client=None):
    """编译并返回主图（自由规划 + 确定执行）。

    - 两个子图以裸编译图直接作为节点（不自带 checkpointer）。
    - 主图统一持有 checkpointer（默认 SqliteSaver("bank.db")），保证中断可恢复、
      thread 状态持久、跨子图一致。
    - llm_client 可注入（离线自测用 FakeClient）。
    """
    global _LLM_CLIENT
    _LLM_CLIENT = llm_client

    read_graph = build_read_agent(llm_client=llm_client)   # 裸
    write_graph = build_write_flow(llm_client=llm_client)  # 裸

    g = StateGraph(MainState)
    g.add_node("understand", understand)
    g.add_node("planner", planner)
    g.add_node("plan_check", plan_check)
    g.add_node("task_dispatch", task_dispatch)
    g.add_node("read_agent", read_graph)
    g.add_node("write_flow", write_graph)
    g.add_node("collect", collect)
    g.add_node("respond", respond)

    g.add_edge(START, "understand")
    g.add_conditional_edges(
        "understand", route_after_understand,
        {"planner": "planner", "write_flow": "write_flow", "read_agent": "read_agent"},
    )
    g.add_edge("planner", "plan_check")
    g.add_conditional_edges("plan_check", route_after_plan_check, {"task_dispatch": "task_dispatch", "respond": "respond"})
    g.add_conditional_edges("task_dispatch", route_after_task_dispatch, {"read_agent": "read_agent", "write_flow": "write_flow"})
    g.add_conditional_edges("read_agent", route_after_subgraph, {"collect": "collect", "respond": "respond"})
    g.add_conditional_edges("write_flow", route_after_subgraph, {"collect": "collect", "respond": "respond"})
    g.add_conditional_edges("collect", route_after_collect, {"task_dispatch": "task_dispatch", "respond": "respond"})
    g.add_edge("respond", END)

    if checkpointer is None:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        checkpointer = SqliteSaver(sqlite3.connect("bank.db", check_same_thread=False))

    return g.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    g = build_main_graph()
    print("main_graph 编译成功，节点：", list(g.get_graph().nodes))
