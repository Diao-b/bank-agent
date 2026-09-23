"""
db_snapshot.py —— 无 mysqldump 时的 MySQL bank_agent 快照/恢复（保证基线可复现）

为什么需要：行为零变化 diff 要求「关键表 count」在重构前后完全一致。但回归脚本会改动
数据，所以每次重录基线前先把库还原到录制黄金基线那一刻的快照。

仅用于演示库 bank_agent 的「数据」还原，不动 schema（重构不迁移表结构）。

用法（bank-agent/ 下，.venv）：
    .venv/Scripts/python.exe tests/baseline/db_snapshot.py save    [--out tests/baseline/bank_agent.snapshot.json]
    .venv/Scripts/python.exe tests/baseline/db_snapshot.py restore [--in  tests/baseline/bank_agent.snapshot.json] [--force]

★ 安全护栏（P2.2）
--------------
`restore` 是 `DELETE + INSERT` 全表覆盖，**用错快照会把整库回滚**（真实事故：9-11 录的
53-user 快照对着治理后的 13-user 库，跑一次 run_regression 就把治理成果抹掉）。
因此 save 会在快照里写一枚**代际指纹**（`__meta__`：录制时间 + `users.id` 全量），
restore 前先比对当前库 —— 不同代 or 旧格式无指纹 → **拒绝还原**并给出处置建议；
确需强行覆盖必须显式 `--force`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pymysql
import config

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bank_agent.snapshot.json")
META_KEY = "__meta__"


class SnapshotMismatch(RuntimeError):
    """快照与当前库不同代（或旧格式无指纹）—— 拒绝还原，避免误覆盖整库。"""


def conn():
    # config.db_config() 已含 cursorclass=DictCursor
    return pymysql.connect(**config.db_config())


def _live_user_ids() -> list[int]:
    """当前库的用户 id 全量 —— 作为「库的代际」指纹（测试不会改用户集合）。"""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT id FROM users ORDER BY id")
            return [int(r["id"]) for r in cur.fetchall()]


def _ser(v):
    from decimal import Decimal
    from datetime import datetime, date

    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def save(out=OUT) -> None:
    from datetime import datetime

    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SHOW TABLES")
            tbls = [list(r.values())[0] for r in cur.fetchall()]
            data: dict[str, list] = {}
            for t in tbls:
                cur.execute(f"SELECT * FROM `{t}`")
                rows = cur.fetchall()
                data[t] = [{k: _ser(v) for k, v in r.items()} for r in rows]
            cur.execute("SELECT id FROM users ORDER BY id")
            uids = [int(r["id"]) for r in cur.fetchall()]
    data[META_KEY] = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "db": config.DB_NAME,
        "users_ids": uids,
        "users_count": len(uids),
        "tables": len(tbls),
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str)
    print(f"snapshot saved: {out} ({len(tbls)} tables, "
          f"{sum(len(v) for k, v in data.items() if k != META_KEY)} rows, "
          f"users={len(uids)})")


def check(inp=OUT) -> dict:
    """只读体检：快照是否与当前库同代。返回 meta；不同代则抛 SnapshotMismatch。"""
    with open(inp, encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get(META_KEY)
    live = _live_user_ids()
    if meta is None:
        raise SnapshotMismatch(
            f"快照 {os.path.basename(inp)} 无指纹（P2.2 之前的旧格式），无法确认与当前库同代。\n"
            f"  当前库 users={len(live)} 行。\n"
            f"  ★ 正常做法：以当前库为准重录 —— python tests/baseline/db_snapshot.py save\n"
            f"  ★ 确实要用它覆盖：restore --force"
        )
    snap_ids = [int(x) for x in (meta.get("users_ids") or [])]
    if snap_ids != live:
        raise SnapshotMismatch(
            f"快照与当前库**不是同一代**数据，拒绝还原（否则会把整库回滚）。\n"
            f"  快照 users={len(snap_ids)} 行（录制于 {meta.get('captured_at')}，表 {meta.get('tables')} 张）\n"
            f"  当前 users={len(live)} 行\n"
            f"  ★ 若当前库才是权威：重录快照 —— python tests/baseline/db_snapshot.py save\n"
            f"  ★ 若确实要用旧快照覆盖：restore --force"
        )
    return meta


def restore(inp=OUT, force: bool = False) -> None:
    with open(inp, encoding="utf-8") as f:
        data = json.load(f)
    meta = data.pop(META_KEY, None)
    if not force:
        check(inp)   # 不同代/无指纹 → SnapshotMismatch（__meta__ 已在上面 pop，check 会重新读）
    failed = []
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for t, rows in data.items():
                try:
                    cur.execute(f"DELETE FROM `{t}`")
                    if not rows:
                        continue
                    cols = list(rows[0].keys())
                    placeholders = ", ".join(["%s"] * len(cols))
                    colsql = ", ".join(f"`{col}`" for col in cols)
                    sql = f"INSERT INTO `{t}` ({colsql}) VALUES ({placeholders})"
                    cur.executemany(sql, [[r.get(col) for col in cols] for r in rows])
                except Exception as exc:  # noqa: BLE001
                    failed.append((t, str(exc)))
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        c.commit()
    if failed:
        print(f"snapshot restored with {len(failed)} table errors:")
        for t, e in failed:
            print(f"  - {t}: {e}")
    else:
        note = "（--force，未做代际校验）" if force else f"（users={len((meta or {}).get('users_ids') or [])}）"
        print(f"snapshot restored: {inp} ({len(data)} tables){note}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["save", "restore", "check"])
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--in", dest="inp", default=OUT)
    ap.add_argument("--force", action="store_true", help="跳过代际校验（危险：会覆盖当前库）")
    a = ap.parse_args()
    if a.cmd == "save":
        save(a.out)
    elif a.cmd == "check":
        try:
            m = check(a.inp)
            print(f"OK 快照与当前库同代：users={m['users_count']} 录制于 {m['captured_at']}")
        except SnapshotMismatch as exc:
            print(f"[拒绝] {exc}")
            sys.exit(3)
    else:
        try:
            restore(a.inp, force=a.force)
        except SnapshotMismatch as exc:
            print(f"[拒绝] {exc}")
            sys.exit(3)
