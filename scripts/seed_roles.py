"""seed_roles.py —— 生成模块 A 分级演示账号（幂等，可重复执行）

★ 仅为演示"角色分级"落地：创建两个分级账号并赋予对应 role。
   - riskop / 123456      → risk_ops  （仅支付风控后台）
   - superadmin / 123456  → super_admin（最高权限：卡密生成/作废 + 角色管理）
   既有 admin / admin123 维持 admin 不变（通用后台）。

★ 走正式开户路径：create_user（pbkdf2 哈希）→ provision_new_user（账户/卡/流水）
   → UPDATE role。绝不手工绕过资产初始化。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import profile_db
from mock_bank import db
from utils.user_provision import provision_new_user


SEED = [
    ("riskop", "123456", "risk_ops"),
    ("superadmin", "123456", "super_admin"),
]


def seed_one(username: str, password: str, role: str) -> str:
    existing = db.fetch_one(
        "SELECT id, username, role FROM users WHERE username=%s", (username,)
    )
    if existing:
        if existing["role"] != role:
            db.execute("UPDATE users SET role=%s WHERE id=%s", (role, int(existing["id"])))
            return f"  · {username}: 已存在，role 调整为 {role}"
        return f"  · {username}: 已存在且 role={role}，跳过"

    created = profile_db.create_user(username, password)
    if not created:
        return f"  · {username}: 创建失败（用户名冲突？）"
    uid = int(created["user_id"])
    try:
        provision_new_user(uid)
    except Exception as exc:  # noqa: BLE001
        return f"  · {username}: 创建 uid={uid} 但开户异常：{exc}"
    db.execute("UPDATE users SET role=%s WHERE id=%s", (role, uid))
    return f"  · {username}: 已创建 uid={uid}，role={role}（密码 {password}）"


def main() -> None:
    print("生成模块 A 分级演示账号：")
    for username, pw, role in SEED:
        print(seed_one(username, pw, role))
    print("完成。可登录后台验证：riskop（风控 Tab）/ superadmin（卡密管理 + 角色管理）。")


if __name__ == "__main__":
    main()
