"""cleanup_checkpoints.py —— LangGraph checkpoint（bank.db）清理（审计 R7）

bank.db 是主图 SqliteSaver 的中断恢复存储，随对话持续增长且从未清理。
本脚本删除「最近更新时间早于 N 天」的 thread 的全部 checkpoint 行，
随后 VACUUM 回收空间。admin_logs / MySQL 业务数据不受影响。

用法（默认 dry-run，只统计不删除）：
    python scripts/cleanup_checkpoints.py           # 统计将清理的内容
    python scripts/cleanup_checkpoints.py --days 30 --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "bank.db"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="保留最近 N 天（默认 30）")
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认 dry-run）")
    args = ap.parse_args()

    if not DB.exists():
        print("bank.db 不存在（服务从未以默认目录启动）")
        return

    cutoff = time.time() - args.days * 86400
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # SqliteSaver 表结构自检（不同版本列名略有差异）
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in cur.fetchall()}
    if "checkpoints" not in tables:
        print("checkpoints 表不存在，无需清理")
        return

    # SqliteSaver 表无时间列 → 以 MySQL conversations 为"时间真相"：
    #   ① 孤儿 thread（checkpoint 有、MySQL 会话表无——CLI 测试/残渣）→ 一律清
    #   ② 会话最近活动（messages 最新 created_at）早于 N 天 → 清
    import pymysql
    from dotenv import dotenv_values
    env = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
    my = pymysql.connect(host=env["DB_HOST"], port=int(env["DB_PORT"]),
                         user=env["DB_USER"], password=env["DB_PASSWORD"],
                         database=env["DB_NAME"], cursorclass=pymysql.cursors.DictCursor)
    with my.cursor() as mc:
        mc.execute(
            "SELECT c.conversation_id, MAX(m.created_at) AS last_at "
            "FROM conversations c LEFT JOIN chat_messages m ON m.conversation_id=c.conversation_id "
            "GROUP BY c.conversation_id"
        )
        conv = {r["conversation_id"]: r["last_at"] for r in mc.fetchall()}
    my.close()

    cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
    threads = [r[0] for r in cur.fetchall()]
    import datetime as _dt
    cutoff_dt = _dt.datetime.now() - _dt.timedelta(days=args.days)
    stale = []
    for t in threads:
        last = conv.get(t)
        if last is None:
            stale.append(t)                       # 孤儿：UI 无法恢复的残渣
        elif isinstance(last, str):
            last = _dt.datetime.fromisoformat(last)
        if last is not None and last < cutoff_dt:
            stale.append(t)
    stale = sorted(set(stale))

    before_bytes = DB.stat().st_size
    cur.execute("SELECT COUNT(*) FROM checkpoints")
    total_rows = cur.fetchone()[0]
    print(f"将清理 {len(stale)} 个 thread（孤儿或 {args.days} 天未活跃）/ 共 {total_rows} 行 checkpoint")

    if args.apply and stale:
        ph = ",".join("?" * len(stale))
        cur.execute(f"DELETE FROM checkpoints WHERE thread_id IN ({ph})", stale)
        if "writes" in tables:
            cur.execute(f"DELETE FROM writes WHERE thread_id IN ({ph})", stale)
        conn.commit()
        cur.execute("VACUUM")
        print(f"已删除并 VACUUM；库大小 {before_bytes/1e6:.1f}MB → {DB.stat().st_size/1e6:.1f}MB")
    else:
        print("dry-run：未删除。加 --apply 执行。")

    conn.close()
    try:
        my.close()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
