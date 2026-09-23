"""utils/deps.py —— FastAPI 依赖（跨域唯一归属）

★ 为什么单列一个文件：
  - `utils/security.py` **刻意不依赖 FastAPI**（好让 Agent / 脚本在无 HTTP 上下文时复用
    同一份 token→uid 解析逻辑，见其头部注释）；
  - 而路由层需要 `current_uid` 这个 FastAPI 依赖，且各**域模块**（card / wealth …）要在
    **定义期**引用它（`Depends(current_uid)` 在函数定义时即求值）。
  放在这里两头兼顾：security 保持纯净，`current_uid` 只有一份定义（不必每个域模块各写一遍
  Header 声明），域模块也因此不必顶层 import `mock_bank.main`（避免循环导入）。
"""
from __future__ import annotations

from fastapi import Header, HTTPException

from utils import security
from utils.security import AuthError


def current_uid(
    authorization: str | None = Header(default=None),
    x_user_token: str | None = Header(default=None, alias="x-user-token"),
) -> int:
    """★ 唯一的 user_id 入口：从签名 token 解析当前用户。

    前端只能靠 Authorization 头证明自己是谁，传任何 user_id 参数都不作数。
    """
    try:
        uid = security.resolve_user_id(security.extract_token(authorization, x_user_token))
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    # ★ 安全修复（审计 R2）：管理员锁定的账号，旧 token 也一律拒绝。
    #   （此前"锁定"只挡新登录，已登录会话 24h 内照常读写真实资产。）
    from profile_db import get_user_locked  # noqa: PLC0415 惰性导入避免循环
    if get_user_locked(uid):
        raise HTTPException(status_code=403, detail="账号已被锁定，请联系管理员或柜台处理")
    return uid
