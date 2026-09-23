"""
run_cli.py —— 命令行统一入口（M3 全链路：自由规划 + 确定执行）

用法：
    python run_cli.py
    python run_cli.py --thread my-session

内置命令：
    /new        开启新会话（新 thread_id，清空上下文）
    /help       帮助
    exit / quit / q / 退出   退出

说明：
    所有多轮 / 中断恢复逻辑都收敛在 agent/driver.py 的 MainDriver.chat 里，
    本文件只负责「读输入 → 调 chat → 打印」，不感知任何子图层级。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from agent.driver import MainDriver  # noqa: E402
import profile_db  # noqa: E402
from utils import security  # noqa: E402
from agent.main_graph import build_main_graph  # noqa: E402

BANNER = f"""
{'=' * 70}
 AI 银行智能体 · M3 主图（自由规划 + 确定执行）
 模型: {config.DEEPSEEK_MODEL} @ {config.DEEPSEEK_BASE_URL}
 银行服务: {config.BANK_API_BASE} | 会话文件: bank.db
 输入 /help 查看命令，exit 退出
{'=' * 70}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 银行智能体 CLI（M3 主图）")
    parser.add_argument("--thread", default="test-user", help="会话 thread_id")
    args = parser.parse_args()

    print(BANNER)
    # ★ BANK_ALLOW_ANON=0 后，CLI 同样要持有效登录态（演示主账号）：
    #   token 供工具调用打 mock_bank 时通过鉴权；ctx 供 Agent 侧身份解析。
    CLI_TOKEN = profile_db.issue_token(config.DEFAULT_USER_ID)

    graph = build_main_graph()
    thread_id = args.thread
    driver = MainDriver(graph=graph, thread_id=thread_id)

    while True:
        try:
            question = input(f"\n\033[96m你[{thread_id}]\033[0m > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见。")
            break

        if not question:
            continue
        if question.lower() in ("exit", "quit", "q", "退出", "/exit", "/quit"):
            print("再见。")
            break
        if question in ("/help", "help", "?"):
            print("  /new   新会话   /help 帮助   exit 退出")
            print("  示例：这个月钱花哪了 / 给妈妈转2000 / 把不用的订阅取消，省下的钱买理财")
            continue
        if question == "/new":
            thread_id = f"test-user-{int(time.time())}"
            driver = MainDriver(graph=graph, thread_id=thread_id)
            print(f"  已开启新会话：{thread_id}")
            continue

        try:
            with security.current_user_ctx(config.DEFAULT_USER_ID),                     security.set_current_auth_token(CLI_TOKEN):
                answer = driver.chat(question)
        except Exception as exc:  # noqa: BLE001
            print(f"\033[91m运行出错：{exc}\033[0m")
            continue

        print(f"\n\033[92m助手\033[0m > {answer or '（无回复）'}")


if __name__ == "__main__":
    main()
