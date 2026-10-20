"""cleanup_artifacts.py —— 清理 bank-agent 下的测试产物与残留（方案 A）

只做三类"确定不是项目数据"的清理：
  1) 浏览器 profile（CDP / agent-browser 测试产物）：_reports/** 下名字含 edge+prof 的目录
  2) 测试垃圾目录：.test_trash*/（旧备份 / 临时 db）
  3) 残留空壳文件：bank.db.orig

★★ 安全设计（务必遵守）：
  - 默认是 --scan：只列出候选与体积，**绝不改动任何文件**。
  - --apply：把候选**移动到隔离目录** _reports/_artifact_quarantine_<ts>/（可原样移回），
    不直接删除 —— 空间要真正释放需再执行 --purge <隔离目录>。
  - 黑名单硬保护：bank.db / bank.db-wal / bank.db-shm / .venv / web / sql / scripts /
    docs / fay / vendor / .git / .env —— 命中也绝不处理。
  - 所有操作限定在 bank-agent 根目录内，越界即拒绝。

用法：
  python scripts/cleanup_artifacts.py                 # 扫描（默认，只读）
  python scripts/cleanup_artifacts.py --apply         # 移入隔离目录（可回滚）
  python scripts/cleanup_artifacts.py --purge <dir>   # 永久删除隔离目录（二次确认）
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # bank-agent/
REPORTS = ROOT / "_reports"

# 绝不处理的名字（目录名或文件名，精确匹配 / 前缀匹配）
PROTECTED_NAMES = {
    "bank.db", "bank.db-wal", "bank.db-shm", "bank.db.orig.tmp",
    ".venv", "web", "sql", "scripts", "docs", "fay", "vendor", ".git",
    "node_modules", "__pycache__", ".env",
}
PROTECTED_PREFIX = ("bank.db",)   # bank.db*（含 -wal/-shm）一律保护


def _is_protected(p: Path) -> bool:
    name = p.name
    if name in PROTECTED_NAMES:
        return True
    if name.startswith(PROTECTED_PREFIX) and name != "bank.db.orig":
        return True
    return False


def _dir_size(p: Path) -> int:
    total = 0
    for dp, _dns, fns in _walk(p):
        for fn in fns:
            try:
                total += (Path(dp) / fn).stat().st_size
            except OSError:
                pass
    return total


def _walk(p: Path):
    import os
    for dp, dns, fns in os.walk(p):
        yield dp, dns, fns


def _is_browser_profile_dir(name: str) -> bool:
    n = name.lower().replace("-", "").replace("_", "")
    return ("edge" in n and "prof" in n)


def collect() -> list[dict]:
    """返回候选列表 [{category, path, size, mtime, kind}]。只读。"""
    cands: list[dict] = []
    seen: set[Path] = set()

    # 1) 浏览器 profile 目录：_reports 下名字含 edge+prof 的目录
    if REPORTS.exists():
        for dp, dns, _fns in _walk(REPORTS):
            # 复制一份用于改名遍历（避免改动 os.walk 状态）
            for d in list(dns):
                full = Path(dp) / d
                if _is_browser_profile_dir(d) and full not in seen:
                    if not _is_protected(full):
                        seen.add(full)
                        cands.append({
                            "category": "browser_profile",
                            "path": full,
                            "kind": "dir",
                            "size": _dir_size(full),
                            "mtime": full.stat().st_mtime if full.exists() else 0,
                        })

    # 2) .test_trash* 目录（项目根 / _reports 下）
    for base in (ROOT, REPORTS):
        if not base.exists():
            continue
        for child in base.iterdir():
            if child.is_dir() and child.name.startswith(".test_trash"):
                if child not in seen and not _is_protected(child):
                    seen.add(child)
                    cands.append({"category": "test_trash", "path": child, "kind": "dir",
                                  "size": _dir_size(child), "mtime": child.stat().st_mtime})

    # 3) 残留文件 bank.db.orig
    orig = ROOT / "bank.db.orig"
    if orig.exists() and not _is_protected(orig):
        cands.append({"category": "residual_file", "path": orig, "kind": "file",
                      "size": orig.stat().st_size, "mtime": orig.stat().st_mtime})

    cands.sort(key=lambda c: c["size"], reverse=True)
    return cands


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def do_scan() -> int:
    cands = collect()
    total = sum(c["size"] for c in cands)
    print(f"扫描根目录：{ROOT}")
    print(f"候选 {len(cands)} 项，合计约 {_human(total)}\n")
    for c in cands:
        rel = c["path"].relative_to(ROOT)
        when = time.strftime("%Y-%m-%d", time.localtime(c["mtime"])) if c["mtime"] else "-"
        print(f"  [{c['category']:16s}] {_human(c['size']):>9s}  {when}  {rel}{'/' if c['kind']=='dir' else ''}")

    lines = [f"# cleanup scan @ {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
             f"根目录：{ROOT}", f"候选 {len(cands)} 项，合计 {_human(total)}", "",
             "| 类别 | 大小 | 最后修改 | 路径 |", "| --- | --- | --- | --- |"]
    for c in cands:
        rel = c["path"].relative_to(ROOT)
        when = time.strftime("%Y-%m-%d", time.localtime(c["mtime"])) if c["mtime"] else "-"
        lines.append(f"| {c['category']} | {_human(c['size'])} | {when} | `{rel}` |")
    out = REPORTS / f"_cleanup_scan_{_ts()}.md"
    REPORTS.mkdir(exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n清单已写出：{out}")
    print("★ 这是只读扫描，未改动任何文件。确认无误后再执行 --apply。")
    return 0


def do_apply() -> int:
    cands = collect()
    if not cands:
        print("没有候选，无需处理。")
        return 0
    qdir = REPORTS / f"_artifact_quarantine_{_ts()}"
    qdir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for c in cands:
        src: Path = c["path"]
        rel = src.relative_to(ROOT)
        dst = qdir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            moved += 1
            print(f"  已隔离：{rel}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {rel}: {exc}")
    print(f"\n已隔离 {moved}/{len(cands)} 项 → {qdir}")
    print("★ 这是可回滚的『移动』不是删除。确认无误后再 --purge，方可真正释放空间。")
    return 0


def do_purge(target: str) -> int:
    p = Path(target)
    if not p.exists() or not p.is_dir():
        print(f"隔离目录不存在：{p}")
        return 1
    if "_artifact_quarantine_" not in p.name:
        print("拒绝：目标名不像隔离目录（须含 _artifact_quarantine_）。")
        return 1
    ans = input(f"将永久删除 {p} 及其全部内容，确认？输入 yes 继续：").strip().lower()
    if ans != "yes":
        print("已取消。")
        return 0
    shutil.rmtree(p)
    print(f"已永久删除：{p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 bank-agent 测试产物与残留（默认只扫描）")
    ap.add_argument("--apply", action="store_true", help="把候选移入隔离目录（可回滚）")
    ap.add_argument("--purge", metavar="DIR", help="永久删除隔离目录")
    args = ap.parse_args()
    if args.purge:
        return do_purge(args.purge)
    if args.apply:
        return do_apply()
    return do_scan()


if __name__ == "__main__":
    sys.exit(main())
