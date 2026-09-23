"""
run_cli_write.py —— 写操作子图命令行驱动（M2 独立测试）

外层驱动模式（全局唯一写法）：
    state = graph.get_state(config)
    if state.next:  # 挂起在 interrupt
        out = graph.invoke(Command(resume=user_input), config)
    else:
        out = graph.invoke({"messages":[("user", user_input)], "task": {...}, ...}, config)
    out.__interrupt__ 非空 → 取 question 展示；否则取 result。

用法：
    python run_cli_write.py
    python run_cli_write.py --thread w1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

CLI_TOKEN = profile_db.issue_token(config.DEFAULT_USER_ID)  # BANK_ALLOW_ANON=0 后 CLI 也须持登录态
import sqlite3  # noqa: E402
from agent.write_flow import build_write_flow, detect_action  # noqa: E402
import profile_db  # noqa: E402
from utils import security  # noqa: E402
from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

BANNER = f"""
{'=' * 70}
 AI 银行智能体 · M2 写操作工作流（确定性 + 人机中断）
 银行服务: {config.BANK_API_BASE} | 会话文件: {config.WRITE_TEST_DB}
 输入 /help 查看命令，exit 退出
{'=' * 70}
"""


def _initial_state(user_input: str, action: str) -> dict:
    return {
        "messages": [("user", user_input)],
        "task": {"type": "write", "action": action, "params": {}},
        "params": {},
        "asked_times": 0,
        "verified": False,
        "confirmed": False,
        "confirm_retries": 0,
        "verify_attempts": 0,
        "result": {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="写操作工作流 CLI")
    parser.add_argument("--thread", default="write-user", help="会话 thread_id")
    args = parser.parse_args()

    print(BANNER)
    cp = SqliteSaver(sqlite3.connect(config.WRITE_TEST_DB, check_same_thread=False))
    graph = build_write_flow(checkpointer=cp)
    thread_id = args.thread
    cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 30}

    while True:
        try:
            question = input(f"\n\033[96m你[{thread_id}]\033[0m > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见。")
            break
        if not question:
            continue
        if question.lower() in ("exit", "quit", "q", "退出"):
            print("再见。")
            break
        if question in ("/help", "help", "?"):
            print("  示例：给妈妈转2000 / 转8000给李雷 / 买5万稳健增利一年 / 冻结卡1")
            print("  在验证码/确认环节直接输入验证码或「确认」/「取消」；exit 退出")
            continue

        state = graph.get_state(cfg)
        try:
            if state.next:  # 挂起在 interrupt → 恢复
                out = None
                with security.current_user_ctx(config.DEFAULT_USER_ID),                         security.set_current_auth_token(CLI_TOKEN):
                    out = graph.invoke(Command(resume=question), cfg)
            else:
                action = detect_action(question)
                out = None
                with security.current_user_ctx(config.DEFAULT_USER_ID),                         security.set_current_auth_token(CLI_TOKEN):
                    out = graph.invoke(_initial_state(question, action), cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"\033[91m运行出错：{exc}\033[0m")
            continue

        # graph.invoke 返回 dict，interrupt 信息挂在 "__interrupt__" 键上（不是属性）
        intr = (out or {}).get("__interrupt__") or []
        if intr:
            for item in intr:
                print(f"\n\033[93m⏸ {item.value}\033[0m")
        else:
            res = (out or {}).get("result") or {}
            print(f"\n\033[92m结果\033[0m > {res}")


if __name__ == "__main__":
    main()
