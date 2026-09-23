"""
scripts/provision_user.py —— 给存量用户补「数据沙箱」初始化的命令行工具

背景：
    多用户隔离改造后，新注册用户会在 /api/auth/register 时自动初始化一套
    独立演示数据（账户 / 卡片 / 收款人 / 流水 / 订阅，见 utils/user_provision.py）。
    但改造前就存在的用户（含默认演示账号 DEFAULT_USER_ID=1）当时只 INSERT 了
    users 行，accounts / cards 等业务表全空 —— 登录后余额为空、转账报"账户不存在"。

    本工具用于给这类老用户补初始化，幂等：已有账户记录则跳过，不会重复建数据。

用法：
    # 给单个用户补初始化（用户名或真实姓名均可）
    python scripts/provision_user.py alice
    python scripts/provision_user.py alice --months 3

    # 列出所有用户 + 是否已初始化
    python scripts/provision_user.py --list

    # 给所有未初始化的用户一键补齐
    python scripts/provision_user.py --all

退出码：0 = 成功/已跳过；1 = 用户不存在或失败。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许从任意 cwd 直接跑：python scripts/provision_user.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from utils.user_provision import (  # noqa: E402
    is_provisioned,
    provision_new_user,
    provision_user_by_username,
)


def _list_users() -> list[dict]:
    import pymysql

    with pymysql.connect(**config.db_config()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT u.id, u.username, COALESCE(u.name, u.username) AS name, "
                "COALESCE(u.real_name, '') AS real_name "
                "FROM users u ORDER BY u.id"
            )
            rows = cur.fetchall()
    for row in rows:
        try:
            row["provisioned"] = is_provisioned(row["id"])
        except Exception:  # noqa: BLE001  DB 不可用时标注 unknown，不中断列表
            row["provisioned"] = None
    return rows


def cmd_list() -> int:
    print(f"{'ID':>4}  {'用户名':<16} {'姓名':<10} {'沙箱':<8} 说明")
    print("-" * 60)
    for r in _list_users():
        st = {True: "✅ 已初始化", False: "⬜ 未初始化", None: "❓ 未知"}[r["provisioned"]]
        note = "" if r["provisioned"] else "（可用本工具补齐）"
        print(f"{r['id']:>4}  {str(r['username'] or ''):<16} {str(r['real_name'] or r['name'] or ''):<10} {st:<8} {note}")
    return 0


def cmd_provision(username: str, months: int) -> int:
    result = provision_user_by_username(username, months=months)
    if not result.get("ok"):
        print(f"[失败] {result.get('error', '未知错误')}")
        return 1
    if result.get("skipped"):
        print(
            f"[跳过] 用户「{result.get('username')}」(id={result.get('user_id')}) "
            f"已有账户记录，无需重复初始化（幂等闸门）"
        )
        return 0
    print(
        f"[完成] 用户「{result.get('username')}」(id={result.get('user_id')}) "
        f"数据沙箱初始化成功："
        f"主账户 id={result.get('account_id')}、卡 id={result.get('card_id')}、"
        f"流水 {result.get('transactions')} 笔、订阅 {len(result.get('subscription_ids') or [])} 项"
    )
    return 0


def cmd_all(months: int) -> int:
    rows = _list_users()
    todo = [r for r in rows if r["provisioned"] is False]
    if not todo:
        print("[完成] 所有用户都已初始化，无需处理。")
        return 0
    print(f"待补齐 {len(todo)} 个用户：{', '.join(str(r['username'] or r['id']) for r in todo)}")
    code = 0
    for r in todo:
        c = cmd_provision(str(r["username"] or r["id"]), months)
        code = code or c
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description="给存量用户补数据沙箱初始化（幂等）")
    ap.add_argument("username", nargs="?", help="用户名（或真实姓名）；--all 时忽略")
    ap.add_argument("--list", action="store_true", help="列出所有用户及其初始化状态")
    ap.add_argument("--all", action="store_true", help="给所有未初始化用户补齐")
    ap.add_argument("--months", type=int, default=1, help="生成近几个月流水（默认 1）")
    args = ap.parse_args()

    try:
        if args.list:
            return cmd_list()
        if args.all:
            return cmd_all(args.months)
        if not args.username:
            ap.print_help()
            return 1
        return cmd_provision(args.username, args.months)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
