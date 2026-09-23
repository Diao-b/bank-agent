"""
scripts/check_migrations.py —— 迁移治理只读体检（P1.1）

检查项（**只读，不改库**）：
  1. UPGRADE_ORDER 里登记的文件是否都存在于 sql/；
  2. sql/ 下是否存在未登记顺序的 upgrade_*.sql；
  3. 台账 schema_migrations 已应用的迁移，当前文件 sha256 是否与登记一致（漂移检测）；
  4. 是否还有未应用的迁移。

退出码：0 = 全部一致；1 = 存在漂移 / 缺失 / 未登记 / 未应用。

用法（bank-agent/ 下，.venv）：
    .venv/Scripts/python.exe scripts/check_migrations.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import pymysql  # noqa: E402
import init_db  # noqa: E402  复用同一份顺序定义 / 校验和实现
from init_db import UPGRADE_ORDER, _MIG_DIR, _sha256_text  # noqa: E402


def db() -> pymysql.connections.Connection:
    return pymysql.connect(**config.db_config())


def main() -> int:
    files = {p.name: p for p in _MIG_DIR.glob("upgrade_*.sql")}
    registered = list(UPGRADE_ORDER)
    missing = [n for n in registered if n not in files]
    unregistered = sorted(set(files) - set(registered))

    print(f"迁移目录：{_MIG_DIR}")
    print(f"已登记 {len(registered)} 个 / 磁盘上 {len(files)} 个")

    rows: list[dict] = []
    try:
        with db() as c:
            with c.cursor() as cur:
                cur.execute("SELECT filename, checksum, statements, skipped, applied_at "
                            "FROM schema_migrations")
                rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"\n[台账缺失] schema_migrations 不可读（{e}）。")
        print("  存量库请先：python init_db.py --baseline")
    applied = {r["filename"]: r["checksum"] for r in rows}

    problems: list[str] = []
    if missing:
        problems.append(f"UPGRADE_ORDER 登记但磁盘缺失：{missing}")
    if unregistered:
        problems.append(f"磁盘存在但未登记顺序：{unregistered}")

    drift: list[str] = []
    for name, ck in applied.items():
        p = files.get(name)
        if p is None:
            drift.append(f"{name}（已登记但文件已删除）")
            continue
        cur_sum = _sha256_text(p.read_text(encoding="utf-8"))
        if cur_sum != ck:
            drift.append(f"{name}（sha {str(ck)[:8]}→{cur_sum[:8]}）")
    if drift:
        problems.append("台账与文件不一致（漂移）：" + "; ".join(drift))

    pending = [n for n in (registered + unregistered) if n not in applied]
    if pending:
        problems.append(f"未应用：{pending}")

    print(f"台账已应用：{len(applied)} 条")
    if pending:
        print("待应用：")
        for n in pending:
            print("  -", n)

    if problems:
        print("\n[体检未通过]")
        for p in problems:
            print("  ✗", p)
        return 1
    print("\n✓ 迁移一致：无漂移 / 无缺失 / 无未登记 / 无待应用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
