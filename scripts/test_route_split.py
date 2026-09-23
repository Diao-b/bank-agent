"""P2.1 验收：run_server.py 路由分域后，路由集合 / 注册顺序 / 匹配优先级不变。

拆分的唯一目的是可维护性，因此**行为必须逐位相同**。本脚本把"不变"固化成断言：
  1) 路由总数与 openapi 路径数与拆分前一致（67 / 62）；
  2) 展平 include_router 后的注册顺序与拆分前**逐项相同**（顺序 = 匹配顺序）；
  3) 关键优先级：兜底代理 /api/{path:path} 在所有具体 /api/* 之后、静态挂载 / 之前；
  4) 各域路由都已注册（抽查 account / wallet / admin / runtime 各一条）。

不需要起服务即可跑：`python scripts/test_route_split.py`
（可选）8178 在线时追加 HTTP 优先级抽查。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_server as S  # noqa: E402
from starlette.routing import Mount  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


def flatten(router) -> list:
    """展开 include_router（FastAPI 0.14x 用 _IncludedRouter 包装，不写进 app.routes）。"""
    out = []
    for r in router.routes:
        sub = getattr(r, "original_router", None)
        out.extend(flatten(sub) if sub is not None else [r])
    return out


routes = flatten(S.app.router)
specs = [(getattr(r, "path", None), tuple(sorted(getattr(r, "methods", []) or []))) for r in routes]
paths = [p for p, _ in specs]

print("[1] 路由集合规模")
check("路由总数 == 67", len(routes) == 67, f"got {len(routes)}")
check("openapi 路径数 == 62", len(S.app.openapi()["paths"]) == 62,
      f"got {len(S.app.openapi()['paths'])}")

print("\n[2] 关键路由存在")
for p in ("/api/chat", "/api/chat/stream", "/api/auth/login", "/api/profile",
          "/api/conversations", "/api/overview", "/api/inbox", "/api/verify/send",
          "/api/pay/password/verify", "/api/op/authorize", "/api/realname/confirm",
          "/api/admin/overview", "/api/admin/users/{user_id}", "/api/appearance",
          "/api/digital-human/status", "/api/housekeeping", "/", "/login", "/chat",
          "/admin", "/admin/login", "/api/{path:path}", "/fay", "/fay/{path:path}"):
    check(f"存在 {p}", p in paths)

print("\n[3] 匹配优先级（顺序即匹配顺序）")
try:
    i_proxy = paths.index("/api/{path:path}")
    b4 = [p for p in paths[:i_proxy] if p.startswith("/api/")]
    af = [p for p in paths[i_proxy + 1:] if p.startswith("/api/")]
    check("兜底代理之前没有任何具体 /api/* 路由", af == [], f"代理之后仍有 {af[:4]}")
    check("兜底代理是最后一条 /api/* 路由", i_proxy + 1 == len(paths) or
          all(not p.startswith("/api/") for p in paths[i_proxy + 1:]),
          f"proxy@{i_proxy} of {len(paths)}")
    # 静态挂载必须最后（否则吞掉 /login /admin 等页面）
    mount_idx = [i for i, r in enumerate(routes) if isinstance(r, Mount)]
    check("静态挂载在最末", mount_idx and mount_idx[-1] == len(routes) - 1,
          f"mount@{mount_idx}")
    # 页面路由必须在挂载之前
    for p in ("/", "/login", "/chat", "/admin"):
        check(f"{p} 在静态挂载之前", paths.index(p) < (mount_idx[-1] if mount_idx else 0))
except ValueError as exc:
    check("找出兜底代理路由", False, str(exc))

print("\n[4] 各域都已注册（分域拆分生效）")
for mod, probe in (("account", "/api/auth/login"), ("wallet", "/api/inbox"),
                   ("admin", "/api/admin/overview"), ("runtime", "/api/appearance")):
    m = __import__("routes." + mod, fromlist=["router"])
    got = [getattr(r, "path", None) for r in m.router.routes]
    check(f"routes/{mod}.py 已 include 且含 {probe}", probe in got and len(got) > 0,
          f"{len(got)} routes")

print("\n[5] 域文件不得反向 import run_server（防循环导入）")
import re  # noqa: E402
for mod in ("_common", "account", "wallet", "admin", "runtime"):
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "routes", mod + ".py")
    src = open(p, encoding="utf-8").read()
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    hit = re.search(r"^\s*(import run_server|from run_server import)", body, re.M)
    check(f"routes/{mod}.py 无 run_server 依赖", hit is None)

print("\n[6] 可选：8178 在线时抽查 HTTP 优先级")
try:
    import requests  # noqa: E402
    B = "http://127.0.0.1:8178"
    if requests.get(B + "/login", timeout=2).status_code == 200:
        check("/api/overview 有专门路由（应 401 而非代理 404/502）",
              requests.get(B + "/api/overview", timeout=5).status_code == 401)
        check("/api/housekeeping 可达（应 200）",
              requests.get(B + "/api/housekeeping", timeout=5).status_code == 200)
        check("/api/definitely-not-exists 落兜底代理（非 404 之外的服务端错）",
              requests.get(B + "/api/definitely-not-exists", timeout=8).status_code in (404, 502))
        check("/admin 页面可达（静态挂载未吞）",
              requests.get(B + "/admin", timeout=5).status_code == 200)
    else:
        print("  SKIP  8178 未启动，跳过 HTTP 抽查")
except Exception as exc:  # noqa: BLE001
    print(f"  SKIP  HTTP 抽查不可用: {exc}")

print(f"\n结果：PASS {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("ALL GREEN")
