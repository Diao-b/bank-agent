"""
agent/state.py —— LangGraph 状态定义

M1：只定义自由 Agent（只读子图）的状态。
M2/M3 扩展写操作工作流与主图路由时，在此继续追加，保持单一来源。
"""
from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class ReadState(TypedDict):
    """自由 Agent（只读子图）状态。

    messages 用 add_messages reducer：
    - 自动按 id 去重 / 覆盖，新增消息追加
    - 配合 checkpointer 即可天然支持多轮对话（M1 用 SqliteSaver 独立测试）
    """

    messages: Annotated[list, add_messages]


# --------------------------------------------------------------------------- #
#  M2：写操作子图（确定性工作流 + 人机中断）
# --------------------------------------------------------------------------- #
class WriteIn(TypedDict):
    """写操作子图输入。

    messages：对话历史（用户每条输入作为 user 消息追加）
    task：    {type:"write", action, params} —— action 由外层（M3 主图路由 / M2 CLI）给出
    """

    messages: Annotated[list, add_messages]
    task: dict


class WriteState(WriteIn):
    """写操作子图完整状态（在 WriteIn 基础上收窄内部字段）。

    params / asked_times / verified / confirmed / result 为工作流内部状态；
    其中 confirm_retries / verify_attempts 是循环计数器，必须随 checkpoint 持久化，
    以便「重启程序后中断处可继续」（验收 #6）。
    """

    params: dict
    asked_times: int
    verified: bool
    confirmed: bool
    result: dict
    # 循环计数（内部，不外泄）
    confirm_retries: int
    verify_attempts: int
