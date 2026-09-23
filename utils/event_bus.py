"""
utils/event_bus.py —— 进程内轻量事件总线（线程安全）

用途：工具执行层（同步、跑在 LangGraph invoke 的 worker 线程里）想向前端
      SSE 管道推送"正在调用 MCP 外部数据 / 慢调用状态"等事件时，唯一的出口。

设计：
- 发布 = emit(dict)，对当前所有订阅者逐个回调；没有订阅者时近乎零开销（读锁一次）。
- 订阅 = subscribe(cb) -> token；取消 = unsubscribe(token)。
- 不 import 任何业务模块，避免循环依赖；emit 里捕获异常，不让事件回调拖垮主链路。

配合 read_agent 的改造：MCP 工具经 utils/mcp_bridge.py 调用时在前后各 emit 一次
  {"type": "tool", ...} / {"type": "tool_result", ...}；超过慢阈值时 emit
  {"type": "status", "text": "正在查询外部实时数据，约需数秒..."}。
run_server 的 POST /api/chat/stream 在每轮请求内订阅并把事件转发成 SSE 帧。
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import uuid
from typing import Any, Callable, Iterator

_lock = threading.Lock()
_listeners: dict[str, Callable[[dict], None]] = {}

# 当前执行流的作用域标记（一般用 thread_id）。emit 时自动打到事件上，
# 订阅方（SSE 端点）据此过滤：只转发属于自己那一轮的事件。
# 没有它的话，两个用户同时聊天，A 的节点进度会串到 B 的页面上。
_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar("event_scope", default=None)


@contextlib.contextmanager
def scope_ctx(name: str | None) -> Iterator[None]:
    """在此作用域内 emit 的事件都会带上 scope=name。"""
    token = _scope.set(name)
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> str | None:
    return _scope.get()


def subscribe(callback: Callable[[dict], None]) -> str:
    """注册一个事件回调，返回取消令牌。"""
    token = uuid.uuid4().hex
    with _lock:
        _listeners[token] = callback
    return token


def unsubscribe(token: str) -> None:
    with _lock:
        _listeners.pop(token, None)


def emit(event: dict) -> None:
    """向当前全部订阅者广播一个事件 dict。

    快路径：无订阅者时直接返回（工具层每次调用都会走这里，必须便宜）。
    """
    if not _listeners:
        return
    scope = _scope.get()
    if scope and isinstance(event, dict) and "scope" not in event:
        event = {**event, "scope": scope}
    with _lock:
        targets = list(_listeners.values())
    for cb in targets:
        try:
            cb(event)
        except Exception:  # noqa: BLE001  事件回调失败绝不能影响工具执行
            pass


def listener_count() -> int:
    with _lock:
        return len(_listeners)
