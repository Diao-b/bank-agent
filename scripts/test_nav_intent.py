"""test_nav_intent.py —— 导航意图判定：只有"明确要求打开页面"才允许自动跳转（验收）

背景（真实问题）：用户问「可以查看我最近的流水账单吗?」——
他只是**咨询**，却被自动跳到了账单页面。根因是 `_reply_payload` 里
只要模型调了 `navigate_to` 就无条件 `auto=True`，前端收到即 `location.hash = nav.to`，
把正在对话的用户踢走。

现在的契约（本脚本即以此为准）：
  · 用户原话**明确要求打开/进入/跳转某个页面** → `nav.auto = True`（自动跳转）
  · 其余（咨询 / 想了解 / 问能不能）        → `nav` 仍给，但**不带 auto**（前端只渲染可点按钮）
  · Agent 正挂起等用户输入（suspended）      → `nav` 整个摘掉（不打断半途流程）

不需要起服务：`python scripts/test_nav_intent.py`
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_server as S  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
#  1) 判定函数：咨询 vs 明确指令
# --------------------------------------------------------------------------- #
CONSULT = [                       # 必须 False —— 只是问问题
    "可以查看我最近的流水账单吗?",
    "这个月花了多少",
    "我想了解一下理财",
    "能看看我的订阅吗",
    "我的卡片有哪些",
    "最近有哪些订阅在扣钱",
    "余额还有多少",
    "帮我转账 2000 给李雷",        # 动钱指令 ≠ 打开页面指令
]
EXPLICIT = [                      # 必须 True —— 明确要求打开页面
    "打开我的账单详情",
    "打开账单",
    "去转账",
    "进入安全中心",
    "跳转到理财页面",
    "回到对话",
    "帮我打开卡片管理",
    "切换到订阅管理",
]

print("[1] _explicit_page_request：咨询不该被判成导航指令")
for t in CONSULT:
    check(f"咨询 → False：{t}", S._explicit_page_request(t) is False)

print("\n[2] _explicit_page_request：明确指令要能识别")
for t in EXPLICIT:
    check(f"指令 → True：{t}", S._explicit_page_request(t) is True)


# --------------------------------------------------------------------------- #
#  2) _reply_payload 的授权闸门（用假 driver 喂一个 navigate_to 工具调用）
#     确定性验证 —— 不依赖模型这次心情好不好
# --------------------------------------------------------------------------- #
class _Snap:
    values = {
        "messages": [
            {"role": "user", "content": "（本轮）"},
            {"role": "assistant", "tool_calls": [
                {"name": "navigate_to", "args": {"page": "/bills"}}]},
            {"role": "assistant", "content": "已处理好。"},
        ]
    }


class _Graph:
    def get_state(self, cfg):
        return _Snap()


class _Driver:
    graph = _Graph()
    cfg = {}

    def __init__(self, suspended=False):
        self._suspended = suspended

    def suspended(self):
        return self._suspended


print("\n[3] _reply_payload：模型调了 navigate_to，但用户只是咨询 → 不得自动跳转")
p = S._reply_payload("您这个月共 6 笔支出。", _Driver(),
                     user_text="可以查看我最近的流水账单吗?")
nav = p.get("nav")
check("给出了可点提示（nav 存在）", bool(nav), f"nav={nav}")
check("★ 但没有 auto（前端不会跳转）", not (nav or {}).get("auto"), f"nav={nav}")

print("\n[4] _reply_payload：用户明确要求打开 → 允许自动跳转")
p2 = S._reply_payload("已为您打开账单页面。", _Driver(), user_text="打开我的账单详情")
nav2 = p2.get("nav")
check("★ nav.auto is True", (nav2 or {}).get("auto") is True, f"nav={nav2}")
check("目标页正确 /bills", (nav2 or {}).get("to") == "/bills", f"nav={nav2}")

print("\n[5] 兜底：不给 user_text（历史调用/内部调用）→ 默认不自动跳转")
p3 = S._reply_payload("已处理好。", _Driver())
check("无 user_text 时不带 auto", not (p3.get("nav") or {}).get("auto"), f"nav={p3.get('nav')}")

print("\n[6] 既有行为不回归：Agent 挂起中 → nav 整个摘掉")
p4 = S._reply_payload("请补充收款人。", _Driver(suspended=True), user_text="打开账单")
check("suspended 时无 nav", p4.get("nav") is None, f"nav={p4.get('nav')}")

print("\n[7] 前端契约一致性（静态检查）")
fe = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "web", "index.html"), encoding="utf-8").read()
check("前端仅在 nav.auto 时 navigate()", "nav.auto &&" in fe or "nav && nav.auto" in fe)
check("前端有 NavHint（可点提示）分支", "function NavHint" in fe)

print(f"\n结果：PASS {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("ALL GREEN")
