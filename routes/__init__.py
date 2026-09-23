"""P2.1 路由分域包。

run_server.py 曾单文件承载 ~70 个路由；此处按域拆出，只为可维护性。
★ 拆分不改行为：注册顺序 = 匹配顺序，具体路由仍排在兜底代理 /api/{path:path} 之前。
★ 域文件顶层不得 import run_server（会循环导入）；共享 helper 收口到 routes/_common.py。
"""
