"""
agent/driver.py —— 统一驱动（全局唯一写法，对嵌套子图中断完全透明）

    def chat(user_input) -> str:
        state = graph.get_state(config)
        if state.next:  # 挂起在 interrupt（含嵌套子图）→ 恢复
            result = graph.invoke(Command(resume=user_input), config)
        else:           # 新的一轮
            result = graph.invoke({"messages": [("user", user_input)]}, config)
        if "__interrupt__" in result:
            return result["__interrupt__"][0].value   # 可能是字符串，也可能是 {"question": ...}
        return result["messages"][-1].content

要点：
- 不出现任何「感知子图层级」的逻辑。子图内部的 clarify/verify/confirm 中断，
  对这段驱动而言与直接节点的中断没有区别——都只是 state.next 非空 + __interrupt__。
- Command(resume=...) 会沿主图一路穿透到挂起的子图节点，恢复其 interrupt。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import config
from .main_graph import build_main_graph
# ★ 复用 write_flow 里唯一的「确认/取消」判定（关键词硬匹配，禁 LLM），
#   保证驱动层的逃生舱与子图内的判定口径完全一致 —— 绝不把「确认」误当新问题放开。
from .write_flow import is_cancel, is_confirm


class MainDriver:
    """封装主图 + thread 状态 + 透明驱动逻辑。"""

    def __init__(self, graph=None, thread_id: str = "u1", checkpointer_path: str | None = None,
                 recursion_limit: int = 50, llm_client=None):
        # checkpointer 路径默认落在项目根目录（config.BASE_DIR），不随 cwd 漂移。
        # 早期版本用相对路径 "bank.db"，从其他目录启动会凭空生成空库，历史"消失"。
        if checkpointer_path is None:
            checkpointer_path = str(config.BASE_DIR / "bank.db")
        self.thread_id = thread_id
        self.recursion_limit = recursion_limit
        if graph is None:
            cp = SqliteSaver(sqlite3.connect(checkpointer_path, check_same_thread=False))
            self.graph = build_main_graph(checkpointer=cp, llm_client=llm_client)
        else:
            self.graph = graph
        self.cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": recursion_limit}

    def chat(self, user_input: str) -> str:
        state = self.graph.get_state(self.cfg)
        if state.next:
            # ★ 逃生舱（用户反馈：澄清 / 确认挂起后，"查余额""查订阅"等任何消息都被当成
            #   对挂起的回答而吞掉，用户被锁死在循环里 —— 问什么都被回一句确认请求）：
            #   只要新消息明显是**另一个请求**（问句式）而不是对挂起的回答，
            #   就先用 resume("取消") 优雅退出旧流程，再按新请求正常处理。
            #
            #   ★ 为什么三种中断（clarify / verify / confirm）都允许放弃：
            #     它们全都发生在 execute **之前**，此刻尚未产生任何资金或数据变更，
            #     放弃 = 回到"这笔操作从未发起"的状态，无副作用、可重新发起。
            #     真正的不可逆动作在 execute 及其后的落库，那已经不在中断挂起态里了。
            #   ★ 但"用户真的是在回答"时绝不放开：见下面 is_confirm / is_cancel 与
            #     问题句式启发式的三重判定，避免把「确认」「取消」误当新问题。
            kind = self._suspended_kind(state)
            if (kind and not is_confirm(user_input) and not is_cancel(user_input)
                    and self._looks_like_new_request(user_input)):
                self.graph.invoke(Command(resume="取消"), self.cfg)
                note = ""
                if kind == "confirm":
                    note = "（已放弃上一笔待确认的操作，未发生任何变更。如需继续办理，请重新发起。）\n\n"
                elif kind == "verify":
                    note = "（已放弃上一笔待验证的操作，未发生任何变更。如需继续办理，请重新发起。）\n\n"
                return note + self.chat(user_input)
            result = self.graph.invoke(Command(resume=user_input), self.cfg)
        else:  # 新的一轮
            result = self.graph.invoke({"messages": [("user", user_input)]}, self.cfg)
        return self._reply_from(result)

    @staticmethod
    def _suspended_kind(state) -> str:
        """挂起中的 interrupt 属于哪一类：confirm / verify / clarify（无挂起 → ""）。

        判定依据是中断问题文本（措辞由 write_flow 生成：_verify_question / _gen_confirm_card）。
        三类都在 execute 之前，放弃均无副作用；分类只用于给用户的提示措辞。
        """
        try:
            for t in (state.tasks or ()):
                for intr in (t.interrupts or ()):
                    v = intr.value
                    q = v if isinstance(v, str) else ((v or {}).get("question", "") if isinstance(v, dict) else str(v))
                    q = q or ""
                    if "回复「确认」" in q or "请确认" in q:
                        return "confirm"
                    if "验证码" in q or "支付密码" in q:
                        return "verify"
                    return "clarify"
        except Exception:  # noqa: BLE001
            pass
        return ""

    @staticmethod
    def _looks_like_new_request(text: str) -> bool:
        """新消息是否像一个新请求（而非对澄清的回答）。保守启发式：
        问题句式（？/吗/查/看/什么/怎么/多少/你知道/介绍）才算；序号、名称、
        取消词都不算 —— 交给 write_flow 正常处理。"""
        import re as _re
        t = (text or "").strip()
        if not t:
            return False
        if _re.search(r"(取消|算了|不用了|退出|停止)", t):
            return False
        if _re.fullmatch(r"[0-9一二三四五六七八九十]+", t):
            return False
        return bool(_re.search(r"[？?]|吗\s*$|怎么|什么|多少|为什么|^(查|看|帮我查|你知道|介绍|说明|有哪些|有吗)", t))

    # ------------------------------------------------------------------ #
    #  结构化任务提交（前端表单）——与 chat 汇入同一条安全流程
    # ------------------------------------------------------------------ #
    def submit(self, task: dict, text: str) -> str:
        """把表单收集到的 task 直接灌进主图（understand 会跳过 LLM 抽意图）。

        ★ 只负责「提交参数」：后续 risk_check → verify → confirm → execute
          与对话路径完全一致，绝不绕过任何一道中断。

        text：写进对话历史的用户消息（如「[转账表单] 向妈妈转账 ¥2000.00」），
              让子图/回读历史都能看到这笔操作从何而来。
        """
        if self.graph.get_state(self.cfg).next:
            # 该 thread 还挂在上一个中断上，混入新输入会让 LangGraph 同时重放
            # 挂起节点与新一轮，状态错乱。调用方应换一个干净 thread（见 run_server）。
            raise RuntimeError("当前会话线程仍有未完成的中断，无法开始新的表单提交")
        result = self.graph.invoke({"messages": [("user", text)], "submit": task}, self.cfg)
        return self._reply_from(result)

    def resume(self, value: str) -> str:
        """显式恢复挂起的中断（验证码 / 确认）。未挂起则报错，避免把恢复语当新指令。"""
        if not self.graph.get_state(self.cfg).next:
            raise RuntimeError("当前会话没有待恢复的中断")
        return self._reply_from(self.graph.invoke(Command(resume=value), self.cfg))

    def _reply_from(self, result: dict | None) -> str:
        """统一出口：有 interrupt 取问题文本，否则取最后一条 assistant 消息。"""
        intr = (result or {}).get("__interrupt__")
        if intr:
            val = intr[0].value
            # 子图 interrupt 的值可能是字符串，也可能是 {"question": ...}，统一兜底
            if isinstance(val, dict):
                return val.get("question") or val.get("value") or str(val)
            return val

        for m in reversed((result or {}).get("messages", [])):
            content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
            role = m.get("role") if isinstance(m, dict) else getattr(m, "type", None)
            if role in ("ai", "assistant") and content:
                return content
        return str((result or {}).get("result") or "")

    def suspended(self) -> bool:
        """当前 thread 是否仍挂起在 interrupt（用于「重启后可继续」判定）。"""
        return bool(self.graph.get_state(self.cfg).next)


def chat(user_input: str, *, graph, thread_id: str = "u1", cfg: dict | None = None) -> str:
    """模块级便捷函数：直接用一段图 + thread_id 驱动一轮（与上面 chat 等价）。"""
    driver = MainDriver(graph=graph, thread_id=thread_id)
    _ = cfg  # cfg 由 driver 内部按 thread_id 构造，保持透明
    return driver.chat(user_input)
