"""
init_db.py —— 用 pymysql 执行 sql/init.sql 并【版本化】应用 sql/upgrade_*.sql

用法：
    python init_db.py                  # 全量：init.sql（★ 含 DROP TABLE，仅限全新库）+ 迁移
    python init_db.py --upgrade-only   # 只应用「未应用」的迁移（存量库安全，不碰 init.sql）
    python init_db.py --status         # 只打印迁移台账与待应用清单后退出（只读）
    python init_db.py --sql sql/init.sql

说明：
- 连接时不选库（init.sql 里自带 CREATE DATABASE / USE）
- 逐条语句执行，遇到 SELECT 会打印结果（init.sql 末尾有三段自检）
- ★ P1.1 迁移版本化：台账 schema_migrations（文件名 + sha256 + 语句/跳过数 + 时间），
  只应用未应用的迁移；文件内容变更（checksum 漂移）会告警并幂等重跑。
- ★ init.sql 含 DROP TABLE，是「全新库初始化」；存量库请用 --upgrade-only。
"""
from __future__ import annotations

import argparse
import hashlib
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
    "upgrade_op_auth.sql",
    "upgrade_runtime_state.sql",
)

# --------------------------------------------------------------------------- #
#  P1.1 迁移版本化：台账 + 只应用未应用
# --------------------------------------------------------------------------- #
_MIG_DIR = Path(__file__).resolve().parent / "sql"
_MIGRATIONS_SQL = _MIG_DIR / "schema_migrations.sql"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ensure_migrations_table(cur) -> None:
    """建迁移台账（幂等）。优先读 sql/schema_migrations.sql，缺失则内联 DDL 兜底。"""
    ddl = (_MIGRATIONS_SQL.read_text(encoding="utf-8") if _MIGRATIONS_SQL.exists() else
           "CREATE TABLE IF NOT EXISTS schema_migrations ("
           " filename VARCHAR(128) NOT NULL, checksum CHAR(64) NOT NULL,"
           " statements INT NOT NULL DEFAULT 0, skipped INT NOT NULL DEFAULT 0,"
           " applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
           " PRIMARY KEY (filename)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    for _st in split_statements(ddl):
        cur.execute(_st)


def _load_applied(cur) -> dict[str, str]:
    """已应用迁移 → 内容 sha256。"""
    cur.execute("SELECT filename, checksum FROM schema_migrations")
    return {r["filename"]: r["checksum"] for r in cur.fetchall()}


def _record_migration(cur, filename: str, checksum: str, statements: int, skipped: int) -> None:
    cur.execute(
        "INSERT INTO schema_migrations (filename, checksum, statements, skipped) "
        "VALUES (%s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE checksum=VALUES(checksum), statements=VALUES(statements), "
        "skipped=VALUES(skipped), applied_at=NOW()",
        (filename, checksum, statements, skipped),
    )


def _db_has_data(cur) -> bool:
    """库内是否已有数据（以 users 行数为准）。表不存在视为「全新库」。"""
    try:
        cur.execute("SELECT COUNT(*) AS n FROM users")
        return int((cur.fetchone() or {}).get("n") or 0) > 0
    except Exception:  # noqa: BLE001
        return False


def _ordered_upgrade_files() -> tuple[list[str], dict[str, Path]]:
    """按 UPGRADE_ORDER 排序的迁移文件名 + 文件表。

    未登记顺序的文件告警，并排在 delete_cascade 之前，避免漏跑成一个静默空档。
    """
    files = {p.name: p for p in _MIG_DIR.glob("upgrade_*.sql")}
    unknown = sorted(set(files) - set(UPGRADE_ORDER))
    ordered = [name for name in UPGRADE_ORDER if name in files]
    if unknown:
        print(f"  [迁移警告] 未登记顺序的升级文件：{', '.join(unknown)}")
        ordered = (
            [n for n in ordered if n != "upgrade_delete_cascade.sql"]
            + unknown
            + (["upgrade_delete_cascade.sql"] if "upgrade_delete_cascade.sql" in files else [])
        )
    return ordered, files


def apply_upgrades(*, fresh: bool = False) -> None:
    """版本化应用 sql/upgrade_*.sql：只跑未应用的迁移，并登记台账。

    ★ 安全闸门（fresh=False 时生效）：库内已有数据但台账为空 → 拒绝执行。
      因为迁移脚本并非全都「数据幂等」（例如 upgrade_products_fix.sql 直接
      `DELETE FROM products` 后重插、upgrade_insite_transfer.sql 无条件
      `UPDATE users SET name=...`），对存量库整体重跑会破坏数据。
      存量库请先 `--baseline`（只登记不执行）纳入版本管理。
      fresh=True 用于「刚跑完 init.sql 的全新库」——此时全量应用是正确语义。

    ★ 每个迁移文件复用同一个连接执行完整语句序列：MySQL 用户变量（@ddl）是连接级的，
      若每条语句各开连接，`SET @ddl=...; PREPARE ... FROM @ddl; EXECUTE ...` 会因 @ddl
      不可见而静默失效。单文件单连接既保留 PREPARE 语义，又让每条语句的成功立即提交。
    """
    if not _MIG_DIR.exists():
        return
    # 认证列原先只由 run_server 启动时补；但 inbox / insite 迁移本身会读写
    # users.username，所以必须在迁移前先建好。
    try:
        profile_db.ensure_auth_schema()
    except Exception as _e:  # noqa: BLE001
        print(f"\n[迁移失败] 认证字段初始化失败：{_e}")
        sys.exit(2)

    ordered, files = _ordered_upgrade_files()
    with db.get_conn() as _c:
        with _c.cursor() as _cu:
            _ensure_migrations_table(_cu)
        _c.commit()
        with _c.cursor() as _cu:
            applied = _load_applied(_cu)
            has_data = _db_has_data(_cu)

    if not applied and has_data and not fresh:
        print("  [迁移拒绝] 库内已有数据但迁移台账为空 —— 直接执行会把全部迁移重跑一遍，")
        print("             其中含 DELETE / 无条件 UPDATE（如 upgrade_products_fix 的")
        print("             DELETE FROM products），并非纯幂等，可能破坏数据。")
        print("             存量库请先登记现有迁移（只登记，不执行 SQL）：")
        print("               python init_db.py --baseline")
        sys.exit(3)

    for _name in ordered:
        _up = files[_name]
        try:
            _text = _up.read_text(encoding="utf-8")
        except Exception as _e:  # noqa: BLE001
            print(f"  [迁移文件读取失败] {_up.name}: {_e}")
            continue
        _sum = _sha256_text(_text)
        _prev = applied.get(_name)
        if _prev == _sum:
            print(f"  [迁移已应用] {_name}")
            continue
        if _prev is not None:
            # ★ 不自动重跑：迁移未必数据幂等，自动重跑可能破坏数据。
            print(f"  [迁移变更·跳过] {_name} 已登记但文件已改（sha {str(_prev)[:8]}→{_sum[:8]}）；"
                  f"如需重跑请先删除台账该行或重跑 --baseline 采纳")
            continue
        _stmts = split_statements(_text)
        # delete_cascade 要给 chat_messages.user_id 建外键，而该列原由
        # profile_db.ensure_schema() 在应用启动时补；建库流程需提前补好。
        if _name == "upgrade_delete_cascade.sql":
            profile_db.ensure_schema()
        _skipped = 0
        with db.get_conn() as _c:
            with _c.cursor() as _cu:
                for _st in _stmts:
                    try:
                        _cu.execute(_st)
                        _c.commit()
                    except Exception as _e:  # noqa: BLE001  幂等脚本的单条失败（对象已存在）忽略
                        _c.rollback()
                        _skipped += 1
                        print(f"  [迁移跳过] {_up.name}: {_e}")
                _record_migration(_cu, _name, _sum, len(_stmts), _skipped)
            _c.commit()
        print(f"  [迁移完成] {_name}（语句 {len(_stmts)}，跳过 {_skipped}）")


def baseline_migrations() -> None:
    """把当前所有迁移登记为「已应用」（**不执行任何 SQL**）—— 将存量库纳入版本管理。"""
    ordered, files = _ordered_upgrade_files()
    with db.get_conn() as c:
        with c.cursor() as cur:
            _ensure_migrations_table(cur)
        c.commit()
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM schema_migrations")
            existing = int((cur.fetchone() or {}).get("n") or 0)
            for _name in ordered:
                _text = files[_name].read_text(encoding="utf-8")
                _record_migration(cur, _name, _sha256_text(_text), len(split_statements(_text)), 0)
        c.commit()
    print(f"已采纳 {len(ordered)} 个迁移为「已应用」（仅登记台账，未执行任何 SQL）。")
    if existing:
        print(f"（台账原有 {existing} 条，已按当前文件校验和更新）")


def print_migration_status() -> None:
    """只读：打印迁移台账 + 待应用清单。"""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            _ensure_migrations_table(cur)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT filename, checksum, statements, skipped, applied_at "
                        "FROM schema_migrations ORDER BY applied_at, filename")
            rows = cur.fetchall()
    print(f"\n迁移台账 schema_migrations：已应用 {len(rows)} 条")
    for r in rows:
        print(f"  - {r['filename']}  sha={str(r['checksum'])[:12]}…  "
              f"语句 {r['statements']} / 跳过 {r['skipped']}  @ {r['applied_at']}")
    ordered, _files = _ordered_upgrade_files()
    applied = {r["filename"] for r in rows}
    pending = [n for n in ordered if n not in applied]
    if pending:
        print("待应用（--upgrade-only 将按序执行）：")
        for n in pending:
            print(f"  - {n}")
    else:
        print("✓ 全部已登记迁移均已应用。")


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
    parser = argparse.ArgumentParser(description="初始化 Mock 银行数据库 / 版本化应用迁移")
    parser.add_argument("--sql", default=str(Path(__file__).resolve().parent / "sql" / "init.sql"))
    parser.add_argument("--upgrade-only", action="store_true",
                        help="跳过 init.sql，只版本化应用未应用的 sql/upgrade_*.sql")
    parser.add_argument("--baseline", action="store_true",
                        help="把现有迁移登记为「已应用」（只登记不执行），用于存量库纳入版本管理")
    parser.add_argument("--status", action="store_true",
                        help="只打印迁移台账与待应用清单后退出（只读）")
    args = parser.parse_args()

    if args.status:
        print(f"目标：{config.DB_USER}@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")
        print_migration_status()
        return

    if args.baseline:
        print(f"目标：{config.DB_USER}@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")
        print("模式：--baseline（采纳现有迁移，不执行 SQL）")
        baseline_migrations()
        return

    if args.upgrade_only:
        print(f"目标：{config.DB_USER}@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")
        print("模式：--upgrade-only（不跑 init.sql，只应用未应用迁移）")
        apply_upgrades()
        print(f"\n完成：迁移已就绪。数据库 {config.DB_NAME}。")
        return

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

    # ---- 版本化应用迁移（只跑未应用的）：init.sql 刚建好全新库 → fresh=True ----
    apply_upgrades(fresh=True)

    print(f"\n完成：成功执行 {executed} 条语句。数据库 {config.DB_NAME} 已就绪。")


if __name__ == "__main__":
    main()
