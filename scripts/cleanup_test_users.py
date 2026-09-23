"""运维脚本：清理「测试注册」遗留用户（P2.2）。

背景
----
部分验收脚本**每次运行都新建一个用户**且不回收，长期累积：

| 前缀 | 来源脚本 | 命名 |
|---|---|---|
| `c6_probe_` / `c6_lock_` | `scripts/test_admin_ops.py` | 时间戳 |
| `rn_t_` | `scripts/test_realname_verified.py` | 随机数 |
| `dbg_` | 临时排障脚本 | 时间戳 |

结果：治理后的演示库（13 个真实/演示账号）里混进了三十多个测试账号。
本脚本把它们清掉，**只动测试账号**，绝不碰演示账号与审计日志。

分两档，默认只清 A 档
--------------------
* **A 档（每次跑必新增，无回收价值）**：`c6_probe_` · `c6_lock_` · `rn_t_` · `dbg_`
* **B 档（专用复用型测试用户，脚本自己会清理业务数据）**：`itest_` · `cardtest_` · `probe_`
  —— 需显式 `--include-candidates` 才会纳入。

★ 删除走 `profile_db.delete_user_cascade()`（P0 起的唯一合法删用户入口，带外键级联），
  **不手工写 `DELETE FROM users`**。

★ 安全护栏：管理员账号永不删除；`KEEP` 演示账号永不删除；删任何用户前打印清单。

用法
----
    .venv/Scripts/python.exe scripts/cleanup_test_users.py                  # 只列清单（默认，不改数据）
    .venv/Scripts/python.exe scripts/cleanup_test_users.py --include-candidates
    .venv/Scripts/python.exe scripts/cleanup_test_users.py --apply           # 真正删除（A 档）
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql  # noqa: E402

import config  # noqa: E402
import profile_db  # noqa: E402

# A 档：每次运行必新增的测试账号前缀
PATTERNS_A = ("c6_probe_", "c6_lock_", "rn_t_", "dbg_")
# B 档：专用复用型测试用户（脚本自己清理业务数据）
PATTERNS_B = ("itest_", "cardtest_", "probe_")
# 演示 / 真实账号：无论如何都不删（按 username 精确匹配）
KEEP = {"user1", "user2", "admin", "lige", "刁伟民", "刁紫豫", "diao", "wei"}


def conn():
    cfg = dict(config.db_config())
    cfg["cursorclass"] = pymysql.cursors.DictCursor
    return pymysql.connect(**cfg)


def find_targets(patterns: tuple[str, ...]) -> list[dict]:
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT id, username, role FROM users ORDER BY id")
            rows = cur.fetchall()
    hits = []
    for r in rows:
        u = r["username"] or ""
        if u in KEEP or r["role"] == "admin":
            continue
        if any(u.startswith(p) for p in patterns):
            hits.append(r)
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="清理测试注册遗留用户")
    ap.add_argument("--include-candidates", action="store_true",
                    help="连同 B 档（itest_/cardtest_/probe_ 等复用型测试用户）一起清理")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只列清单）")
    args = ap.parse_args()

    patterns = PATTERNS_A + (PATTERNS_B if args.include_candidates else ())
    targets = find_targets(patterns)
    keep_hits = []
    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            total = int(cur.fetchone()["n"])
            cur.execute("SELECT username FROM users ORDER BY id")
            for r in cur.fetchall():
                u = r["username"] or ""
                if u in KEEP:
                    keep_hits.append(u)

    print(f"当前 users 共 {total} 行；匹配档位：{'A+B' if args.include_candidates else 'A'} "
          f"（前缀 {', '.join(patterns)}）")
    print(f"保留（演示/真实账号）：{', '.join(sorted(keep_hits))}")
    print(f"\n--- 命中测试账号 {len(targets)} 个 ---")
    for r in targets:
        print(f"  #{r['id']:<5} {r['username']}")

    if not targets:
        print("\n无命中，无需清理。")
        return 0
    if not args.apply:
        print(f"\n[dry-run] 未做任何变更。确认后执行："
              f"\n  .venv/Scripts/python.exe scripts/cleanup_test_users.py"
              f"{' --include-candidates' if args.include_candidates else ''} --apply")
        return 0

    print("\n--- 走 delete_user_cascade 删除（带外键级联）---")
    ok = 0
    for r in targets:
        try:
            res = profile_db.delete_user_cascade(int(r["id"]))
            print(f"  #{r['id']:<5} {r['username']:<24} OK  级联删除 {res.get('deleted_total')} 行")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  #{r['id']:<5} {r['username']:<24} FAIL {exc}")

    with conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            left = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'")
            admins = int(cur.fetchone()["n"])
    print(f"\n删除 {ok}/{len(targets)}；users {total} → {left}；管理员仍 {admins} 个")
    with conn() as c:
        with c.cursor() as cur:
            # ★ 只看**外部**收款人（linked_user_id IS NULL）：站内收款人（如「李雷→李哥」）
            #   本就不该出现在 external_accounts，算进来是误报。
            cur.execute(
                "SELECT COUNT(*) AS n FROM payees p "
                "WHERE p.linked_user_id IS NULL AND p.card_no IS NOT NULL AND p.card_no<>'' "
                "AND NOT EXISTS (SELECT 1 FROM external_accounts e WHERE e.card_no=p.card_no)")
            dangling = int(cur.fetchone()["n"])
    if dangling:
        # ★ 刻意不自动补登记：既有「卡号重生成导致的真漂移」（该补），
        #   也有**故意不登记**的验收用幽灵卡（补了会让 test_crossbank_settlement 直接失效）。
        print(f"[提示] 有 {dangling} 个外部收款人卡号未登记 external_accounts —— "
              f"**不要盲目跑 seed**，请先逐个确认：")
        print("       SELECT id,user_id,nickname,card_no FROM payees "
              "WHERE linked_user_id IS NULL AND card_no NOT IN "
              "(SELECT card_no FROM external_accounts);")
        print("       （真漂移 = 补登记；「幽灵卡号（验收测试）」等 = 故意留着，补了会破坏跨行验收）")
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
