"""
agent/tools.py —— 工具定义（OpenAI function calling 格式）与执行器

★ 安全设计（M1）：
  自由 Agent 只拿到【只读】工具（LOCAL_TOOLS / READ_TOOLS）。
  所有写操作（转账 / 买理财 / 冻结卡 / 取消订阅）在 M2 走独立的安全工作流，
  绝不出现在自由 Agent 的工具清单里，避免"边聊边改数据"。

★ 外部实时金融数据（MCP 接入，只读）：
  阿里云百炼 MCP 广场的「东方财富妙想」作为外部数据源，动态并入 read_agent
  的自由工具清单（全部 mcp_ 前缀、全部只读）。MCP 不可用（未配 key / 断连 /
  熔断）时 get_llm_tools() 自动退回纯本地清单 —— 动态降级，不影响主链路。
  mcp_ 工具只在自由对话里由 LLM 按需选择，不进主图 whitelist / plan_check。

执行方式：execute_tool 内部用 requests 调 mock_bank（FastAPI）或转调百炼 MCP；
         base url 从 config.BANK_API_BASE 读；任何异常都收敛成 {"error": "..."}，
         让 LLM 看到可读错误而不是把异常抛到图外。
"""
from __future__ import annotations

import json
from typing import Any, Callable

import requests

import config
from utils import mcp_bridge
from utils.security import auth_headers, get_current_user_id

MCP_PREFIX = mcp_bridge.MCP_PREFIX

# --------------------------------------------------------------------------- #
#  本地工具 Schema（OpenAI / DeepSeek function calling 格式）
#  MCP 外部工具不写死在这里，见下方 build_llm_tools() 的动态并入逻辑。
# --------------------------------------------------------------------------- #
LOCAL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_balance",
            "description": "查询账户余额。不传 account_id 时返回当前用户的全部账户及总余额。",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "integer",
                        "description": "账户 ID，可选。省略则返回全部账户。",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_transactions",
            "description": (
                "按日期区间查询银行流水，返回明细 + 汇总（总支出/总收入/分类占比）。"
                "用户说'这个月/上个月/最近三个月'等相对时间时，必须先换算成 "
                "start/end 的绝对日期（YYYY-MM-DD）再调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                    "end": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                    "category": {
                        "type": "string",
                        "description": "分类过滤：餐饮/购物/交通/娱乐/订阅/转账/收入",
                    },
                    "account_id": {"type": "integer", "description": "账户 ID，可选"},
                    "type": {"type": "string", "description": "expense=支出 / income=收入，可选"},
                },
                "required": ["start", "end"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_payees",
            "description": (
                "按昵称模糊查询收款人。注意昵称可能重复（例如两个'妈妈'），"
                "命中多条时必须把候选列给用户确认，禁止自行挑一个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "昵称关键字，如 妈妈；为空返回全部收款人"}
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_products",
            "description": "查询在售理财产品列表（含期限、风险等级、年化收益率、起投金额）。查不到即代表没有该产品。",
            "parameters": {
                "type": "object",
                "properties": {
                    "risk_level": {"type": "integer", "description": "风险等级 1/2/3，可选"},
                    "max_amount": {"type": "number", "description": "按起投金额上限过滤，可选"},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_subscriptions",
            "description": "查询用户的订阅/会员扣费项：含来源（contracted=签约代扣 / identified=流水识别）、"
                           "关联商户、下次扣款日 next_due、月付合计、闲置天数（idle_over_60）。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_subscriptions",
            "description": "从近 6 个月流水中识别「疑似周期性订阅扣款但尚未登记」的商户候选"
                           "（如每月固定日期扣同额费用的视频会员）。返回候选列表（商户/金额/周期/"
                           "出现次数/预计下次扣款日），供用户确认后转正为订阅。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_profile",
            "description": "查询当前登录用户的称呼、性别、职业、账户、偏好等基本信息。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate_to",
            "description": (
                "当用户明确要求『打开 / 进入 / 去 / 查看某个功能页面』，或希望跳转到对应页面"
                "继续操作时调用（如『打开我的账单详情』『去转账』『看看我的理财』『进入安全中心』）。"
                "调用后前端会自动跳转到该页面，无需用户再点。仅用于页面导航，不涉及任何数据查询"
                "或资金操作。纯咨询类问题（答案在对话里就能说完）不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page": {
                        "type": "string",
                        "description": "目标页面路径（必须是白名单之一）",
                        "enum": ["/bills", "/transfer", "/wealth", "/cards", "/subs", "/safe", "/chat"],
                    }
                },
                "required": ["page"],
                "additionalProperties": False,
            },
        },
    },
]

# 页面导航白名单：navigate_to 工具可用的目标页，以及回写给前端 nav 时的文案。
# 路径与前端 SPA 路由严格一致（见 web/index.html ALL_ROUTES）。
# ★ 只给 label，不带 icon：全站 0 emoji 0 icon（README §22 / CONTRIBUTING 表格）。
NAV_PAGES: dict[str, dict] = {
    "/bills":    {"label": "账单分析"},
    "/transfer": {"label": "转账"},
    "/wealth":   {"label": "理财"},
    "/cards":    {"label": "卡片管理"},
    "/subs":     {"label": "订阅管理"},
    "/safe":     {"label": "安全中心"},
    "/chat":     {"label": "对话"},
}

# ★ 只读白名单【仅含本地工具】：主图 whitelist / plan_check / execute_tool 兜底拦截
#   都拿它做显式校验。MCP 外部工具（mcp_ 前缀）是另一条受控通道：
#   - 只允许出现在 read_agent 自由对话的工具清单里（见 get_llm_tools）
#   - execute_tool 对 mcp_ 前缀做独立分发（同样只读），不进本白名单
READ_TOOLS: list[dict] = LOCAL_TOOLS
READ_TOOL_NAMES: list[str] = [t["function"]["name"] for t in READ_TOOLS]


# --------------------------------------------------------------------------- #
#  MCP 外部工具动态并入（read_agent 每轮 LLM 调用前经 get_llm_tools() 拉取）
# --------------------------------------------------------------------------- #
def build_llm_tools() -> list[dict]:
    """本地只读工具 +（MCP 可用时的）mcp_ 外部金融工具。

    MCP 不可用（未配 key / 连接失败 / 连续失败熔断）时返回纯本地清单 ——
    这就是"断网 / key 错误启动不崩、TOOLS 自动降级"的落点。
    """
    return list(LOCAL_TOOLS) + mcp_bridge.get_openai_tools()


# 模块级全量工具快照（兼容历史调试引用；读侧请一律走 get_llm_tools）
TOOLS: list[dict] = build_llm_tools()


def get_llm_tools() -> list[dict]:
    """read_agent 每次调用 LLM 前取工具清单（★动态：熔断后下一轮自动摘掉 mcp_）。"""
    global TOOLS
    TOOLS = build_llm_tools()
    return TOOLS


# --------------------------------------------------------------------------- #
#  HTTP 调用 mock_bank
# --------------------------------------------------------------------------- #
def _request(method: str, path: str, **kwargs) -> dict:
    """统一 HTTP 出口：任何异常都转成 {'error': ...}。

    ★ 认证头：当前登录用户的 token 由进程内上下文带出（run_server /api/chat 设置），
       mock_bank 据此解析真实 user_id —— 工具层不再有"传 user_id 参数"这回事。
       没有上下文时（CLI 直跑）不带头，mock_bank 回落默认演示用户。
    """
    url = f"{config.BANK_API_BASE.rstrip('/')}{path}"
    headers = dict(kwargs.pop("headers", None) or {})
    headers.update(auth_headers())
    try:
        resp = requests.request(method, url, timeout=config.HTTP_TIMEOUT,
                                headers=headers, **kwargs)
    except requests.exceptions.ConnectionError:
        return {
            "error": f"无法连接 Mock 银行服务（{url}）。请先在 bank-agent/ 目录执行："
                     f"uvicorn mock_bank.main:app --reload --port {config.BANK_API_PORT}"
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"请求 {url} 失败：{exc}"}

    if resp.status_code != 200:
        return {"error": f"Mock 银行服务返回 HTTP {resp.status_code}: {resp.text[:300]}"}
    try:
        data = resp.json()
    except ValueError:
        return {"error": f"Mock 银行服务返回非 JSON: {resp.text[:300]}"}
    if not isinstance(data, dict):
        return {"error": f"意外的返回类型: {type(data).__name__}"}
    return data


def _get(path: str, params: dict | None = None) -> dict:
    clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    return _request("GET", path, params=clean)


def _slim(payload: dict) -> dict:
    """裁掉明细里的大字段，避免把几十条流水全塞回上下文。"""
    if not payload.get("ok"):
        return payload
    data = dict(payload)
    txs = data.get("transactions")
    if isinstance(txs, list) and len(txs) > 40:
        data["transactions"] = txs[:40]
        data["_truncated"] = f"明细共 {len(txs)} 条，此处仅返回前 40 条；统计口径基于全部明细。"
    return data


# --------------------------------------------------------------------------- #
#  工具实现（与 TOOLS 一一对应，名称保持一致）
# --------------------------------------------------------------------------- #
def get_balance(account_id: int | None = None, **_: Any) -> dict:
    return _get("/api/balance", {"account_id": account_id})


def get_transactions(
    start: str,
    end: str,
    category: str | None = None,
    account_id: int | None = None,
    type: str | None = None,
    **_: Any,
) -> dict:
    return _slim(
        _get(
            "/api/transactions",
            {"start": start, "end": end, "category": category, "account_id": account_id, "type": type},
        )
    )


def list_payees(keyword: str | None = None, **_: Any) -> dict:
    return _get("/api/payees", {"keyword": keyword})


def list_products(risk_level: int | None = None, max_amount: float | None = None, **_: Any) -> dict:
    return _get("/api/products", {"risk_level": risk_level, "max_amount": max_amount})


def list_subscriptions(**_: Any) -> dict:
    return _get("/api/subscriptions")


def discover_subscriptions(**_: Any) -> dict:
    return _get("/api/subscriptions/discover")


def get_user_profile(user_id: int | None = None, **_: Any) -> dict:
    """只读：直接读「当前登录用户」的画像摘要（不经 LLM）。

    ★ user_id 入参已被移除出工具 schema（LLM 无法指定查谁）；
      这里再拦一道，防止历史 checkpoint / 注入残留带 user_id 过来。
       返回与 Agent 注入系统提示一致的字段：称呼 / 姓名 / 性别 / 账户 / 偏好 / 当前时间，
       不含任何敏感信息（手机号只以尾号存在，本工具不返回手机号）。
    """
    from profile_db import get_user_context

    # 进程内当前用户（HTTP 请求由 token 解析；CLI 无上下文回落默认演示用户）
    uid = get_current_user_id() or config.DEFAULT_USER_ID
    try:
        return {"ok": True, **get_user_context(uid)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"查询用户画像失败：{exc}"}


def navigate_to(page: str, **_: Any) -> dict:
    """只读导航工具：登记用户想去哪个页面；前端收到 nav.auto 后自动跳转。

    不产生任何副作用（不查数据、不动资金），仅作"意图登记"。
    页面不在白名单时返回 error，让 LLM 自行修正或改为纯文本回答。
    """
    if page not in NAV_PAGES:
        return {"error": f"未知页面：{page}（可用：{', '.join(NAV_PAGES)}）"}
    return {"ok": True, "page": page, "navigated": True}


TOOL_IMPLS: dict[str, Callable[..., dict]] = {
    "get_balance": get_balance,
    "get_transactions": get_transactions,
    "list_payees": list_payees,
    "list_products": list_products,
    "list_subscriptions": list_subscriptions,
    "discover_subscriptions": discover_subscriptions,
    "get_user_profile": get_user_profile,
    "navigate_to": navigate_to,
}


# --------------------------------------------------------------------------- #
#  执行入口
# --------------------------------------------------------------------------- #
def _parse_args(args: Any) -> dict:
    if args is None:
        return {}
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args) if args.strip() else {}
            return parsed if isinstance(parsed, dict) else {"_raw": args}
        except ValueError:
            return {"_raw": args}
    return {}


def parse_tool_call(tc: Any) -> tuple[str | None, str | None, dict]:
    """把 tool_call 统一解析成 (id, name, args_dict)。

    需要同时兼容两种表示：
    - OpenAI 原生：{"id":.., "type":"function", "function":{"name":..,"arguments":"<json str>"}}
    - LangChain / LangGraph：{"name":.., "args":{..}, "id":.., "type":"tool_call"}
      （add_messages 会把 OpenAI 格式自动转成这一种，所以 state 里拿到的是后者）
    """
    if not isinstance(tc, dict):
        fn = getattr(tc, "function", None)
        tc = {
            "id": getattr(tc, "id", None),
            "name": getattr(tc, "name", None) if hasattr(tc, "name") else None,
            "args": getattr(tc, "args", None) if hasattr(tc, "args") else None,
            "function": fn,
        }

    fn = tc.get("function")
    if isinstance(fn, dict):
        name, raw = fn.get("name"), fn.get("arguments")
    elif fn is not None:  # OpenAI SDK 对象
        name, raw = getattr(fn, "name", None), getattr(fn, "arguments", None)
    else:  # LangChain 风格
        name, raw = tc.get("name"), tc.get("args")
    return tc.get("id"), name, _parse_args(raw)


def execute_tool(name: str, args: Any = None) -> dict:
    """执行一个工具调用，永远返回 dict；出错返回 {"error": "..."}。

    分发规则：
    - name 以 mcp_ 开头 → 剥前缀后转百炼 MCP（只读外部金融数据），结果 dict
      或 {"error": ...}，与本地工具同一返回契约；
    - 其余 → 原有 requests 调 Mock 银行逻辑（原样不动）；
    - M1 只注册只读工具，任何不在白名单里的名字（含未来的写工具）一律拒绝。
    """
    # ★ MCP 外部工具通道：只读、mcp_ 前缀、独立分发，不参与本地白名单
    if name.startswith(MCP_PREFIX):
        parsed = _parse_args(args)
        return mcp_bridge.call_mcp_tool_sync(name, parsed)

    impl = TOOL_IMPLS.get(name)
    if impl is None:
        if name in READ_TOOL_NAMES:
            return {"error": f"工具 {name} 未实现"}
        return {
            "error": f"工具 {name} 不可用：自由 Agent 只提供只读查询能力"
                     f"（可用：{', '.join(READ_TOOL_NAMES)}）"
        }
    parsed = _parse_args(args)
    try:
        result = impl(**parsed)
    except TypeError as exc:
        return {"error": f"工具 {name} 参数有误：{exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"工具 {name} 执行失败：{exc}"}
    if not isinstance(result, dict):
        return {"ok": True, "data": result}
    return result


def execute_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """把一条 assistant 消息里的多个 tool_calls 转成 OpenAI 的 tool 消息列表。"""
    outputs: list[dict] = []
    for tc in tool_calls or []:
        tc_id, name, args = parse_tool_call(tc)
        result = execute_tool(name, args)
        outputs.append(
            {
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "name": name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            }
        )
    return outputs
