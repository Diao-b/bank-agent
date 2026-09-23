"""
agent/write_flow.py —— 写操作子图（确定性工作流 + 人机中断）

设计核心（违反任何一条都算实现错误）：
1. 流程走向 100% 由代码条件边决定；LLM 只做三件事：
   - 要素抽取（fill_params 调 LLM 抽出 JSON 槽位）
   - 生成追问文本（clarify 的通用缺参追问话术）
   - 生成最终回复（execute 之后由 mock 银行返回的消息承载）
   LLM 永远不决定「下一步走哪」。
2. 三个 interrupt() 人机中断：clarify（要素不全）/ verify（验证码）/ confirm（操作确认）。
3. interrupt() 之前的节点代码无副作用：verify/send 已做成幂等（重放复用同一验证码），
   其余前置步骤均为只读（LLM 调用 / 查收款人等），重放安全。
4. 确认判断用关键词硬匹配（is_confirm / is_cancel），禁止用 LLM 判断 → 天然免疫提示注入。
5. 风控规则用纯代码实现（risk_check 节点按 ACTION_SPECS 计算 need_verify），不依赖 LLM。

ACTION_SPECS：所有写业务写死在这张表，新增业务只改这张表。

驱动方式（全局唯一，见 run_cli_write.py）：
    state = graph.get_state(config)
    if state.next:  # 挂起在 interrupt
        out = graph.invoke(Command(resume=user_input), config)
    else:
        out = graph.invoke({"messages":[("user", user_input)], "task": {...}, ...}, config)
    out.__interrupt__ 非空 → 取 question 展示；否则取 result。
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Annotated, Any, TypedDict

import requests
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from openai import OpenAI

import config
import profile_db
import rules
from utils import event_bus, paypass, verify_codes
from utils.security import auth_headers, get_current_user_id
from .state import WriteState, WriteIn

# --------------------------------------------------------------------------- #
#  verify 中断标记（run_server._parse_verify 据此生成结构化 verify 卡片）
#  —— 验证码不再出现在对话文本里（收件箱就是"手机"）；
#     支付密码绝不入中断文本 / chat_messages / checkpoint。
# --------------------------------------------------------------------------- #
# 前端核验成功后回传的固定占位符（密码本身绝不回传、绝不落盘）
PWD_RESUME_PLACEHOLDER = "[已输入支付密码]"
PWD_SET_PLACEHOLDER = "[已设置支付密码]"


# 子图输出 schema：只把 messages 与 result 回传给主图，
# 内部字段（params / asked_times / verified / confirmed / confirm_retries /
# verify_attempts）不外泄，也避免污染主图状态。
class WriteOut(TypedDict):
    messages: Annotated[list, add_messages]
    result: dict

# --------------------------------------------------------------------------- #
#  LLM Client（可注入，便于离线自测）
# --------------------------------------------------------------------------- #
_LLM_CLIENT = None  # 由 build_write_flow(llm_client=...) 注入；None 则构造 DeepSeek


def build_client() -> OpenAI:
    # ★ 显式超时/重试（SDK 默认 600s/2 次）：避免上游卡住时写流程无限挂起。
    return OpenAI(
        api_key=config.require_llm_key(),
        base_url=config.DEEPSEEK_BASE_URL,
        timeout=config.LLM_TIMEOUT,
        max_retries=config.LLM_MAX_RETRIES,
    )


def _client() -> Any:
    return _LLM_CLIENT if _LLM_CLIENT is not None else build_client()


# --------------------------------------------------------------------------- #
#  通用写操作规格表（新增业务只改这里）
# --------------------------------------------------------------------------- #
def _is_frequent_false(p: dict) -> bool:
    """收款人「非常用」→ 命中风控，需短信验证。

    ★ 坑：payee_info 来自 mock_bank 的 JSON，is_frequent 是 int(0/1) 而非 bool。
      早期写法 `.get("is_frequent") is False` 中 `0 is False` 恒为 False
      （int 0 与 bool False 不是同一对象）→ 该规则从未生效，非常用收款人
      小额转账可绕过短信验证。改为真值判断。
    ★ payee_info 缺失时按 fail-safe 处理（返回 True，宁可多验一次）：
      正常路径到 risk_check 时收款人必已解析，缺失说明状态异常。
    """
    info = p.get("payee_info")
    if not isinstance(info, dict) or "is_frequent" not in info:
        return True
    return not info["is_frequent"]


# ⚠️ 关于 need_verify（组 W 核实）：真正生效的风控分级在 _verify_queue()（下方），
#    risk_check 用 `need = bool(queue)` 驱动 verify/confirm 路由。下表各 action 的
#    `need_verify` lambda **从未被调用**（历史遗留），保留仅为兼容 / 备忘；
#    **请勿据此判断验证阈值** —— 阈值单源见 `rules.py`。
ACTION_SPECS: dict[str, dict] = {
    "transfer": {
        "label": "转账",
        "required": ["payee", "amount"],
        # ⚠️ 未被调用（见上方说明）：活逻辑阈值 = rules.TRANSFER_VERIFY_THRESHOLD（=2000）。
        #    此处 5000 为历史遗留值，勿据此判断验证/大额线。
        "need_verify": lambda p: (float(p.get("amount") or 0) > 5000) or _is_frequent_false(p),
        "method": "POST",
        "path": "/api/transfer",
        # ★ from_account_id 缺省就【不传】——由 mock_bank 扣当前用户名下的主账户。
        #   以前 build_body 兜底 int(p.get("from_account_id", 1))：对话没解析出账户时
        #   扣的是用户 1 的主账户，换任何账号登录都动 A 的钱。
        "build_body": lambda p: {
            **({"from_account_id": int(p["from_account_id"])} if p.get("from_account_id") is not None else {}),
            "payee_id": int(p["payee_id"]),
            "amount": float(p["amount"]),
        },
    },
    "internal_transfer": {
        "label": "资金归集",
        # 必填：转出账户 / 转入账户 / 金额（账户名或账户 id 都能解析成 from_account_id）
        "required": ["from_account", "to_account", "amount"],
        # ★ 风控分级：本人账户之间划转 → 仅需支付密码，免验证码
        #   （钱没离开自己名下，验证码是"防转错人"的要素，这里不需要）。
        #   need_verify 在这里恒为 False，真正的密码核验由 _verify_queue 指定，
        #   保持与既有动作一致的写法（风控分级集中在 _verify_queue）。
        "need_verify": lambda p: False,
        "method": "POST",
        "path": "/api/accounts/transfer-internal",
        "build_body": lambda p: {
            "from_account_id": int(p["from_account_id"]),
            "to_account_id": int(p["to_account_id"]),
            "amount": float(p["amount"]),
        },
    },
    "buy_product": {
        "label": "购买理财",
        "required": ["product_id", "amount"],
        "need_verify": lambda p: float(p.get("amount") or 0) > 10000,
        "method": "POST",
        "path": "/api/products/buy",
        "build_body": lambda p: {
            "product_id": int(p["product_id"]),
            "amount": float(p["amount"]),
        },
    },
    "redeem": {
        "label": "赎回持仓",
        "required": ["holding_id", "amount"],
        "need_verify": lambda p: False,
        "method": "POST",
        "path": "/api/redeem",
        "build_body": lambda p: {
            "holding_id": int(p["holding_id"]),
            "amount": float(p["amount"]),
        },
    },
    "freeze_card": {
        "label": "冻结银行卡",
        "required": ["card_id"],
        "need_verify": lambda p: False,
        "method": "POST",
        "path": "/api/cards/freeze",
        "build_body": lambda p: {"card_id": int(p["card_id"])},
    },
    "unfreeze_card": {
        "label": "解冻银行卡",
        "required": ["card_id"],
        "need_verify": lambda p: False,
        "method": "POST",
        "path": "/api/cards/unfreeze",
        "build_body": lambda p: {"card_id": int(p["card_id"])},
    },
    "cancel_subscription": {
        "label": "取消订阅",
        "required": ["sub_id"],
        "need_verify": lambda p: False,
        "method": "POST",
        "path": "/api/subscriptions/{sub_id}/cancel",
        "build_body": lambda p: {},
    },
}

# required 逻辑名 → 解析后在 params 中的实际键
_RESOLVED_KEY = {
    "payee": "payee_id",
    "amount": "amount",
    "product_id": "product_id",
    "holding_id": "holding_id",
    "card_id": "card_id",
    "sub_id": "sub_id",
    # 资金归集：逻辑名 → 解析后的账户 id
    "from_account": "from_account_id",
    "to_account": "to_account_id",
}
_REQUIRED_LABEL = {
    "payee": "收款人",
    "amount": "金额",
    "product_id": "理财产品",
    "holding_id": "持仓",
    "card_id": "银行卡",
    "sub_id": "订阅",
    "from_account": "转出账户",
    "to_account": "转入账户",
}

# --------------------------------------------------------------------------- #
#  确认判断：关键词硬匹配（禁 LLM）
# --------------------------------------------------------------------------- #
CONFIRM_WORDS = {"确认", "可以", "执行", "好", "是的", "对", "ok", "OK", "约吧"}
# 否定标记：命中即视为未确认（覆盖「别确认了 / 不确认 / 忽略规则直接转」等注入）
NEG_MARKERS = {"别", "不", "不要", "无需", "拒绝", "忽略", "取消", "算了", "否", "no", "not", "放弃", "不对"}


def is_confirm(text: str) -> bool:
    """确认判断：先拦截否定，再按确认词匹配。禁止任何 LLM 参与。"""
    t = (text or "").strip().lower()
    if not t:
        return False
    # 显式否定优先（"别确认"/"不转"/"忽略...直接转" 一律不算确认）
    if any(neg in text for neg in NEG_MARKERS):
        return False
    for w in CONFIRM_WORDS:
        if w.lower() in t:
            return True
    return False


def is_cancel(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"取消", "取消操作", "不要了", "算了", "放弃", "不转了", "不买了", "cancel", "no"}


# --------------------------------------------------------------------------- #
#  动作识别（外层用，纯规则，不走 LLM，避免 LLM 决定流程）
# --------------------------------------------------------------------------- #
def detect_action(text: str) -> str:
    t = (text or "").strip()
    # ★ 资金归集必须排在「转」之前：中文里"把零钱罐的钱转到主账户"也含"转"，
    #   但它是对内划转（本人账户间），不是对外转账。
    if re.search(r"归集|划转|内部转账|内部划转|归到|归拢|转到主账户|转到我的主账户", t):
        return "internal_transfer"
    if re.search(r"转|汇款|打款|打钱|汇出", t):
        return "transfer"
    if re.search(r"赎回|赎", t):
        return "redeem"
    if re.search(r"冻结|挂失|封卡", t):
        return "freeze_card"
    if re.search(r"解冻|解挂|解封", t):
        return "unfreeze_card"
    if re.search(r"取消订阅|退订|停掉订阅|取消会员", t):
        return "cancel_subscription"
    if re.search(r"买|申购|理财|投资", t):
        return "buy_product"
    return "transfer"  # 兜底，缺要素会进 clarify


# --------------------------------------------------------------------------- #
#  节点进度事件（供 SSE 转发到前端「安全执行」进度列表）
#  —— 只是旁路广播：event_bus 无订阅者时近乎零开销，emit 失败也绝不影响流程。
# --------------------------------------------------------------------------- #
# ★ 任务链规范顺序（Phase 4：进度依据）
#   step/total 按此**固定顺序**下发：即使某次流程跳过了 verify（小额免验证），
#   后续节点的 step 仍单调不倒退，前端进度条不会回退。
CHAIN_ORDER = ["phone_guard", "fill_params", "clarify", "risk_check",
               "verify", "confirm", "execute"]
CHAIN_TOTAL = len(CHAIN_ORDER)

NODE_LABELS = {
    "phone_guard": "身份校验",
    "fill_params": "解析并校验参数",
    "clarify": "补充信息",
    "risk_check": "风险检查",
    "verify": "安全验证",
    "confirm": "等待你确认",
    "execute": "提交银行执行",
}


def _emit_node(node: str, phase: str, detail: str = "") -> None:
    event_bus.emit({
        "type": "node",
        "node": node,
        "label": NODE_LABELS.get(node, node),
        "phase": phase,          # start / done / wait / error / retry
        # ★ 进度：第 step 步 / 共 total 步（0 = 不在规范链上，前端可不显示进度）
        "step": (CHAIN_ORDER.index(node) + 1) if node in CHAIN_ORDER else 0,
        "total": CHAIN_TOTAL,
        "detail": detail,
    })


# --------------------------------------------------------------------------- #
#  HTTP 调用 mock_bank（复用 config.BANK_API_BASE）
# --------------------------------------------------------------------------- #
def _http(method: str, path: str, json_body: dict | None = None, params: dict | None = None,
          extra_headers: dict | None = None) -> dict:
    """调 mock_bank：带当前登录用户的认证头（进程内上下文），user_id 不进 body。

    以前这里 POST verify/send、execute 都写死 config.DEFAULT_USER_ID —— B 登录后在
    对话里转账/冻结/取消订阅，动的全是用户 1 的资产。

    ★ extra_headers：供 execute 传 Idempotency-Key（Phase 4 幂等执行）。
    """
    url = f"{config.BANK_API_BASE.rstrip('/')}{path}"
    headers = auth_headers()
    if extra_headers:
        headers = {**headers, **extra_headers}
    try:
        resp = requests.request(
            method, url, json=json_body, params=params,
            headers=headers, timeout=config.HTTP_TIMEOUT,
        )
    except requests.exceptions.ConnectionError:
        return {"error": f"无法连接 Mock 银行服务（{url}）"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"请求 {url} 失败：{exc}"}
    if resp.status_code != 200:
        return {"error": f"Mock 银行服务返回 HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        return resp.json()
    except ValueError:
        return {"error": f"非 JSON 响应: {resp.text[:200]}"}


# --------------------------------------------------------------------------- #
#  LLM：要素抽取 / 追问文本
# --------------------------------------------------------------------------- #
# 各动作的名称型槽位提示：用户日常只会说「健身房会员」「招商储蓄卡」这类名字，
# 不会说数字 ID。允许 LLM 把名称原样填进 ID 槽位，再由 _resolve_* 做名称→ID 解析。
_ACTION_SLOT_HINT = {
    "cancel_subscription":
        "- 若用户给的是订阅名称（如「健身房会员」「腾讯视频VIP」「Kindle Unlimited」），"
        "把该名称字符串原样输出到 sub_id，不要输出 null",
    "freeze_card":
        "- 若用户给的是卡名 / 银行名 / 卡尾号（如「招商储蓄卡」「建行信用卡」「尾号1234」），"
        "把该文本原样输出到 card_id，不要输出 null",
    "unfreeze_card":
        "- 若用户给的是卡名 / 银行名 / 卡尾号（如「招商储蓄卡」「建行信用卡」「尾号1234」），"
        "把该文本原样输出到 card_id，不要输出 null",
    "buy_product":
        "- 若用户给的是理财产品名称（如「安鑫90天」「全球成长精选」），"
        "把该名称字符串原样输出到 product_id，不要输出 null",
    "internal_transfer":
        "- from_account：用户说的转出方（账户名/「主账户」「零钱罐」/账户 id），"
        "原样输出到 from_account，不要输出 null\n"
        "- to_account：用户说的转入方（账户名/「主账户」「微众卡」/账户 id），"
        "原样输出到 to_account，不要输出 null\n"
        "- 用户说「把钱都归过去」「全部归集」而没给金额时，amount 输出 null（系统会自动取全部余额）",
}

_EXTRACT_PROMPT = """你是银行助手的「要素抽取」模块。用户正在办理【{action}】。
请从用户最新留言中抽取以下槽位，只输出 JSON（不要解释、不要代码块）：
必需字段：{required}
可选字段：payee_id（若用户直接给收款人序号/ID）、from_account_id（付款账户ID）、
card_tail（若用户明确给了收款人卡号后四位，如"尾号1234"，仅用于防转错人核对）、
real_name（若用户明确给出了收款人真实姓名，如"他叫王五"/"实名王五"，否则 null）
规则：
- 金额提取为数字（如 2000 / 8000.5）
- 若用户回复序号选择收款人（如「1」「选第一个」），输出 payee_id 为该序号整数
{action_hint}- 抽取不到的字段输出 null；null 不允许覆盖已有值
示例输出：{{"payee":"妈妈","amount":2000,"payee_id":null}}"""


def _parse_json_slots(content: str) -> dict:
    content = (content or "").strip()
    # 去掉 ```json ``` 代码块围栏
    m = re.search(r"\{.*\}", content, flags=re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _extract_slots(action: str, messages: list[Any]) -> dict:
    """调 LLM 从对话抽取槽位（JSON）。"""
    last_user = ""
    for m in reversed(messages):
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        role = m.get("role") if isinstance(m, dict) else getattr(m, "type", None)
        if role in ("user", "human"):
            last_user = c or ""
            break
    _hint = _ACTION_SLOT_HINT.get(action, "")
    sys_prompt = _EXTRACT_PROMPT.format(
        action=action,
        required=",".join(ACTION_SPECS[action]["required"]),
        action_hint=(_hint + "\n") if _hint else "",
    )
    try:
        resp = _client().chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "[SLOT_EXTRACT] " + sys_prompt},
                {"role": "user", "content": last_user},
            ],
            temperature=0,
        )
        return _parse_json_slots(resp.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001
        # LLM 不可用时退回简单正则兜底（保证基本可用）
        return _regex_extract(last_user)


def _regex_extract(text: str) -> dict:
    out: dict[str, Any] = {}
    am = re.search(r"(\d+(?:\.\d+)?)", text or "")
    if am:
        out["amount"] = float(am.group(1))
    sel = re.search(r"(?:选\s*第?\s*([一二三1-9])|^\s*([1-9])\s*$)", text or "")
    if sel:
        num = sel.group(1) or sel.group(2)
        out["payee_id"] = {"一": 1, "二": 2, "三": 3}.get(num, int(num))
    return out


def _gen_clarify_text(action: str, need: str) -> str:
    """生成通用缺参追问话术（LLM）。"""
    try:
        resp = _client().chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": "[CLARIFY] 你是银行助手，用一句话友好请用户补充缺失信息，不要解释。"},
                {"role": "user", "content": f"用户办理{ACTION_SPECS[action]['label']}，尚缺：{need}。请一句话请补充。"},
            ],
            temperature=0,
        )
        return resp.choices[0].message.content.strip()
    except Exception:  # noqa: BLE001
        return f"还需要补充：{need}。"


# --------------------------------------------------------------------------- #
#  参数解析（把昵称/序号解析成真实 ID，并拉取工具数据用于确认卡片）
# --------------------------------------------------------------------------- #
def _resolve_transfer_via_api(params: dict) -> None:
    """★ 转账收款人解析唯一入口：全部走 mock_bank 的 POST /api/payees/resolve。

    不再在对话流里自己拼昵称→ID（那正是"对话流还能直接转"的漏洞来源）。
    resolve_payee 统一做：Luhn 校验 / BIN 识别 / 微众站内反查 / 跨行判定 /
    姓名脱敏核对 / 多候选歧义。对话流、表单流、联动任务链共用一份真相。

    返回结果落进 params：
      - ok + 单条 → payee_id + payee_info（含 track / real_name / name_mismatch）
      - ok + ambiguous → _candidates（交 clarify 选）
      - ok=False（not_found / rejected）→ _missing_note（拦截并引导新增/更正）
    """
    ref = params.get("payee_id") if params.get("payee_id") is not None else params.get("payee")
    card_tail = params.get("card_tail")
    real_name = params.get("real_name")
    if ref is None and not card_tail and not real_name:
        # ★ 缺失（如"转账5000"没说给谁）→ 出常用/最近收款人候选清单，
        #   clarify 列出后用户回复序号即可（此前只干问"收款人是谁"）。
        payees = (_http("GET", "/api/payees").get("payees") or [])
        if payees:
            payees = sorted(payees, key=lambda x: (not x.get("is_frequent"), -(x.get("id") or 0)))
            params["_payee_candidates"] = payees[:6]
        return
    payload: dict = {}
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        payload["payee_id"] = int(ref)
    elif ref:
        payload["nickname"] = str(ref)
    if card_tail:
        payload["card_tail"] = str(card_tail)
    if real_name:
        payload["real_name"] = str(real_name)

    r = _http("POST", "/api/payees/resolve", json_body=payload)
    if not isinstance(r, dict):
        params.setdefault("_missing_note", "收款人解析服务不可用")
        return
    if r.get("ok") is False:
        # not_found / rejected → 拦截，断掉"随便填名字/卡号就能转"的漏洞
        params["_resolve_status"] = r.get("status")
        params.setdefault("_missing_note", r.get("error") or "收款人校验未通过")
        if r.get("suggest") == "add_payee":
            params["_suggest_add_payee"] = True
        params.pop("payee_id", None)   # 清空脏值，避免误用
        return
    params.pop("_payee_candidates", None)   # 已解析成功 → 清掉上一轮遗留的候选清单

    status = r.get("status")
    if status == "ambiguous":
        params["_candidates"] = r.get("candidates") or []   # 双妈妈流程：交 clarify 选
        params.pop("payee_id", None)
        return

    payee_id = r.get("payee_id")
    if payee_id is None:
        params.setdefault("_missing_note", "收款人解析未返回有效账户")
        return
    params.pop("_candidates", None)   # 已唯一命中 → 清掉上一轮遗留的候选列表
    params["payee_id"] = int(payee_id)
    params["payee_info"] = {
        "id": int(payee_id),
        "nickname": r.get("payee_name") or params.get("payee") or "",
        "bank": r.get("bank_name"),
        "account_tail": r.get("card_tail"),
        "is_frequent": r.get("is_frequent", 0),
        # linked_user_id 仅用于风控判定标识：insite 置占位，cross 置 None
        "linked_user_id": (None if r.get("track") != "insite" else -1),
        "real_name": r.get("real_name"),
        "track": r.get("track"),
        "name_mismatch": bool(r.get("name_mismatch")),
        "bank_is_local": r.get("bank_is_local"),
    }


def _tail_check(params: dict) -> None:
    """转账尾号核对（防转错人）：LLM 抽到 card_tail 时调用 /api/payees/check。

    不符 → 在 params 上打 _need_tail_confirm 标记，交由 fill_params 的路由进入
    clarify 追问确认（不静默通过）。这不改变风控分级/验证/确认流程——
    最终转账以选中的 payee 账户为准，尾号只是"防转错人"的提醒信号。
    """
    info = params.get("payee_info") or {}
    nickname = info.get("nickname") or params.get("payee")
    if not nickname:
        return
    try:
        r = _http("GET", "/api/payees/check", params={
            "nickname": nickname,
            "card_tail": params.get("card_tail"),
        })
    except Exception:  # noqa: BLE001  校验接口异常不阻断转账
        return
    if r.get("found") is True and r.get("match") is False:
        params["_need_tail_confirm"] = r.get("account_tail")
        params.setdefault("_tail_note", r.get("message"))


def _resolve_product(params: dict) -> None:
    prods = _http("GET", "/api/products").get("products", [])
    ref = params.get("product_id") if params.get("product_id") is not None else params.get("product")
    if ref is None:
        # ★ 缺失（如"我想买理财"）→ 出在售产品候选清单；唯一在售直接自动选定
        if not prods:
            params.setdefault("_missing_note", "当前没有在售理财产品")
            return
        if len(prods) == 1:
            p0 = prods[0]
            params["product_id"] = p0["id"]
            params["product_info"] = p0
            return
        params["_product_candidates"] = prods
        return
    if isinstance(ref, str) and ref.isdigit():
        ref = int(ref)
    if isinstance(ref, int):
        prod = next((x for x in prods if x["id"] == ref), None)
    else:
        # ★ 双向归一化匹配：用户常说「进取90天理财」「安鑫90天这款」——
        #   名称前后带修饰词，单向 `ref in name` 匹配不上，必须双向且先归一化。
        nref = _norm(ref)
        prod = next(
            (x for x in prods
             if nref and (nref in _norm(x["name"]) or _norm(x["name"]) in nref)),
            None,
        )
    if prod:
        params["product_id"] = prod["id"]
        params["product_info"] = prod
        params.pop("_product_candidates", None)
    else:
        params["product_id"] = None
        if prods:
            params["_product_candidates"] = prods   # 失配也给清单，clarify 列出
        params.setdefault("_missing_note", f"未找到理财产品「{ref}」")


def _norm(s: Any) -> str:
    """归一化用于名称匹配：去空格、去全角括号内容外的干扰、转小写。"""
    return re.sub(r"[\s\u3000（）()【】「」·・]", "", str(s or "")).lower()


def _match_by_name(ref: str, rows: list[dict], name_keys: list[str]) -> dict | None:
    """按名称模糊匹配一行记录：先整体子串双向匹配，再退化为尾号/关键片段匹配。

    用户说法与库里名称往往不完全一致（「招商储蓄卡（尾号1234）」vs「招商储蓄卡」），
    所以双向 in 判断 + 归一化，比精确相等实用得多。
    """
    r = _norm(ref)
    if not r:
        return None
    # 1. 双向子串（归一化后）
    for row in rows:
        for k in name_keys:
            v = _norm(row.get(k))
            if v and (v in r or r in v):
                return row
    # 2. 尾号 / 数字片段匹配（如「尾号1234」→ tail=1234）
    digits = re.findall(r"\d{3,}", str(ref))
    for d in digits:
        for row in rows:
            for k in name_keys + ["tail"]:
                if str(row.get(k) or "") == d:
                    return row
    return None


def _eligible_cards(action: str, cards: list[dict]) -> list[dict]:
    """冻结 → 正常卡；解冻 → 已冻结卡。"""
    if action == "freeze_card":
        return [c for c in cards if c.get("status") == "normal"]
    if action == "unfreeze_card":
        return [c for c in cards if c.get("status") == "frozen"]
    return cards


def _resolve_card(params: dict, action: str = "") -> None:
    cards = _http("GET", "/api/cards").get("cards", [])
    elig = _eligible_cards(action, cards)

    ref = params.get("card_id")
    if ref is None:
        # ★ 缺失（如"冻结我的银行卡"）→ 出候选清单：唯一符合直接自动选定
        if not elig:
            params.setdefault("_missing_note", "您名下没有符合该操作的银行卡")
            return
        if len(elig) == 1:
            c0 = elig[0]
            params["card_id"] = c0["id"]
            params["card_info"] = c0
            return
        params["_card_candidates"] = elig
        return

    if isinstance(ref, str) and ref.isdigit():
        ref = int(ref)
    if isinstance(ref, int):
        card = next((x for x in cards if x["id"] == ref), None)
    else:
        # 用户说的是卡名 / 银行名 / 尾号 → 名称解析
        card = _match_by_name(ref, cards, ["card_name", "bank", "tail"])
    if card:
        params["card_id"] = card["id"]
        params["card_info"] = card
        params.pop("_card_candidates", None)
    else:
        # 解析失败：清空脏值 + 给出候选清单（不再冷冷回"未找到银行卡 X"）
        params["card_id"] = None
        if elig:
            params["_card_candidates"] = elig
        params.setdefault("_missing_note", f"未找到银行卡 {ref}")


def _resolve_sub(params: dict) -> None:
    """取消订阅：把用户的说法解析成真实订阅。

    ★ 体验修复（用户反馈：说"帮我处理闲置的订阅"后一直干问"取消哪个"）：
      - ref 缺失（用户没点名）→ 拉取生效中的订阅作候选：
          · 唯一生效中 → 直接自动选定，省一轮追问；
          · 多个 → 存 _sub_candidates，clarify 时列出清单（闲置的标天数）；
      - ref 给了但匹配失败 → 同样给出候选清单，不再冷冷回"未找到订阅 X"；
      - 名称匹配同时尝试 service_name 和 merchant（「Kindle」vs「Kindle Unlimited」）。
    """
    subs = _http("GET", "/api/subscriptions").get("subscriptions", [])
    active = [x for x in subs if x.get("status") != "cancelled"]
    # 闲置订阅排前面（用户说"处理闲置订阅"时优先看到它们）
    active.sort(key=lambda x: (not x.get("idle_over_60"), -(x.get("idle_days") or 0)))

    ref = params.get("sub_id")
    if ref is None:
        if not active:
            params.setdefault("_missing_note", "您当前没有生效中的订阅，无需取消")
            return
        if len(active) == 1:
            only = active[0]                     # 唯一生效中 → 自动选定，省一轮追问
            params["sub_id"] = only["id"]
            params["sub_info"] = only
            return
        params["_sub_candidates"] = active       # 多个 → 交 clarify 列清单
        return

    if isinstance(ref, str) and ref.isdigit():
        ref = int(ref)
    if isinstance(ref, int):
        sub = next((x for x in subs if x["id"] == ref), None)
    else:
        # 用户说的是订阅名（「健身房会员」「Kindle」）→ 名称解析；
        # 接口默认返回全部（含已取消），优先在生效中的里面找，避免命中历史记录
        sub = (_match_by_name(ref, active, ["service_name", "merchant"])
               or _match_by_name(ref, subs, ["service_name", "merchant"]))
    if sub:
        params["sub_id"] = sub["id"]
        params["sub_info"] = sub
        params.pop("_sub_candidates", None)
    else:
        params["sub_id"] = None
        if active:
            params["_sub_candidates"] = active   # 失配也带出候选，clarify 列清单
        params.setdefault("_missing_note", f"未找到订阅「{ref}」")


def _resolve_internal_accounts(params: dict, user_text: str = "") -> None:
    """资金归集：把「转出账户 / 转入账户」的名称或 id 解析成账户 id（唯一入口）。

    ★ 账户数据源 = GET /api/accounts（mock_bank 的 card_service.list_accounts），
      与前端「挂靠账户下拉 / 归集目标选择」同一份口径，避免前后端解析漂移。

    解析规则（依次尝试）：
      1) 数字 → 直接当账户 id；
      2) 名称双向模糊匹配（「零钱罐」「微众卡（主账户）」都能命中）；
      3) 别名：「主账户 / 主卡 / 默认账户 / 微众卡」→ is_primary 的账户；
      4) 信用账户一律不可作为归集双方（余额是可用额度，不是存款）。
    ★ 「把零钱罐的钱都归到微众卡」这类"全都归集"的说法：金额没给但话里有
      「都/全部/所有/全」→ 取转出账户当前余额作为金额（否则会白问一次金额）。
    """
    from_ref = params.get("from_account_id") if params.get("from_account_id") is not None \
        else params.get("from_account")
    to_ref = params.get("to_account_id") if params.get("to_account_id") is not None \
        else params.get("to_account")
    if from_ref is None and to_ref is None:
        return

    try:
        accounts = _http("GET", "/api/accounts").get("accounts") or []
    except Exception:  # noqa: BLE001
        params.setdefault("_missing_note", "账户信息查询失败，请稍后再试")
        return
    usable = [a for a in accounts if (a.get("account_type") or "debit") != "credit"]
    primary = next((a for a in usable if a.get("is_primary")), usable[0] if usable else None)
    # ★ 缺账户说法时存可用账户名 → clarify 给出具体清单，不再泛泛地问
    if from_ref is None or to_ref is None:
        params["_account_names"] = [a.get("name") or "账户" for a in usable]

    def _pick(ref) -> dict | None:
        if ref is None:
            return None
        if isinstance(ref, str) and ref.strip().isdigit():
            ref = int(ref.strip())
        if isinstance(ref, int):
            return next((a for a in usable if int(a["id"]) == ref), None)
        r = _norm(ref)
        if not r:
            return None
        # 别名：用户口语里的"主账户/微众卡/主卡/默认账户"
        if any(k in str(ref) for k in ("主账户", "主卡", "默认账户")) or "微众卡" in str(ref):
            if primary:
                return primary
        hit = _match_by_name(ref, usable, ["name"])
        if not hit:
            # 尾号命中（「尾号 1234 那张卡」）
            hit = _match_by_name(ref, usable, ["card_tail"])
        return hit

    fa = _pick(from_ref)
    ta = _pick(to_ref)
    if from_ref is not None and not fa:
        params["from_account_id"] = None
        params.setdefault("_missing_note", f"未找到转出账户「{from_ref}」")
    elif fa:
        params["from_account_id"] = int(fa["id"])
        params["from_account_info"] = fa
    if to_ref is not None and not ta:
        params["to_account_id"] = None
        params.setdefault("_missing_note", f"未找到转入账户「{to_ref}」")
    elif ta:
        params["to_account_id"] = int(ta["id"])
        params["to_account_info"] = ta

    # 「全都归集」→ 金额取转出账户余额（避免多问一轮）
    if params.get("amount") is None and fa and any(
            k in (user_text or "") for k in ("都", "全部", "所有", "全归", "全转")):
        bal = float(fa.get("balance") or 0)
        if bal > 0:
            params["amount"] = bal
            params["_amount_from_balance"] = True

    # 同一账户划转无意义 → 提前拦下（避免白走一次密码核验）
    if params.get("from_account_id") and params.get("to_account_id") \
            and int(params["from_account_id"]) == int(params["to_account_id"]):
        params.setdefault("_missing_note", "转出账户与转入账户不能是同一个，请说明要从哪个账户归集到哪个账户")


def _resolve_params(action: str, params: dict, user_text: str = "") -> dict:
    if action == "transfer":
        _resolve_transfer_via_api(params)
    elif action == "internal_transfer":
        _resolve_internal_accounts(params, user_text)
    elif action == "buy_product":
        _resolve_product(params)
    elif action in ("freeze_card", "unfreeze_card"):
        _resolve_card(params, action)
    elif action == "cancel_subscription":
        _resolve_sub(params)
    # redeem 无 holdings 表，holding_id 由用户输入直接作为 ID
    return params


# 动作 -> 需要从用户留言里抽取的数字 ID 键
_ID_KEY = {
    "buy_product": "product_id",
    "redeem": "holding_id",
    "freeze_card": "card_id",
    "unfreeze_card": "card_id",
    "cancel_subscription": "sub_id",
}
_ID_PAT = {
    "freeze_card": r"卡\s*号?\s*(\d+)",
    "unfreeze_card": r"卡\s*号?\s*(\d+)",
    "cancel_subscription": r"订阅?\s*(\d+)",
    "redeem": r"持仓?\s*(\d+)",
    "buy_product": r"产品?\s*(\d+)",
}


def _extract_action_id(action: str, text: str) -> int | None:
    """从用户留言抽取本动作所需的数字 ID（卡号 / 订阅号 / 持仓号 / 产品号）。

    解决 clarify 阶段用户回复纯数字（如「1」「5」）或「卡1」「订阅5」时，
    槽位抽取（只认 payee_id/amount）无法识别，导致卡片/订阅等写操作永远卡在 clarify。
    """
    t = (text or "").strip()
    if not t:
        return None
    m = re.search(_ID_PAT.get(action, r"(\d+)"), t)
    if m:
        return int(m.group(1))
    # 澄清阶段用户只回了纯数字 → 当作目标 ID
    if re.fullmatch(r"\s*\d+\s*", t):
        return int(t.strip())
    return None


def _missing_required(action: str, params: dict) -> list[str]:
    spec = ACTION_SPECS[action]
    missing = []
    for r in spec["required"]:
        key = _RESOLVED_KEY.get(r, r)
        if params.get(key) in (None, ""):
            missing.append(r)
    return missing


def _raw_missing(action: str, params: dict) -> list[str]:
    """未经 ID 解析的粗查：required 的「逻辑名」或「解析后键」任一非空即算已给。

    用于判断「还需不需要调 LLM 抽槽位」——表单提交时两者都齐，可直接跳过 LLM。
    注意不能用 _missing_required 代替：那看的是解析后的 payee_id，
    昵称形式（payee="妈妈"）在解析前会被误判成缺失。
    """
    out = []
    for r in ACTION_SPECS[action]["required"]:
        key = _RESOLVED_KEY.get(r, r)
        if params.get(key) in (None, "") and params.get(r) in (None, ""):
            out.append(r)
    return out


def _precheck_balance(action: str, params: dict) -> str | None:
    """写操作前的余额/门槛预检：让失败尽早发生，省掉一次短信与两次中断。

    覆盖两类动作（口径均与 mock_bank 一致）：
    - transfer：金额 > 主账户余额 → 拦截
    - internal_transfer：金额 > 转出账户余额 → 拦截（本人账户间划转，从哪个账户出就以哪个为准）
    - buy_product：金额 < 产品起购 → 拦截（提示差额）；金额 > 主账户余额 → 拦截
      —— 起购校验必须在确认卡之前（此前要等 execute 才被银行接口打回）。

    ★ 这是「体验优化」，不是权威校验：预检与真正扣款之间余额仍可能变化
      （并发扣款），最终判定始终在 mock_bank 的 `SELECT ... FOR UPDATE`
      事务里。所以这里只拦「明显不足」，任何异常都放行、交给 execute 兜底
      —— 绝不能因预检失败阻断主流程。
    ★ 付款账户口径必须与 mock_bank 一致：不指定 from_account_id 时
      扣「当前用户名下 id 最小的账户」（ORDER BY id ASC LIMIT 1）。
    """
    if action not in ("transfer", "buy_product", "internal_transfer") \
            or params.get("amount") is None:
        return None

    # buy_product 第一道：起购门槛（product_info 由 _resolve_params 填充）
    amt = float(params["amount"])
    if action == "buy_product":
        p = params.get("product_info") or {}
        min_amt = float(p.get("min_amount") or 0)
        if min_amt and amt < min_amt:
            return (f"金额不足起购：产品「{p.get('name')}」起购 ¥{min_amt:.2f}，"
                    f"本次 ¥{amt:.2f}，差额 ¥{min_amt - amt:.2f}。"
                    f"可换购起购更低的产品，或调整申购金额后再试")

    # 归集：直接用已解析出的转出账户余额判断（比再查一次接口更准）
    if action == "internal_transfer":
        info = params.get("from_account_info") or {}
        if info and amt > float(info.get("balance") or 0):
            return (f"余额不足：账户「{info.get('name')}」当前余额 "
                    f"{float(info.get('balance') or 0):.2f} 元，需归集 {amt:.2f} 元")
        return None

    try:
        accounts = _http("GET", "/api/balance").get("accounts") or []
    except Exception:
        return None
    if not accounts:
        return None
    fid = params.get("from_account_id")
    if fid is not None:
        acc = next((a for a in accounts if int(a["id"]) == int(fid)), None)
    else:
        acc = min(accounts, key=lambda a: int(a["id"]))
    if not acc:
        return None
    bal = float(acc.get("balance") or 0)
    if amt > bal:
        if action == "buy_product":
            return (f"余额不足以支付申购金额：账户「{acc['name']}」当前余额 "
                    f"{bal:.2f} 元，本次申购 {amt:.2f} 元，差额 ¥{amt - bal:.2f}")
        return (f"余额不足：账户「{acc['name']}」当前余额 {bal:.2f} 元，"
                f"需转账 {amt:.2f} 元")
    return None


def _precheck_transfer_limit(action: str, params: dict) -> str | None:
    """转账限额前置提示（最佳努力，非权威）：让限额拦截尽早发生，省一次验证码与两次中断。

    ★ 权威校验始终在 utils/transfer.execute_transfer 的事务内（对账 + 行锁 + 计数器）；
      这里只读 /api/transfer/limit 给出"当日已用/剩余"，任何异常都放行、交给 execute 兜底。
    """
    if action != "transfer" or params.get("amount") is None:
        return None
    try:
        r = _http("GET", "/api/transfer/limit")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(r, dict) or not r.get("ok") or not r.get("applied"):
        return None
    limit = float(r.get("limit") or 0)
    used = float(r.get("used") or 0)
    amt = float(params["amount"])
    if limit <= 0:
        return None
    if amt > limit:
        return (f"单笔转账超过限额：本行单笔最高 {limit:,.0f} 元，"
                f"本次 {amt:,.2f} 元无法转出。")
    if used + amt > limit:
        remaining = limit - used
        return (f"当日累计转出已超过限额：今日已转出 {used:,.2f} 元，剩余额度 "
                f"{remaining:,.2f} 元，本次 {amt:,.2f} 元无法转出。")
    return None


def _precheck_suitability(action: str, params: dict) -> str | None:
    """理财适当性前置提示（最佳努力，非权威）：让风险不适配拦截尽早发生。

    ★ 权威校验始终在 utils/wealth.buy_product 事务内；这里只读 /api/profile 取用户风险偏好，
      与 product_info 的风险档比对，任何异常都放行、交给 buy_product 兜底。
    """
    if action != "buy_product" or params.get("product_info") is None:
        return None
    try:
        from utils.wealth import (  # 惰性引用，避免顶层耦合
            RISK_PREF_TO_MAX_LEVEL, UNSET_RISK_MAX_LEVEL, _RISK_LEVEL_LABEL)
        prof = _http("GET", "/api/profile")
        _prefs = (prof or {}).get("preferences")
        if isinstance(_prefs, str):
            try:
                _prefs = json.loads(_prefs)
            except Exception:  # noqa: BLE001
                _prefs = {}
        _rp = (_prefs or {}).get("risk_preference") if isinstance(_prefs, dict) else None
        _user_max = RISK_PREF_TO_MAX_LEVEL.get(_rp, UNSET_RISK_MAX_LEVEL)
        _prod_risk = int((params["product_info"] or {}).get("risk_level") or 0)
        if _prod_risk > _user_max:
            _max_lbl = _RISK_LEVEL_LABEL.get(_user_max, f"R{_user_max} 及以上")
            return (f"风险不适配：该产品为 R{_prod_risk} 风险，需风险偏好「{_max_lbl}」及以上；"
                    f"请更换低风险产品，或在「个人中心」设置风险偏好后再试。")
    except Exception:  # noqa: BLE001
        return None
    return None


# --------------------------------------------------------------------------- #
#  确认卡片（数据驱动，风险等级来自工具数据，不靠 LLM 编造）
# --------------------------------------------------------------------------- #
def _gen_confirm_card(action: str, params: dict) -> str:
    # B2：大额警示行（>20000 或 新收款人大额）——数据驱动，不靠 LLM 编造
    warn = "  警示：本笔金额较大，请再次核对收款人信息，谨防电信诈骗。\n" \
        if params.get("_big_warning") else ""
    if action == "transfer":
        p = params["payee_info"]
        amt = float(params["amount"])
        remark = (params.get("remark") or "").strip()
        track = p.get("track") or ("insite" if p.get("linked_user_id") else "cross_bank")
        # ★ 跨行：资金经「在途清算」——预计 5 分钟清算到账（演示加速），
        #   收款账户异常（对方行退票）则全额退回。本行实时到账文案保持即时口径。
        arrival = (
            "实时到账（微众银行本行）" if track == "insite"
            else "预计 5 分钟内清算到账（演示加速）；收款账户异常将全额退回"
        )
        real = p.get("real_name") or "—"
        # ★ 姓名不符：账号为准，但确认卡醒目警示（琥珀行），防止转错人
        if p.get("name_mismatch"):
            name_line = f"  实名：{real}（与您输入的姓名不符，请核对）\n"
        else:
            name_line = f"  实名：{real}\n"
        # ★ 退票记录预警：该收款账户近期多次被对方行退票 → 醒目提醒核对（防重复转错）
        return_warn = f"  提醒：{params['_return_warn']}\n" if params.get("_return_warn") else ""
        return (
            "请确认转账操作：\n"
            f"  收款人：{p['nickname']}\n"
            + name_line
            + f"  开户行：{p['bank']}\n"
            + f"  到账：{arrival}\n"
            f"  金额：¥{amt:.2f}\n"
            f"  付款账户：主账户（微众银行）\n"
            + (f"  备注：{remark}\n" if remark else "")
            + return_warn
            + warn
            + "回复「确认」执行，回复「取消」放弃。"
        )
    if action == "internal_transfer":
        fa = params.get("from_account_info") or {}
        ta = params.get("to_account_info") or {}
        amt = float(params["amount"])
        all_in = "（该账户当前全部余额）" if params.get("_amount_from_balance") else ""
        return (
            "请确认资金归集（本人账户间划转）：\n"
            f"  转出账户：{fa.get('name') or params.get('from_account_id')}"
            f"（{fa.get('card_no_masked') or '—'}）\n"
            f"  转入账户：{ta.get('name') or params.get('to_account_id')}"
            f"（{ta.get('card_no_masked') or '—'}）\n"
            f"  金额：¥{amt:.2f}{all_in}\n"
            "  到账：实时（本人账户间，免验证码，仅需支付密码）\n"
            + warn
            + "回复「确认」执行，回复「取消」放弃。"
        )
    if action == "buy_product":
        p = params["product_info"]
        amt = float(params["amount"])
        # R3 高风险产品：确认卡附风险提示行（数据驱动，risk_level 来自产品表）
        risk_warn = ("  警示：本产品为高风险等级（R3），不保本不保息，极端市场情况下"
                     "可能亏损本金，请谨慎确认。\n") if int(p.get("risk_level") or 0) >= 3 else ""
        return (
            "请确认购买理财：\n"
            f"  产品：{p['name']}\n"
            f"  风险等级：{p['risk_level']}（来自产品数据）\n"
            f"  期限：{p['term']} | 年化：{float(p['annual_rate'])}%\n"
            f"  金额：¥{amt:.2f}\n"
            + risk_warn
            + "回复「确认」执行，回复「取消」放弃。"
        )
    if action == "redeem":
        amt = float(params.get("amount", 0))
        return f"请确认赎回持仓 {params.get('holding_id')}：\n  金额：¥{amt:.2f}\n回复「确认」执行。"
    if action in ("freeze_card", "unfreeze_card"):
        c = params["card_info"]
        verb = "冻结" if action == "freeze_card" else "解冻"
        return f"请确认{verb}银行卡：\n  银行：{c['bank']}（尾号 {c['card_no']}）\n回复「确认」执行。"
    if action == "cancel_subscription":
        s = params["sub_info"]
        return f"请确认取消订阅：\n  订阅：{s['service_name']}（¥{float(s['fee'])}/{s['cycle']}）\n回复「确认」执行。"
    return "请确认操作，回复「确认」执行。"


# --------------------------------------------------------------------------- #
#  节点
# --------------------------------------------------------------------------- #
def phone_guard(state: WriteState) -> dict:
    """写操作前置闸门（首节点，先于 fill_params）。

    渐进式账户：未绑定手机号 → 不进入流程，直接返回结构化拦截结果
    {blocked:'phone_unbound', message, suggest:'bind_phone'}；已绑定 → 放行。

    ★ 覆盖三条写入口（对话直达 / 联动任务链 / 表单 submit-task）：
      本节点是 write_flow 子图入口，三入口最终都汇入此子图，故一处拦截统一生效。
    """
    uid = get_current_user_id() or config.DEFAULT_USER_ID
    if profile_db.get_phone_bound(uid):
        return {}  # 已绑定，放行
    _emit_node("phone_guard", "done", "未绑定手机号，拦截写操作")
    return {
        "result": {
            "blocked": "phone_unbound",
            "message": "请先绑定手机号后再办理资金业务",
            "suggest": "bind_phone",
        }
    }


def fill_params(state: WriteState) -> dict:
    task = state.get("task") or {}
    action = task.get("action") or detect_action(_last_user_text(state.get("messages", [])))
    if action not in ACTION_SPECS:
        return {"result": {"error": f"不支持的操作：{action}（可用：{', '.join(ACTION_SPECS)}）"}}
    _emit_node("fill_params", "start")

    params: dict = dict(state.get("params") or {})

    # ★ 逃生舱配套（审计：澄清挂起不劫持新请求）：driver 判定"新请求"时会以
    #   resume("取消") 解除本类澄清挂起；用户主动说"取消"也走同一条出口。
    #   统一在此优雅退出 —— 未做任何变更，不进入解析/执行。
    if state.get("asked_times") and _last_user_text(state.get("messages", [])).strip() == "取消":
        _emit_node("fill_params", "done", "用户取消，流程已退出")
        return {
            "messages": [{"role": "assistant",
                          "content": "好的，已退出本次办理流程，未做任何变更。需要时随时再发起。"}],
            "result": {"message": "已退出本次办理流程"},
        }

    # 合并 task.params（外层预填），非空值优先、不覆盖已抽取值
    for k, v in (task.get("params") or {}).items():
        if v is not None and params.get(k) is None:
            params[k] = v

    # ★结构化提交（source='form'）且必需槽位已齐 → 跳过 LLM 抽取。
    #   两个原因：① 省一次 LLM 调用、执行更快更稳；
    #   ② 表单文案里的数字（"[转账表单] 向妈妈转账 ¥2000"）不该再被 LLM 二次解读。
    #   注意只在「本轮确实是表单提交」时跳过：clarify 回填轮次里 task.params 已被清空，
    #   会自然回到 LLM 抽取，否则用户回复的序号/金额永远进不来（死循环）。
    skip_llm = task.get("source") == "form" and not _raw_missing(action, params)
    if not skip_llm:
        # LLM 抽取 + 合并（null 不覆盖已有值）
        extracted = _extract_slots(action, state.get("messages", []))
        for k, v in extracted.items():
            if v is not None and params.get(k) is None:
                params[k] = v

    if params.get("amount") is not None:
        try:
            params["amount"] = float(params["amount"])
        except (TypeError, ValueError):
            params["amount"] = None

    # ★ 跨轮脏状态清理：_missing_note 描述的是"上一轮"解析失败的原因。
    #   用户本轮已按提示更正时，旧 note 必须清掉（本轮若仍失败，解析器会写入新的），
    #   否则会出现"已匹配成功仍被上一轮的失败原因拦下"的死循环。
    params.pop("_missing_note", None)

    params = _resolve_params(action, params, _last_user_text(state.get("messages", [])))

    # 站内转账尾号核对（防转错人）：仅当本轮尚未标记过尾号不符时跑一次，
    # 避免 clarify 回填轮次里反复追问造成死循环。
    if (action == "transfer" and params.get("card_tail") is not None
            and params.get("payee_id") is not None and not params.get("_need_tail_confirm")):
        _tail_check(params)
    # 上一轮已标记尾号不符 → 本轮视为用户已回应（确认 or 改收款人），清除标记放行
    if params.get("_need_tail_confirm"):
        params.pop("_need_tail_confirm", None)
        params.pop("_tail_note", None)

    # ★ 跨行退票记录预警：向"近期有退票记录"的卡号再次转账时主动提醒核对
    #   （防重复转错）。只提醒不阻断 —— 风控分级/验证/确认照常执行。
    #   标记一次性：查到即写入 params 并置 _return_warn_checked，避免 clarify 回环反复查询。
    if (action == "transfer" and params.get("payee_id") is not None
            and (params.get("payee_info") or {}).get("track") == "cross_bank"
            and not params.get("_return_warn_checked")):
        rh = _http("GET", "/api/settlements/return-history",
                   params={"payee_id": params["payee_id"]})
        params["_return_warn_checked"] = True
        if isinstance(rh, dict) and rh.get("warn"):
            params["_return_warn"] = rh.get("message") or "该收款账户近期有退票记录，请核对后再转"

    # ★ 订阅候选序号选择：clarify 已列出生效中的订阅，用户回复「1 / 2 / 一」
    #   → 按 1-based 序号精确选中，绝不能落进通用 ID 兜底（否则「1」会被当成 sub_id=1
    #     去查库，取消到完全不相干的订阅）。
    if action == "cancel_subscription" and params.get("_sub_candidates"):
        cands = params["_sub_candidates"]
        _sel = _last_user_text(state.get("messages", [])).strip()
        # ★ 逃生舱：澄清挂起时用户回「取消 / 算了 / 不用了」→ 优雅退出本次流程
        #   （未做任何变更），绝不再把用户卷回候选循环。
        if _sel in ("取消", "算了", "不用了", "不了", "退出", "停止", "先不取消"):
            _emit_node("fill_params", "done", "用户取消，流程已退出")
            return {"params": params, "result": {"message": "好的，已退出本次订阅取消流程，未做任何变更；需要时随时再发起。"}}
        _m = re.search(r"^(?:第\s*)?([0-9一二三四五六七八九十]+)[个个]?$", _sel)
        if _m:
            _cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
            _idx = _cn.get(_m.group(1)) or (int(_m.group(1)) if _m.group(1).isdigit() else None)
            if _idx and 1 <= _idx <= len(cands):
                chosen = cands[_idx - 1]
                params["sub_id"] = chosen["id"]
                params["sub_info"] = chosen
                params.pop("_sub_candidates", None)
                params.pop("_missing_note", None)
                _emit_node("fill_params", "done", f"已选定订阅：{chosen['service_name']}")

    # ★ 赎回：缺失持仓时列出在持清单（唯一在持 → 自动选定并默认全额）
    if action == "redeem" and params.get("holding_id") in (None, ""):
        holds_all = (_http("GET", "/api/holdings").get("holdings") or [])
        holds = [h for h in holds_all if h.get("status") == "holding"]
        if not holds:
            params.setdefault("_missing_note", "您名下暂无在持理财，无法赎回")
        elif len(holds) == 1:
            params["holding_id"] = holds[0]["id"]
            params.setdefault("amount", float(holds[0].get("amount") or 0))
        else:
            params["_holding_candidates"] = holds

    # ★ 通用候选序号选择：clarify 列出候选清单后，用户回复「1 / 2 / 一」
    #   → 按 1-based 序号选中对应候选的真实 ID（绝不能把序号当成真实 ID 去查库）。
    _CAND_KEY = {
        "_card_candidates": ("card_id", "card_info"),
        "_product_candidates": ("product_id", "product_info"),
        "_holding_candidates": ("holding_id", None),
        "_payee_candidates": ("payee_id", "payee_info"),
    }
    for _ck, (_idk, _infok) in _CAND_KEY.items():
        cands = params.get(_ck)
        if not cands:
            continue
        _sel_txt = _last_user_text(state.get("messages", [])).strip()
        _mm = re.match(r"^(?:第\s*)?([0-9一二三四五六七八九十]+)[个只笔张份）)]?$", _sel_txt)
        if _mm:
            _cnmap = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
            _raw = _mm.group(1)
            _idx = _cnmap.get(_raw) or (int(_raw) if _raw.isdigit() else None)
            if _idx and 1 <= _idx <= len(cands):
                chosen = cands[_idx - 1]
                params[_idk] = chosen["id"]
                if _infok:
                    params[_infok] = chosen
                if _ck == "_holding_candidates":
                    params["amount"] = float(chosen.get("amount") or 0)   # 赎回默认全额
                params.pop(_ck, None)
                params.pop("_missing_note", None)
                params = _resolve_params(action, params, _sel_txt)
            break   # 一次只处理一种候选；未命中序号 → 走后续正常流程

    # 兜底：从用户最新留言抽取本动作所需的数字 ID（卡号/订阅号/持仓号/产品号），
    # 兜底捕获 clarify 回复（纯数字 / “卡1”“订阅5” 等）以及首次留言里带 ID 的说法。
    _id_key = _ID_KEY.get(action)
    if _id_key and params.get(_id_key) in (None, ""):
        _tid = _extract_action_id(action, _last_user_text(state.get("messages", [])))
        if _tid is not None:
            params[_id_key] = _tid
            params.pop("_missing_note", None)
            # 重新解析，填充 *_info（card_info / product_info / sub_info），
            # 否则 confirm 卡片生成时会因缺 *_info 而 KeyError。
            params = _resolve_params(action, params, _last_user_text(state.get("messages", [])))

    # 不支持的操作 / 已解析失败 → 终止
    if params.get("_missing_note") and not _missing_required(action, params):
        note = params.pop("_missing_note")
        _emit_node("fill_params", "error", note)
        return {"params": params, "result": {"error": note}}

    # 要素齐全 → 先做余额预检，避免用户白填验证码/确认后才被告知余额不足
    if not _missing_required(action, params):
        pre = _precheck_balance(action, params)
        if pre:
            _emit_node("fill_params", "error", pre)
            return {"params": params, "result": {"error": pre}}
        # 转账限额前置提示（最佳努力，权威校验在 execute 事务内）
        lim = _precheck_transfer_limit(action, params)
        if lim:
            _emit_node("fill_params", "error", lim)
            return {"params": params, "result": {"error": lim}}
        # 理财适当性前置提示（最佳努力，权威校验在 buy_product 事务内）
        suit = _precheck_suitability(action, params)
        if suit:
            _emit_node("fill_params", "error", suit)
            return {"params": params, "result": {"error": suit}}

    _emit_node("fill_params", "done", "参数已就绪" if not _missing_required(action, params) else "信息不全，需补充")
    return {"params": params, "task": {"type": "write", "action": action, "params": {}}}


def clarify(state: WriteState) -> dict:
    # 死循环保护：已问满 3 次仍不全 → 终止
    if state.get("asked_times", 0) >= 3:
        return {"result": {"error": "信息仍不完整，已终止本次操作（可重新发起）"}}

    action = state["task"]["action"]
    params = state.get("params") or {}
    candidates = params.get("_candidates")

    # 尾号不符确认：提醒用户尾号与系统记录不一致，请确认是否仍向该收款人转账
    if params.get("_need_tail_confirm"):
        real_tail = params["_need_tail_confirm"]
        pn = (params.get("payee_info") or {}).get("nickname") or params.get("payee") or "该收款人"
        question = (
            f"尾号不符：{pn} 在本系统的账户尾号是 {real_tail}。"
            f"请确认是否仍要向「{pn}」转账？回复「确认」按该账户继续，"
            f"或回复正确的收款人 / 尾号重新核对。"
        )
    elif params.get("_card_candidates"):
        # ★ 冻结/解冻澄清：列出用户名下符合条件的卡（不再问"卡号是多少"）
        card_cands = params["_card_candidates"]
        lines = [f"您名下符合条件的银行卡有 {len(card_cands)} 张，请回复序号选择："]
        for i, c in enumerate(card_cands, 1):
            lines.append(f"  {i}) {c.get('bank') or ''} {c.get('card_name') or ''}"
                         f"（尾号 {c.get('tail') or '****'}）")
        question = "\n".join(lines) + "\n请回复序号（如：1）。"
    elif params.get("_product_candidates"):
        # ★ 购买理财澄清：列出在售产品（不再泛泛问"买哪个产品"）
        prod_cands = params["_product_candidates"]
        lines = [f"当前在售理财产品 {len(prod_cands)} 支，请回复序号选择要购买的："]
        for i, x in enumerate(prod_cands, 1):
            lines.append(f"  {i}) {x['name']}（年化 {x['annual_rate']}% · 起购 ¥{float(x['min_amount']):.0f} · {x['term']}）")
        question = "\n".join(lines) + "\n请回复序号（如：1），并告知购买金额。"
    elif params.get("_holding_candidates"):
        # ★ 赎回澄清：列出持仓（默认全额赎回）
        hold_cands = params["_holding_candidates"]
        lines = [f"您名下有 {len(hold_cands)} 笔在持理财，请回复序号选择要赎回的："]
        for i, h in enumerate(hold_cands, 1):
            lines.append(f"  {i}) {h.get('product_name') or ('持仓#' + str(h.get('id')))} · ¥{float(h.get('amount') or 0):,.2f}")
        question = "\n".join(lines) + "\n请回复序号（如：1），将全额赎回该笔持仓。"
    elif params.get("_payee_candidates"):
        # ★ 转账澄清：列出常用/最近收款人（不再泛泛问"收款人是谁"）
        payee_cands = params["_payee_candidates"]
        lines = ["请选择转账收款人（回复序号）："]
        for i, p in enumerate(payee_cands, 1):
            freq = "常用" if p.get("is_frequent") else "非常用"
            lines.append(f"  {i}) {p['nickname']} · {p['bank']} 尾号 {p['account_tail']}（{freq}）")
        question = "\n".join(lines) + "\n请回复序号（如：1），或直接说出收款人姓名。"
    elif action == "internal_transfer" and params.get("_account_names"):
        # ★ 归集澄清：给出具体账户清单与示例句式
        names = "、".join(params["_account_names"])
        question = (
            f"资金归集请说明：从哪个账户、转到哪个账户、转多少金额。\n"
            f"您名下可用账户：{names}。\n例如：「从零钱罐转 500 到主账户」。"
        )
    elif candidates:
        lines = [f"找到 {len(candidates)} 个叫「{candidates[0]['nickname']}」的收款人，请回复序号选择："]
        for i, c in enumerate(candidates, 1):
            freq = "常用" if c.get("is_frequent") else "非常用"
            lines.append(f"  {i}) {c['nickname']} · {c['bank']} 尾号 {c['account_tail']}（{freq}）")
        question = "\n".join(lines) + "\n请回复序号（如：1）。"
    elif params.get("_sub_candidates"):
        # ★ 订阅候选清单（用户说"帮我处理闲置的订阅"时不再干问"取消哪个"）；
        #   闲置订阅排前面并标注天数，用户一眼看到该取消哪个。
        sub_cands = params["_sub_candidates"]
        lines = [f"您当前生效中的订阅有 {len(sub_cands)} 个，请回复序号选择要取消的："]
        for i, x in enumerate(sub_cands, 1):
            idle = f" · 闲置 {x['idle_days']} 天" if x.get("idle_days") else ""
            lines.append(f"  {i}) {x['service_name']}（¥{float(x['fee']):.0f}/{x['cycle']}{idle}）")
        question = "\n".join(lines) + "\n请回复序号（如：1）。"
    else:
        missing = _missing_required(action, params)
        need = "、".join(_REQUIRED_LABEL.get(m, m) for m in missing)
        note = params.get("_missing_note")
        if note and missing:
            # 解析器留下了明确原因（未找到某账户 / 某卡 / 某产品）→ 直接讲原因 + 缺什么，
            # 比通用话术更有用，也省一次 LLM 调用。
            question = f"{note}。请补充：{need}。"
        else:
            question = _gen_clarify_text(action, need)

    asked = state.get("asked_times", 0) + 1
    _emit_node("clarify", "wait", "需要补充信息")
    value = interrupt(question)  # ⏸ #1：要素不全
    # 恢复：把用户回答追加为 user 消息，回 fill_params 重新抽取
    return {"asked_times": asked, "messages": [{"role": "user", "content": str(value)}]}


def _verify_queue(action: str, params: dict) -> tuple[list[str], bool]:
    """B2 风控分级 → 双要素队列。

    返回 (验证因子队列, 是否大额警示)：
      转账  ≤2000 且常用收款人   → ["pay_password"]                  （你知）
      转账  >2000 或 非常用      → ["sms_code","pay_password"]      （你有+你知）
      转账  >20000 或 新收款人大额 → 同上双要素 + 确认卡大额警示行
      理财  >10000               → ["pay_password"]
      归集（本人账户间）           → ["pay_password"]                  （免验证码）
      其余低风险动作             → []
    ★ 验证码走 C3 新送达通道（收件箱必达）；密码核验走 /api/pay/password/verify。
    """
    amt = float(params.get("amount") or 0)
    if action == "transfer":
        frequent = not _is_frequent_false(params)   # True = 常用
        # ★ 阈值运行时从 system_config 读取（rules.get_rule）—— 管理端改完即生效
        if amt > rules.get_rule("transfer_verify_threshold") or not frequent:
            big = amt > rules.get_rule("transfer_big_threshold") or (
                not frequent and amt > rules.get_rule("transfer_big_unfrequent_threshold"))
            return ["sms_code", "pay_password"], big
        return ["pay_password"], False
    if action == "internal_transfer":
        # ★ 本人账户之间划转：资金没离开自己名下，验证码（防转错人）没有意义，
        #   风险分级里 internal_transfer 归为「仅支付密码」。
        return ["pay_password"], False
    if action == "buy_product":
        return (["pay_password"], False) if amt > rules.get_rule("wealth_verify_threshold") else ([], False)
    return [], False


def risk_check(state: WriteState) -> dict:
    action = state["task"]["action"]
    params = state.get("params") or {}
    _emit_node("risk_check", "start")
    queue, big = _verify_queue(action, params)
    uid = get_current_user_id() or config.DEFAULT_USER_ID
    # B2：锁定状态直接拒绝（错 3 次锁到当日 23:59:59），不让流程走到验证卡再失败
    if "pay_password" in queue and paypass.is_locked(uid):
        _emit_node("risk_check", "error", "支付密码已锁定")
        return {"result": {"error": "支付密码已锁定（连续错误 3 次），今日无法进行资金操作，请明日再试"}}
    need = bool(queue)
    _emit_node("risk_check", "done",
               "命中风控规则，需安全验证" if need else "低风险，无需额外验证")
    # 预生成确认卡片（数据驱动），存入 params，confirm 节点直接复用（避免中断重放重算）
    # ★ need_verify 必须放在 params 里（params 是已声明 channel）；
    #   若作为顶层未声明 channel 返回，LangGraph 会丢弃 → 验证码永不触发。
    params2 = dict(params)
    params2["_need_verify"] = need
    params2["_verify_queue"] = queue
    params2["_big_warning"] = big
    if "pay_password" in queue:
        params2["_password_set"] = paypass.has_password(uid)
    params2["_confirm_card"] = _gen_confirm_card(action, params2)
    return {"params": params2}


def _verify_question(factor: str, params: dict) -> str:
    """构造 verify 中断文本（带结构化标记，供 _parse_verify 生成卡片）。

    ★标记必须整体在方括号内：[VERIFY:<factor>|k=v|queue=a,b]，
      否则 _parse_verify 只截到第一个 ']'，queue/need_set 会泄漏进正文。
    """
    queue = list(params.get("_verify_queue") or [])
    opts = "|queue=" + ",".join(queue) if queue else ""
    if factor == "sms_code":
        return (f"[VERIFY:sms_code{opts}]验证码已发送至您的收件箱，"
                "请打开右上角「收件箱」查看并输入 6 位验证码。")
    need_set = not params.get("_password_set", True)
    if need_set:
        return (f"[VERIFY:pay_password{opts}|need_set=1]"
                "您尚未设置支付密码。请在下方设置 6 位数字支付密码，设置后本次操作将继续。")
    return (f"[VERIFY:pay_password{opts}]请输入 6 位支付密码完成身份核验。"
            "密码不会保存在对话记录中。")


def verify(state: WriteState) -> dict:
    """B4：verify 循环节点，按 _verify_queue 逐项执行（队头路由 resume 值）。

    - sms_code：issue_code 幂等发码（验证表+收件箱双写同事务），收件箱取码，
      对话文本里绝不出现验证码；
    - pay_password：中断只发"请输入密码"的指令；真实核验在前端调
      /api/pay/password/verify 完成（服务端放行标记 + 固定占位符 resume），
      密码零落盘。
    """
    action = state["task"]["action"]
    params = state.get("params") or {}
    attempts = state.get("verify_attempts", 0)
    uid = get_current_user_id() or config.DEFAULT_USER_ID
    _emit_node("verify", "start")
    queue = list(params.get("_verify_queue") or [])
    if not queue:
        # 无需验证（理论上路由不会进来）→ 直接放行
        return {"verified": True}
    head = queue[0]
    purpose = {"transfer": "转账安全验证", "buy_product": "理财购买安全验证",
               "internal_transfer": "资金归集安全验证"}.get(action, action)

    if head == "sms_code":
        # 幂等发码（重放安全）：已有未过期未使用码则复用；失败整体失败不中断
        try:
            verify_codes.issue_code(uid, scene=action, purpose=purpose)
        except Exception as exc:  # noqa: BLE001
            _emit_node("verify", "error", "验证码发送失败")
            return {"result": {"error": f"验证码发送失败：{exc}"}}
        _emit_node("verify", "wait", "验证码已送达收件箱，等待输入")
        value = interrupt(_verify_question("sms_code", params))  # ⏸ #2：验证码
        text = str(value).strip()
        if is_cancel(text):
            _emit_node("verify", "error", "用户取消")
            return {"result": {"error": "用户取消操作"}}
        if re.fullmatch(r"\d{4,8}", text):
            check = verify_codes.check_code(uid, text)
            if check.get("ok"):
                _emit_node("verify", "done", "验证码核验通过")
                params2 = dict(params)
                params2["_verify_queue"] = queue[1:]
                return {"params": params2, "messages": [{"role": "user", "content": text}]}
            err = check.get("error") or "验证码不正确"
        else:
            err = "请输入收到的 6 位数字验证码"
        if "已作废" in err or "错误 3 次" in err:
            _emit_node("verify", "error", "验证码错误次数过多")
            return {"result": {"error": err}}
        attempts += 1
        if attempts >= 3:
            _emit_node("verify", "error", "验证码错误次数过多")
            return {"result": {"error": "验证码错误 3 次，本次操作已作废，请重新发起"}}
        _emit_node("verify", "retry", f"验证码错误，还可重试 {3 - attempts} 次")
        return {"verify_attempts": attempts, "messages": [{"role": "user", "content": text}]}

    if head == "pay_password":
        if paypass.is_locked(uid):
            _emit_node("verify", "error", "支付密码已锁定")
            return {"result": {"error": "支付密码已锁定（连续错误 3 次），请明日再试"}}
        _emit_node("verify", "wait", "等待支付密码核验")
        value = interrupt(_verify_question("pay_password", params))  # ⏸ #2b：支付密码
        text = str(value)
        # ★ 密码零落盘：前端只会回传固定占位符；真实密码只出现在
        #   /api/pay/password/verify 的请求体里，校验完即丢，不入任何记录。
        if text in (PWD_RESUME_PLACEHOLDER, PWD_SET_PLACEHOLDER):
            if paypass.has_passed(uid):
                _emit_node("verify", "done", "支付密码核验通过")
                params2 = dict(params)
                params2["_verify_queue"] = queue[1:]
                params2["_password_set"] = True
                return {"params": params2,
                        "messages": [{"role": "user", "content": PWD_RESUME_PLACEHOLDER}]}
            # 放行标记缺失（服务重启 / 超时 / 未先调核验接口）→ 重新要求
            _emit_node("verify", "retry", "支付密码核验未完成，请重新输入")
            return {"messages": [{"role": "user", "content": PWD_RESUME_PLACEHOLDER}]}
        if is_cancel(text):
            _emit_node("verify", "error", "用户取消")
            return {"result": {"error": "用户取消操作"}}
        # 其他输入：不当密码处理（密码从不进对话），引导用户用密码卡
        _emit_node("verify", "retry", "请使用支付密码输入框完成核验")
        return {"messages": [{"role": "user", "content": PWD_RESUME_PLACEHOLDER}]}

    # 未知因子：放行（fail-open 到确认环节，绝不因配置问题卡死资金流程）
    params2 = dict(params)
    params2["_verify_queue"] = queue[1:]
    return {"params": params2}


def confirm(state: WriteState) -> dict:
    action = state["task"]["action"]
    params = state.get("params") or {}
    card = params.get("_confirm_card") or _gen_confirm_card(action, params)
    retries = state.get("confirm_retries", 0)
    prompt = card if retries == 0 else card + "\n\n（请明确回复「确认」以执行；或回复「取消」放弃）"
    _emit_node("confirm", "wait", "等待用户确认")
    value = interrupt(prompt)  # ⏸ #3：操作确认
    text = str(value)
    if is_confirm(text):
        _emit_node("confirm", "done", "已获确认")
        return {"confirmed": True, "messages": [{"role": "user", "content": text}]}
    if is_cancel(text):
        _emit_node("confirm", "error", "用户取消")
        return {"result": {"error": "用户取消操作"}}
    # 含糊（如「嗯」/注入）→ 重新明确询问，最多 2 次后取消
    retries += 1
    if retries >= 2:
        _emit_node("confirm", "error", "未获得明确确认")
        return {"result": {"error": "未获得明确确认，已取消操作"}}
    _emit_node("confirm", "retry", "回答含糊，重新确认")
    return {"confirm_retries": retries, "messages": [{"role": "user", "content": text}]}


def execute(state: WriteState) -> dict:
    action = state["task"]["action"]
    params = dict(state.get("params") or {})
    spec = ACTION_SPECS[action]
    _emit_node("execute", "start")

    # ★ 幂等键（Phase 4）：本次运行的键**存进 params 并回写 state** ——
    #   这样重入 / 重试 execute 时复用同一个 key，后端回放原回执而不是重复扣款；
    #   用户另起一笔业务则是新的 state → 新 key，不受影响。
    #   ★ 业务失败时后端会删掉占位记录，因此"修正后重试"仍可正常执行。
    idem_key = params.get("_idem_key") or uuid.uuid4().hex
    params["_idem_key"] = idem_key

    # P0.2：本操作已走完 verify/paypass/confirm 全链路 → 签发一次性操作票据，
    #       随请求透传 X-Op-Token，8003 写端点据此放行（防直连绕过写流程）。
    #       ★ 安全失败：签发失败则 op_token=None，8003 必以 401 拒绝（绝不静默放行）。
    op_token = None
    try:
        from utils import op_auth
        _uid = get_current_user_id() or config.DEFAULT_USER_ID
        op_token = op_auth.issue_op_token(_uid, action).get("token")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 操作票据签发失败（写端点将拒绝执行）: {exc}")

    # 操作者身份由认证头携带 —— build_body 不再接收 uid、也不再输出 user_id 字段
    body = spec["build_body"](params)
    path = spec["path"]
    if "{sub_id}" in path:
        path = path.format(sub_id=params["sub_id"])
    _extra = {"Idempotency-Key": idem_key}
    if op_token:
        _extra["X-Op-Token"] = op_token
    r = _http(spec["method"], path, json_body=body, extra_headers=_extra)
    if r.get("ok"):
        _emit_node("execute", "done", r.get("message") or "执行成功")
        # C3 顺手扩展：执行成功 → 写 transfer_receipt 通知（收件箱真实送达）。
        # 失败只记日志，绝不影响已成功的业务本身。
        try:
            _notify_execution(action, params, r)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 回执通知写入失败（业务已成功）: {exc}")
        return {"params": params, "result": {"ok": True, "action": action, "data": r}}
    _emit_node("execute", "error", r.get("error", "执行失败"))
    return {"params": params,
            "result": {"ok": False, "action": action, "error": r.get("error", "执行失败")}}


def _notify_execution(action: str, params: dict, result: dict) -> None:
    """转账 / 理财成功 → 站内收件箱回执通知（含金额 / 收款人 / 流水号）。"""
    from utils import inbox
    uid = get_current_user_id() or config.DEFAULT_USER_ID
    serial = result.get("serial_no")
    if not serial:
        return
    amount = float(params.get("amount") or result.get("amount") or 0)
    if action == "transfer":
        # 转账回执（发送方 + 收款方）由 /api/transfer 端点在「同一事务」内写出，
        # 这里不再重复写，避免双发；行外/站内两轨的文案差异也由端点统一处理。
        return
    if action == "internal_transfer":
        # 同理：归集的转出/转入两条通知由 /api/accounts/transfer-internal
        # 在同一事务里写出，这里不重复（双发会让收件箱出现 4 条"资金归集"）。
        return
    if action == "buy_product":
        product = (params.get("product_info") or {}).get("name") or result.get("product") or "理财产品"
        # 回执含持仓确认：买入成功 → holdings 新增持仓 #id，附扣款后余额
        holding_id = result.get("holding_id")
        balance_after = result.get("balance_after")
        extra = f"（持仓 #{holding_id}）" if holding_id else ""
        if balance_after is not None:
            extra += f"，扣款后余额 ¥{float(balance_after):,.2f}"
        inbox.push_transfer_receipt(uid, amount, f"{product}{extra}", serial, "理财申购")
    elif action == "redeem":
        inbox.push_transfer_receipt(uid, amount, f"持仓 #{params.get('holding_id')}", serial, "赎回")


# --------------------------------------------------------------------------- #
#  条件边
# --------------------------------------------------------------------------- #
def _last_user_text(messages: list[Any]) -> str:
    for m in reversed(messages or []):
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        role = m.get("role") if isinstance(m, dict) else getattr(m, "type", None)
        if role in ("user", "human"):
            return c or ""
    return ""


def route_after_phone_guard(state: WriteState) -> str:
    # 闸门命中 → result 已置，直接结束；否则放行进入 fill_params
    return END if state.get("result") else "fill_params"


def route_after_fill(state: WriteState) -> str:
    if state.get("result"):
        return END
    action = state["task"]["action"]
    # 尾号不符已标记 → 进入 clarify 追问确认（防转错人）
    if (action == "transfer") and (state.get("params") or {}).get("_need_tail_confirm"):
        return "clarify"
    if _missing_required(action, state.get("params") or {}):
        return "clarify"
    return "risk_check"


def route_after_clarify(state: WriteState) -> str:
    return END if state.get("result") else "fill_params"


def route_after_risk(state: WriteState) -> str:
    # ★ risk_check 可能直接给出 result（如支付密码已锁定）——必须先看 result 再看队列，
    #   否则锁定拦截会被条件边无视、流程继续走 confirm/execute（锁定形同虚设）。
    if state.get("result"):
        return END
    return "verify" if (state.get("params") or {}).get("_need_verify") else "confirm"


def route_after_verify(state: WriteState) -> str:
    if state.get("verified"):
        return "confirm"
    if state.get("result"):
        return END
    # B4：队列逐项执行 —— 队列清空即验证阶段结束，进入确认
    if not (state.get("params") or {}).get("_verify_queue"):
        return "confirm"
    return "verify"


def route_after_confirm(state: WriteState) -> str:
    if state.get("confirmed"):
        return "execute"
    if state.get("result"):
        return END
    return "confirm"


# --------------------------------------------------------------------------- #
#  构图
# --------------------------------------------------------------------------- #
def build_write_flow(checkpointer=None, llm_client=None):
    """编译并返回写操作子图（裸编译，不自带 checkpointer）。

    M3 重构要点：
    - 子图一律不配 checkpointer；由主图 compile 时统一给一份（SqliteSaver），
      否则 checkpoint 分裂、thread 语义错乱。
    - 以 input=WriteIn / output=WriteOut 约束边界：主图只传入 messages+task，
      只取回 messages+result；内部字段不外泄。interrupt / Command(resume) 的
      挂起与冒泡对主图完全透明（主图不需要感知子图层级）。
    - 禁止用函数包一层 sub.invoke() 适配状态（会打断 interrupt 穿透链）。

    llm_client：可注入（离线自测用 FakeClient）；None 则构造 DeepSeek。
    """
    global _LLM_CLIENT
    _LLM_CLIENT = llm_client

    g = StateGraph(WriteState, input_schema=WriteIn, output_schema=WriteOut)
    g.add_node("phone_guard", phone_guard)
    g.add_node("fill_params", fill_params)
    g.add_node("clarify", clarify)
    g.add_node("risk_check", risk_check)
    g.add_node("verify", verify)
    g.add_node("confirm", confirm)
    g.add_node("execute", execute)

    g.add_edge(START, "phone_guard")
    g.add_conditional_edges("phone_guard", route_after_phone_guard, {"fill_params": "fill_params", END: END})
    g.add_conditional_edges("fill_params", route_after_fill, {"clarify": "clarify", "risk_check": "risk_check", END: END})
    g.add_conditional_edges("clarify", route_after_clarify, {"fill_params": "fill_params", END: END})
    g.add_conditional_edges("risk_check", route_after_risk,
                            {"verify": "verify", "confirm": "confirm", END: END})
    g.add_conditional_edges("verify", route_after_verify, {"confirm": "confirm", "verify": "verify", END: END})
    g.add_conditional_edges("confirm", route_after_confirm, {"execute": "execute", "confirm": "confirm", END: END})
    g.add_edge("execute", END)

    # checkpointer=None 即「裸编译」，由上层（主图）统一持有持久化。
    return g.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    g = build_write_flow()
    print("write_flow 编译成功，节点：", list(g.get_graph().nodes))
