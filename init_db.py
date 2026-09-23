"""
init_db.py —— 用 pymysql 执行 sql/init.sql（Windows 下不必手动敲 mysql 命令）

用法：
    python init_db.py
    python init_db.py --sql sql/init.sql

说明：
- 连接时不选库（init.sql 里自带 CREATE DATABASE / USE）
- 逐条语句执行，遇到 SELECT 会打印结果（init.sql 末尾有三段自检）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from mock_bank import db  # noqa: E402
import profile_db  # noqa: E402


# 升级文件不能只按文件名字母序跑：例如 card_account 依赖 insite_transfer 的
# linked_user_id，delete_cascade 又依赖 chat/profile/inbox 建出的表与列。
# 顺序只在 Fresh DB 上有强约束；存量库重复跑仍由各脚本自身兜底。
UPGRADE_ORDER = (
    "upgrade_account_system.sql",
    "upgrade_admin_ops.sql",
    "upgrade_isolation.sql",
    "upgrade_profile.sql",
    "upgrade_chat.sql",
    "upgrade_phone_bind.sql",
    "upgrade_inbox_admin.sql",
    "upgrade_insite_transfer.sql",
    "upgrade_card_account.sql",
    "upgrade_realname_verified.sql",
    "upgrade_crossbank.sql",
    "upgrade_subscription_v2.sql",
    "upgrade_products_fix.sql",
    "upgrade_knowledge_rag.sql",
    "upgrade_transfer_limit.sql",
    "upgrade_idempotency.sql",
    "upgrade_delete_cascade.sql",
)


def split_statements(sql_text: str) -> list[str]:
    """按语句结束符切分 SQL，正确处理字符串字面量、反引号标识符、注释与 DELIMITER。

    ★ DELIMITER 支持（存储过程必需）：
      MySQL 客户端指令 `DELIMITER //` 不是 SQL，直接喂给 pymysql 会 1064；
      而存储过程体内的 ';' 又必须靠它临时改结束符。所以这里把结束符做成状态：
      遇到 DELIMITER 指令行就切换，体内语句直到遇见新结束符才算一条。
      对不含 DELIMITER 的脚本，行为与"固定按 ';' 切分"完全一致。
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql_text)
    in_single = in_double = in_back = False
    delim = ";"

    def flush() -> None:
        stmt = "".join(buf).strip()
        if stmt:
            statements.append(stmt)
        buf.clear()

    while i < n:
        ch = sql_text[i]
        nxt = sql_text[i + 1] if i + 1 < n else ""

        if in_single or in_double or in_back:
            buf.append(ch)
            quote = "'" if in_single else ('"' if in_double else "`")
            if ch == "\\" and (in_single or in_double):  # 转义字符
                if i + 1 < n:
                    buf.append(sql_text[i + 1])
                    i += 2
                    continue
            elif ch == quote:
                in_single = in_double = in_back = False
            i += 1
            continue

        # 行注释
        if ch == "-" and nxt == "-":
            while i < n and sql_text[i] != "\n":
                i += 1
            continue
        if ch == "#":
            while i < n and sql_text[i] != "\n":
                i += 1
            continue
        # 块注释
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (sql_text[i] == "*" and sql_text[i + 1] == "/"):
                i += 1
            i += 2
            continue

        # DELIMITER 指令（只在"新语句开头"处识别，避免误伤同名标识符）
        if not "".join(buf).strip() and sql_text[i : i + 9].upper() == "DELIMITER":
            j = sql_text.find("\n", i)
            j = n if j == -1 else j
            delim = sql_text[i + 9 : j].strip() or ";"
            i = j
            continue

        # 语句结束符（默认 ';'，DELIMITER 切换后为自定义串）
        if sql_text[i : i + len(delim)] == delim:
            flush()
            i += len(delim)
            continue

        if ch == "'":
            in_single = True
            buf.append(ch)
        elif ch == '"':
            in_double = True
            buf.append(ch)
        elif ch == "`":
            in_back = True
            buf.append(ch)
        else:
            buf.append(ch)
        i += 1

    flush()
    return statements


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 Mock 银行数据库")
    parser.add_argument("--sql", default=str(Path(__file__).resolve().parent / "sql" / "init.sql"))
    args = parser.parse_args()

    sql_path = Path(args.sql)
    if not sql_path.exists():
        print(f"找不到 SQL 文件：{sql_path}")
        sys.exit(1)

    sql_text = sql_path.read_text(encoding="utf-8")
    statements = split_statements(sql_text)
    print(f"目标：{config.DB_USER}@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")
    print(f"共解析到 {len(statements)} 条语句，开始执行...\n")

    executed = 0
    with db.get_conn(use_db=False) as conn:  # 不选库：init.sql 自带 CREATE DATABASE / USE
        with conn.cursor() as cur:
            for idx, stmt in enumerate(statements, 1):
                head = " ".join(stmt.split())[:70]
                try:
                    cur.execute(stmt)
                    executed += 1
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    print(f"\n[失败] 第 {idx} 条语句：\n{stmt[:400]}\n原因：{exc}")
                    sys.exit(2)

                if stmt.lstrip().upper().startswith("SELECT"):
                    rows = cur.fetchall()
                    print(f"-- {head}")
                    if not rows:
                        print("   (无结果)")
                    for r in rows:
                        print("   ", {k: (str(v) if not isinstance(v, (int, float)) else v) for k, v in r.items()})
            conn.commit()

    # ---- 登记新增迁移（幂等 SQL）：建库后按显式依赖顺序执行 sql/upgrade_*.sql ----
    # ★ 每个迁移文件复用同一个连接执行完整语句序列。
    #   MySQL 用户变量（@ddl）是连接级的；若每条语句各开连接，
    #   `SET @ddl=...; PREPARE ... FROM @ddl; EXECUTE ...` 会因 @ddl 不可见而静默失效。
    #   单文件单连接既保留 PREPARE 语义，又让每条语句的成功立即提交。
    _upg_dir = Path(__file__).resolve().parent / "sql"
    if _upg_dir.exists():
        # 认证列原先只由 run_server 启动时补；但 inbox / insite 迁移本身会读写
        # users.username，所以必须在迁移前先建好。
        try:
            profile_db.ensure_auth_schema()
        except Exception as _e:  # noqa: BLE001
            print(f"\n[迁移失败] 认证字段初始化失败：{_e}")
            sys.exit(2)

        _up_files = {p.name: p for p in _upg_dir.glob("upgrade_*.sql")}
        _unknown = sorted(set(_up_files) - set(UPGRADE_ORDER))
        _ordered_names = [name for name in UPGRADE_ORDER if name in _up_files]
        # 未登记的迁移放在 delete_cascade 之前，避免漏跑成一个静默空档。
        if _unknown:
            print(f"  [迁移警告] 未登记顺序的升级文件：{', '.join(_unknown)}")
            _ordered_names = (
                [name for name in _ordered_names if name != "upgrade_delete_cascade.sql"]
                + _unknown
                + (["upgrade_delete_cascade.sql"] if "upgrade_delete_cascade.sql" in _up_files else [])
            )

        for _name in _ordered_names:
            _up = _up_files[_name]
            try:
                _up_text = _up.read_text(encoding="utf-8")
                _up_statements = split_statements(_up_text)
                # delete_cascade 要给 chat_messages.user_id 建外键，而该列原由
                # profile_db.ensure_schema() 在应用启动时补；建库流程需提前补好。
                if _name == "upgrade_delete_cascade.sql":
                    profile_db.ensure_schema()
                with db.get_conn() as _c:
                    with _c.cursor() as _cu:
                        for _st in _up_statements:
                            try:
                                _cu.execute(_st)
                                _c.commit()
                            except Exception as _e:  # noqa: BLE001  幂等脚本的单条失败忽略
                                _c.rollback()
                                print(f"  [迁移跳过] {_up.name}: {_e}")
            except Exception as _e:  # noqa: BLE001
                print(f"  [迁移文件读取失败] {_up.name}: {_e}")

    print(f"\n完成：成功执行 {executed} 条语句。数据库 {config.DB_NAME} 已就绪。")


if __name__ == "__main__":
    main()
