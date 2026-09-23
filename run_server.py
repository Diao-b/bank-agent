"""
run_server.py —— Web 界面 + Agent 统一入口

一条命令把前端页面和 M3 主图跑在一起：
    python run_server.py
然后浏览器打开 http://127.0.0.1:8178

职责（只做适配，不感知子图层级）：
1. 按 thread_id 缓存 MainDriver（一个会话一个实例），每线程加锁防并发 invoke
2. POST /api/chat：文本进 / 解析结果出。对 driver 返回的 interrupt 文本做轻量
   解析（确认卡字段 / 验证码），让前端能渲染结构化卡片；解析失败就当纯文本
3. GET /api/state：代理 mock_bank 的余额 / 操作日志，供左侧信息栏展示
4. GET /：挂载 web/ 静态目录，前后端同源免 CORS

前置条件（与 run_cli 相同）：
    - MySQL 已启动且 bank_agent 库已初始化（init_db.py）
    - Mock 银行服务已启动：uvicorn mock_bank.main:app --port 8000
    - .env 已配置 DEEPSEEK_API_KEY
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import re
import threading
import time
from datetime import date

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, JSONResponse, StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from mock_bank import db  # 连接收口到单一源（组 R）
import profile_db  # 用户画像数据层（MySQL bank_agent）
from agent.driver import MainDriver
from routes._common import WEB_DIR, _bank_headers, _resolve_uid  # P2.1：跨域共享 helper
from utils import event_bus, mcp_client, scheduler, security
from utils.digital_human import digital_human  # 数字柜员桥接层（Fay 渲染端，独立进程）

SERVER_PORT = 8178

app = FastAPI(title="小微 · AI 银行助手", docs_url=None, redoc_url=None)

# --------------------------------------------------------------------------- #
# 会话管理：thread_id → (driver, lock, owner_uid)
#   owner_uid：driver 首次被使用时绑定的登录用户。一个会话属于一个用户，
#   若他人拿同 thread_id 来续（正常 UI 不会发生，只有手工构造请求才可能），
#   拒绝而不是让他接着 A 的挂起流程往下办 —— 见 _get_driver。
# --------------------------------------------------------------------------- #
_drivers: dict[str, dict] = {}
_registry_lock = threading.Lock()


def _get_driver(thread_id: str, uid: int) -> tuple[MainDriver, threading.Lock]:
    """取（或建）thread 的 driver。★driver 与用户绑定：跨用户复用 → 403。"""
    with _registry_lock:
        entry = _drivers.get(thread_id)
        if entry is None:
            driver = MainDriver(thread_id=thread_id)
            entry = {"driver": driver, "lock": threading.Lock(), "owner_uid": uid}
            _drivers[thread_id] = entry
        if entry["owner_uid"] != uid:
            raise HTTPException(
                status_code=403,
                detail=f"会话 {thread_id} 不属于当前用户，无权继续（防跨用户续办挂起流程）",
            )
        return entry["driver"], entry["lock"]


@contextlib.contextmanager
def _login_ctx(uid: int, token: str | None):
    """进程内身份作用域：driver.chat() 跑在哪个用户下、带哪个 token。

    Agent 层（tools / write_flow / 画像注入）没有 HTTP 请求对象，全靠这个
    ContextVar 判断"我是谁"——必须在每次 chat 调用外沿设置，且退出后立即还原，
    否则 uvicorn 线程池复用线程时会把上一个登录用户的身份带到下一个请求。
    """
    with security.current_user_ctx(uid):
        security.set_current_auth_token(token)
        try:
            yield
        finally:
            security.set_current_auth_token(None)


# --------------------------------------------------------------------------- #
# interrupt 文本轻量解析（fail-soft：解析不出就按纯文本返回）
# --------------------------------------------------------------------------- #
_ROW_RE = re.compile(r"^\s*([\u4e00-\u9fa5A-Za-z /]+)：(.+?)\s*$")
_AMOUNT_RE = re.compile(r"[¥￥]\s*([0-9,]+(?:\.[0-9]{1,2})?)")


def _parse_confirm(text: str) -> dict | None:
    """把 _gen_confirm_card 的文本解析成行式字段 + 金额。"""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].startswith("请确认"):
        return None
    title = lines[0].rstrip("：")
    fields: list[dict] = []
    amount: float | None = None
    for line in lines[1:]:
        if "回复「确认」" in line or "回复「取消」" in line:
            continue
        m = _ROW_RE.match(line)
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        if key == "金额":
            amt = _AMOUNT_RE.search(value)
            if amt:
                amount = float(amt.group(1).replace(",", ""))
        fields.append({"key": key, "value": value})
    if not fields:
        return None
    return {"kind": "confirm", "title": title, "fields": fields, "amount": amount}


def _parse_verify(text: str) -> dict | None:
    """解析 write_flow verify 中断的结构化标记 [VERIFY:<factor>|k=v...]。

    ★返回必须带 verify 类型字段（factor）—— 前端按类型渲染卡片，
      支付密码和验证码绝不能搞混；验证码正文里不再出现码本身，
      取码一律走站内收件箱。
    """
    raw = text or ""
    if not raw.startswith("[VERIFY:"):
        return None
    end = raw.find("]")
    if end < 0:
        return None
    head = raw[:end + 1]
    body = raw[end + 1:].strip()
    inner = head[len("[VERIFY:"):-1]
    parts = inner.split("|")
    factor = (parts[0] or "").strip()
    opts: dict[str, str] = {}
    for kv in parts[1:]:
        if "=" in kv:
            k, v = kv.split("=", 1)
            opts[k.strip()] = v.strip()
    queue = [q for q in (opts.get("queue") or "").split(",") if q] or [factor]
    return {
        "kind": "verify",
        "verify": factor,                       # ★类型字段
        "factor": factor,
        "need_set": opts.get("need_set") == "1",
        "queue": queue,
        "reply": body or "请完成安全验证。",
    }


_SERIAL_RE = re.compile(r"流水号\s*([A-Za-z0-9]+)")


def _parse_receipt(text: str) -> dict | None:
    """从执行成功的回执文本里抠出流水号与金额，供前端渲染绿色回执卡。

    只做「有就用、没有就算」的解析：文本由 mock_bank 的 message 拼成，
    格式变了也不会影响主流程（前端拿不到 receipt 就退回纯文本气泡）。
    """
    if not text or "流水号" not in text:
        return None
    m = _SERIAL_RE.search(text)
    if not m:
        return None
    out: dict = {"serial_no": m.group(1)}
    amt = re.search(r"转账\s*([0-9,]+(?:\.[0-9]{1,2})?)\s*元", text) or _AMOUNT_RE.search(text)
    if amt:
        try:
            out["amount"] = float(amt.group(1).replace(",", ""))
        except ValueError:
            pass
    return out


# 页面导航白名单：与前端 SPA 路由严格一致（见 web/index.html ALL_ROUTES）。
# Agent 调 navigate_to(page) 时 page 必在此表，后端据此回写 nav（label）。
# ★ 不带 icon：全站 0 emoji 0 icon（README §22）。
_NAV_PAGES: dict[str, dict] = {
    "/bills":    {"label": "账单分析"},
    "/transfer": {"label": "转账"},
    "/wealth":   {"label": "理财"},
    "/cards":    {"label": "卡片管理"},
    "/subs":     {"label": "订阅管理"},
    "/safe":     {"label": "安全中心"},
    "/chat":     {"label": "对话"},
}


def _detect_nav(text: str) -> dict | None:
    """从回复内容推断"可以去哪个页面看详情"，供前端在气泡下渲染跳转入口。

    这是"对话管操作、页面管浏览"的回流通道：Agent 在对话里给出结论后，
    引导用户到对应页面看完整数据，而不是把大段明细堆在聊天流里。
    判定按业务优先级从高到低，命中即返回；判不出就返回 None（前端不渲染）。
    """
    if not text:
        return None
    # 1. 操作已落地 → 安全中心看留痕（优先级最高：刚发生资金/权限变动）
    if any(k in text for k in ("已完成", "已成功", "转账成功", "已取消订阅",
                               "已冻结", "已解冻", "已为您赎回", "购买成功")):
        return {"to": "/safe", "label": "查看操作记录"}
    # 2. 账单/支出分析 → 账单分析页
    if any(k in text for k in ("支出", "花哪", "花了", "账单", "消费")):
        return {"to": "/bills", "label": "查看完整账单报告"}
    # 3. 订阅相关
    if "订阅" in text:
        return {"to": "/subs", "label": "查看订阅管理"}
    # 4. 理财 / 持仓
    if any(k in text for k in ("理财", "年化", "持仓", "赎回")):
        return {"to": "/wealth", "label": "查看理财与持仓"}
    # 5. 银行卡
    if any(k in text for k in ("银行卡", "尾号", "额度")):
        return {"to": "/cards", "label": "查看卡片管理"}
    return None


def _extract_agent_nav(driver: MainDriver) -> dict | None:
    """从主图最终 state 里捞本轮（最后一条 user 消息之后）的 navigate_to 工具调用，
    把 Agent 的导航意图回写成 nav（前端据此自动跳转，不必点）。

    比关键词 _detect_nav 更聪明：能理解口语化、间接的跳转诉求
    （如『我想看看上个月钱都花哪了』→ /bills），且优先级高于关键词。
    取不到 state 时静默返回 None，不影响主对话。
    """
    try:
        snap = driver.graph.get_state(driver.cfg)
    except Exception:  # noqa: BLE001
        return None
    if not snap or not getattr(snap, "values", None):
        return None
    msgs = snap.values.get("messages", []) or []
    # 只在本轮（最后一条 user/human 消息之后）找，避免命中历史轮次的 navigate_to
    last_user_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        role = m.get("role") if isinstance(m, dict) else getattr(m, "type", None)
        if role in ("user", "human"):
            last_user_idx = i
            break
    for m in msgs[last_user_idx + 1:]:
        tcs = m.get("tool_calls") if isinstance(m, dict) else getattr(m, "tool_calls", None)
        if not tcs:
            continue
        for tc in tcs:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if name != "navigate_to":
                continue
            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
            page = (args or {}).get("page") if isinstance(args, dict) else None
            if page in _NAV_PAGES:
                return {"to": page, "label": _NAV_PAGES[page]["label"]}
    return None


def parse_reply(text: str) -> dict:
    payload = _parse_confirm(text) or _parse_verify(text) or {"kind": "text"}
    # 确认卡 / 验证码阶段是"待用户决策"，此时不引导跳走，避免打断确认流程
    if payload["kind"] == "text":
        nav = _detect_nav(text)
        if nav:
            payload["nav"] = nav
        receipt = _parse_receipt(text)
        if receipt:
            payload["receipt"] = receipt
        # 写操作被「未绑定手机号」拦截 → 引导去绑定手机（与 transfer_page 同机制）
        if _suggest_bind_phone(text):
            payload["suggest"] = "bind_phone"
    return payload


# 「转账信息不全」的追问特征：命中则给前端一个跳转转账页的引导标记。
# 这里只认关键词、不调 LLM —— 判错的代价只是少/多一张引导卡，绝不影响资金流程。
_TRANSFER_ASK_MARKERS = ("收款人", "转给谁", "转给", "转账", "转多少", "汇给", "打给谁")


def _suggest_transfer_page(text: str) -> bool:
    return bool(text) and any(k in text for k in _TRANSFER_ASK_MARKERS)


def _suggest_bind_phone(text: str) -> bool:
    """回复文本命中「未绑手机号」拦截提示 → 前端渲染『去绑定手机』引导卡。"""
    return bool(text) and "绑定手机号" in text


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
class ChatReq(BaseModel):
    thread_id: str = "web"
    message: str


# ★ ProfileUpdate 模型随「账号域」迁到 routes/account.py（P2.1 路由分域）


def _reply_payload(reply: str, driver: MainDriver) -> dict:
    """把 driver 返回文本转成前端 payload（与 POST /api/chat 的返回结构完全一致）。

    SSE 的 done 事件直接携带同一结构 —— 前端收到后走和 POST 一模一样的渲染分支。
    """
    payload = parse_reply(reply or "")
    # LLM 结构化导航意图（navigate_to 工具）→ 自动跳转，优先级高于关键词 _detect_nav
    agent_nav = _extract_agent_nav(driver)
    if agent_nav:
        agent_nav["auto"] = True
        payload["nav"] = agent_nav
    payload.setdefault("reply", reply or "")
    # ★ 全站 0 emoji 0 icon：模型偶发在回复里带 emoji（实测出现过「都给您查好了 👇」），
    #   在这里统一剥离。必须放在 dh_speak_ms 之前 —— 播报时长按清洗后的文本估算。
    payload["reply"] = digital_human.strip_reply_emoji(payload["reply"])
    # 数字柜员播报时长（毫秒，按**实际朗读文本**估算，非原文长度）。
    # 「与小微对话」用它决定何时重开麦克风 —— 估算偏差是"麦克风听到它自己尾音"的根因，故由后端单点计算。
    payload["dh_speak_ms"] = digital_human.estimate_speak_ms(payload["reply"]) if digital_human.enabled else 0
    payload["suspended"] = driver.suspended()
    # Agent 正在等用户补充信息 → 不给跳转入口，避免把人从半途的办理流程里带走
    if payload["suspended"]:
        payload.pop("nav", None)
        # 「帮我转账」这类信息不全的追问：给前端一个结构化标记，让它渲染
        # 「前往转账页面 / 直接对话填写」的引导卡（前端自己拉收款人列表）。
        # 注意仍然是 clarify 中断挂着的状态 —— 用户继续在对话里补齐信息完全照旧。
        if payload["kind"] == "text" and _suggest_transfer_page(reply):
            payload["suggest"] = "transfer_page"
    return payload


def _dh_on(request: Request) -> bool:
    """数字柜员是否在本请求激活：前端开关头 X-Digital-Human=1 且 Fay 已配置。"""
    return (request.headers.get("X-Digital-Human") == "1") and digital_human.enabled


def _dh_interrupt() -> None:
    """新消息到达时打断上一句播报；未激活 / Fay 未启动则静默 no-op。"""
    if digital_human.enabled:
        digital_human.stop()


def _persist_chat(thread_id: str, user_msg: str, reply: str, uid: int) -> None:
    """审计项 M1：真实对话落库（失败只记日志，不影响对话链路）。"""
    try:
        profile_db.save_message(thread_id, "user", user_msg, uid)
        if reply:
            profile_db.save_message(thread_id, "assistant", reply, uid)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 会话落库失败 thread={thread_id}: {exc}")


def _prefetch_knowledge(text: str) -> None:
    """★ 预热知识库检索：把用户这句话的向量**提前在后台算好**。

    检索原本是同步卡在回答的关键路径上的（`read_agent._llm_node` 每次调 LLM 前都要跑），
    而实测 `dashscope.aliyuncs.com` 首次 DNS 解析约 11s、每次嵌入请求 130~450ms。
    提前到「请求刚进来」时就起线程算，等 read_agent 真正需要时向量已在缓存里 ——
    这段网络耗时被藏到意图理解那次 LLM 调用的背后，用户无感。失败静默。
    """
    try:
        from utils import knowledge

        threading.Thread(target=knowledge.prefetch, args=(text,),
                         daemon=True, name="kb-prefetch").start()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 知识库预热失败（不影响回答）: {exc}")


def _knowledge_warmup() -> None:
    """服务启动时预热知识库（DNS + 连接 + 向量表）；内部开线程，立即返回。"""
    try:
        from utils import knowledge

        knowledge.warmup()
        print("[knowledge] 已后台预热检索（DNS + 向量表）")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 知识库预热挂载失败（不影响服务）: {exc}")


@app.post("/api/chat")
def chat(req: ChatReq, request: Request):
    _prefetch_knowledge(req.message)   # ★ 立刻起线程预热检索（别让嵌入网络耗时挡在回答路径上）
    # ★ 身份贯穿：token → uid → 绑定 driver（跨用户 403）→ 进程内上下文。
    #   以前 _get_driver 不传 uid、chat 也从不设置 security 上下文 —— Agent 的
    #   工具调用/写流程全落到 DEFAULT_USER_ID，B 在对话里查的、动的都是 A 的资产。
    token = security.extract_token(
        request.headers.get("authorization"), request.headers.get("x-user-token")
    )
    uid = _resolve_uid(request)
    _assert_conversation_owner(req.thread_id, uid)
    driver, lock = _get_driver(req.thread_id, uid)
    dh_on = _dh_on(request)
    _dh_interrupt()
    with lock:
        try:
            with _login_ctx(uid, token):
                reply = driver.chat(req.message)
        except Exception as exc:  # noqa: BLE001  LLM/银行服务的错误统一透出给前端
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    payload = _reply_payload(reply, driver)
    # 审计项 M1：真实对话落库（此前只进 LangGraph 的 SQLite checkpoint，MySQL 完全不知情）
    # 落库失败只记日志、不影响本轮对话，避免 DB 抖动把聊天整条链路打断
    _persist_chat(req.thread_id, req.message, reply, uid)
    # 数字柜员联动：本轮回复合口（未开 / Fay 未启动 → 静默跳过）
    digital_human.maybe_speak_reply(payload.get("reply"), dh_on)

    return payload


@app.post("/api/chat/stream")
async def chat_stream(req: ChatReq, request: Request):
    """SSE 聊天流（POST /api/chat 的事件版，协议同构，POST 原样保留作兜底）。

    事件帧（每帧 data: <json>\\n\\n）：
      {"type":"tool",        "id","name":"mcp_xxx","label","realtime":true}
                                  —— MCP 外部工具开始：前端追加"实时"胶囊 + 转圈
      {"type":"status",      "text":"正在查询外部实时数据，约需数秒..."}
                                  —— MCP 调用超过 8s 仍未返回时推送（前端小状态条）
      {"type":"tool_result", "id","name","label","elapsed_s"}
                                  —— MCP 工具返回：胶囊转圈→✓、状态条消失
      {"type":"done",        "payload":{kind,reply,nav,suspended,...}}
                                  —— 最终回复：payload 结构与 POST /api/chat 完全一致
      {"type":"error",       "detail":"..."}
                                  —— 运行异常（对应 POST 的 HTTP 500 语义）

    仅当 read_agent 本轮真的调用了 mcp_ 工具时才会出现 tool/status 帧；
    纯本地查询时管道只推一个 done —— 对既有前端行为零干扰。
    """
    _prefetch_knowledge(req.message)   # ★ 立刻起线程预热检索（别让嵌入网络耗时挡在回答路径上）
    token = security.extract_token(
        request.headers.get("authorization"), request.headers.get("x-user-token")
    )
    uid = _resolve_uid(request)
    _assert_conversation_owner(req.thread_id, uid)
    driver, lock = _get_driver(req.thread_id, uid)
    dh_on = _dh_on(request)
    _dh_interrupt()
    return _sse_response(
        thread_id=req.thread_id, driver=driver, lock=lock, uid=uid, token=token,
        runner=lambda d: d.chat(req.message),
        after=lambda reply, payload: _persist_chat(req.thread_id, req.message, reply, uid),
        dh_on=dh_on,
    )


# --------------------------------------------------------------------------- #
#  SSE 管道（唯一实现）：/api/chat/stream、/api/chat/submit-task、/api/chat/resume
#  三个端点共用 —— 事件协议因此天然同构，前端一个 hook 全接。
# --------------------------------------------------------------------------- #
def _sse_response(*, thread_id: str, driver: MainDriver, lock: threading.Lock,
                  uid: int, token: str | None, runner, after=None,
                  first_events: tuple[dict, ...] = (), dh_on: bool = False) -> StreamingResponse:
    """把「一次同步 driver 调用」包成 SSE 流。

    runner(driver) -> reply 文本；after(reply, payload) 做落库等收尾（异常只记日志）。
    first_events：在 worker 启动前先发的帧（如告知前端本轮用的 thread_id）。
    """
    # 事件队列：run_server 工作线程（同步、持有 driver 锁）与 SSE 生成器之间桥接
    events: queue.Queue = queue.Queue()
    closed = threading.Event()

    def _emit(ev: dict) -> None:
        if closed.is_set():
            return
        # ★作用域过滤：只转发本轮（scope == 本 thread）或未标记 scope 的事件。
        #   否则两个用户同时在跑，A 的节点进度会串到 B 的 SSE 里。
        scope = ev.get("scope")
        if scope and scope != thread_id:
            return
        events.put_nowait(ev)

    bus_token = event_bus.subscribe(_emit)

    def _worker() -> None:
        try:
            with lock:
                # scope_ctx：让 write_flow / MCP 工具 emit 的事件带上本 thread 标记
                with _login_ctx(uid, token), event_bus.scope_ctx(thread_id):
                    reply = runner(driver)
            payload = _reply_payload(reply, driver)
            _emit({"type": "done", "payload": payload})
            if after is not None:
                try:
                    after(reply, payload)
                except Exception as exc:  # noqa: BLE001  落库失败不影响已完成的业务
                    print(f"[warn] SSE 收尾失败 thread={thread_id}: {exc}")
        except Exception as exc:  # noqa: BLE001
            _emit({"type": "error", "detail": str(exc)})
        finally:
            # 必须用 put_nowait：客户端断开后无人消费队列，阻塞 put 会让 worker
            # 卡死并一直占着 driver 锁（后续同 thread 的请求全被饿死）
            try:
                events.put_nowait(None)
            except Exception:  # noqa: BLE001
                pass
            event_bus.unsubscribe(bus_token)
            closed.set()

    threading.Thread(target=_worker, daemon=True, name=f"chat-stream-{thread_id}").start()

    async def _event_gen():
        try:
            for ev in first_events:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            while True:
                # ★ 1s 超时的队列读取（而非无限阻塞的 events.get）：
                #   空窗期推一帧 SSE 注释保活（`: keepalive`，前端解析器按规范跳过），
                #   让「后端真的在算」和「链路已死」在前端可区分，同时避开反向代理空闲超时。
                try:
                    item = await asyncio.to_thread(events.get, True, 1.0)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                # 数字柜员联动：final reply 帧（done.payload.reply）触发 Fay 开口；
                # 未开 / Fay 未配置 / 异常全部静默跳过，绝不阻塞事件流。
                digital_human.maybe_speak_event(item, dh_on)
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            # 客户端断开/生成器被取消：通知 worker 别再往队列塞（其自身会收尾退出）
            closed.set()

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
#  结构化任务提交（转账页表单）+ 中断恢复
#
#  ★铁律：表单只负责收集参数。这里不会、也没有任何路径去直接调 /api/transfer，
#    提交后一律走主图 → write_flow：risk_check →（需要则）verify → confirm → execute。
#    不点「确认」就绝不会扣款。
# --------------------------------------------------------------------------- #
class SubmitTaskReq(BaseModel):
    conversation_id: str = "web"
    task: dict


class ResumeReq(BaseModel):
    conversation_id: str = "web"
    resume: str
    thread_id: str | None = None


_ACTION_CN = {
    "transfer": "转账", "buy_product": "购买理财", "redeem": "赎回持仓",
    "freeze_card": "冻结银行卡", "unfreeze_card": "解冻银行卡",
    "cancel_subscription": "取消订阅",
}


def _assert_conversation_owner(conv_id: str, uid: int) -> None:
    """会话归属校验：不存在则建（归当前用户），属于别人 → 403。"""
    try:
        owner = profile_db.conversation_owner(conv_id)
    except Exception as exc:  # noqa: BLE001  DB 抖动不该把提交打死
        print(f"[warn] 会话归属校验失败 conv={conv_id}: {exc}")
        return
    if owner is None:
        try:
            profile_db.ensure_conversation(conv_id, uid)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 会话创建失败 conv={conv_id}: {exc}")
        return
    if owner != uid:
        raise HTTPException(status_code=403, detail="该会话不属于当前用户")


def _payee_label(payee_id, request: Request) -> str:
    """取收款人昵称用于对话留痕文案（拿不到就退回 #ID，不影响主流程）。"""
    try:
        r = requests.get(
            f"{config.BANK_API_BASE}/api/payees",
            headers=_bank_headers(request), timeout=3,
        ).json()
        for p in r.get("payees", []):
            if str(p.get("id")) == str(payee_id):
                return p.get("nickname") or f"收款人#{payee_id}"
    except Exception:  # noqa: BLE001
        pass
    return f"收款人#{payee_id}"


def _task_user_text(task: dict, request: Request) -> str:
    """把结构化任务渲染成一条人类可读的用户消息（进 chat_messages，对话里可见）。"""
    action = task.get("action")
    params = task.get("params") or {}
    if action == "transfer":
        who = _payee_label(params.get("payee_id"), request) if params.get("payee_id") else (params.get("payee") or "")
        amt = float(params.get("amount") or 0)
        text = f"[转账表单] 向{who}转账 ¥{amt:,.2f}"
        if params.get("remark"):
            text += f"（备注：{params['remark']}）"
        return text
    return f"[表单提交] {_ACTION_CN.get(action, action)}"


def _clean_form_thread(conv_id: str, uid: int) -> tuple[str, MainDriver, threading.Lock]:
    """给表单提交挑一条干净的 LangGraph 线程。

    ★为什么不直接用会话线程：会话线程可能正挂在某个对话内的中断上（比如用户在
      聊天里问到一半就跑去转账页）。往挂起的线程灌新输入，LangGraph 会同时重放
      挂起节点与新一轮，状态直接错乱。所以表单流程走独立命名空间 `<conv>::form*`：
      - 与对话线程互不干扰，聊天里的半截流程原样保留；
      - chat_messages 仍写回同一个 conversation_id，所以对话里照样看到这笔转账。
      若上一次表单流程被用户中途丢弃（还挂着），就顺次换 form2 / form3…
    """
    seq = 0
    while seq < 20:
        tid = f"{conv_id}::form" + (str(seq + 1) if seq else "")
        driver, lock = _get_driver(tid, uid)
        if not driver.suspended():
            return tid, driver, lock
        seq += 1
    raise HTTPException(status_code=429, detail="待处理的转账流程过多，请先完成或取消已有流程")


@app.post("/api/chat/submit-task")
def submit_task(req: SubmitTaskReq, request: Request):
    """结构化任务提交：表单 → 主图 → write_flow 安全流程（SSE，协议同 chat/stream）。

    事件帧：
      {"type":"thread","thread_id":"<conv>::form"}   —— 本轮线程（resume 时带回来）
      {"type":"node","node","label","phase","detail"} —— write_flow 节点进度
      {"type":"done","payload":{kind:"verify"|"confirm"|"text", ...}}
                                                     —— 中断问题 / 最终回执
      {"type":"error","detail":"..."}
    """
    from agent.main_graph import normalize_submit  # 局部导入避免顶层循环依赖

    token = security.extract_token(
        request.headers.get("authorization"), request.headers.get("x-user-token")
    )
    uid = _resolve_uid(request)
    conv_id = (req.conversation_id or "web").strip() or "web"
    _assert_conversation_owner(conv_id, uid)
    dh_on = _dh_on(request)
    _dh_interrupt()

    task = normalize_submit(req.task)
    if task is None:
        raise HTTPException(status_code=400, detail="非法的任务参数（type 必须为 write，action 需在白名单内）")

    user_text = _task_user_text(task, request)
    # 用户消息先落库：即便后面 LLM/银行服务抖动，对话里也留下「他确实提交过这笔」
    try:
        profile_db.save_message(conv_id, "user", user_text, uid, node_trace=[])
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 表单提交消息落库失败 conv={conv_id}: {exc}")

    tid, driver, lock = _clean_form_thread(conv_id, uid)

    def _after(reply: str, payload: dict) -> None:
        # 只在流程走完（不再挂起）时写 assistant 消息：中断问题属页面交互，
        # 不该把「请输入验证码」这类过程态灌进对话历史。
        if not payload.get("suspended") and reply:
            profile_db.save_message(conv_id, "assistant", reply, uid, node_trace=[])

    return _sse_response(
        thread_id=tid, driver=driver, lock=lock, uid=uid, token=token,
        runner=lambda d: d.submit(task, user_text),
        after=_after,
        first_events=({"type": "thread", "thread_id": tid, "conversation_id": conv_id},),
        dh_on=dh_on,
    )


@app.post("/api/chat/resume")
def chat_resume(req: ResumeReq, request: Request):
    """恢复挂起的中断（验证码 / 确认）——现有驱动逻辑的 HTTP 封装，同样走 SSE。"""
    token = security.extract_token(
        request.headers.get("authorization"), request.headers.get("x-user-token")
    )
    uid = _resolve_uid(request)
    conv_id = (req.conversation_id or "web").strip() or "web"
    _assert_conversation_owner(conv_id, uid)
    dh_on = _dh_on(request)
    _dh_interrupt()

    tid = _pending_thread(conv_id, uid, req.thread_id)
    if tid is None:
        raise HTTPException(status_code=409, detail="当前没有待恢复的流程（可能已完成或已取消）")
    driver, lock = _get_driver(tid, uid)
    text = (req.resume or "").strip()

    def _after(reply: str, payload: dict) -> None:
        # 验证码只是过程凭据，不入库；「确认 / 取消」这类决策要留在对话里
        if text and not re.fullmatch(r"\d{4,8}", text):
            profile_db.save_message(conv_id, "user", text, uid, node_trace=[])
        if not payload.get("suspended") and reply:
            profile_db.save_message(conv_id, "assistant", reply, uid, node_trace=[])

    return _sse_response(
        thread_id=tid, driver=driver, lock=lock, uid=uid, token=token,
        runner=lambda d: d.resume(text),
        after=_after,
        first_events=({"type": "thread", "thread_id": tid, "conversation_id": conv_id},),
        dh_on=dh_on,
    )


def _pending_thread(conv_id: str, uid: int, hint: str | None = None) -> str | None:
    """找出该会话下正挂着中断的线程：优先前端带回的 thread_id，其次会话线程与表单线程。"""
    candidates: list[str] = []
    if hint:
        candidates.append(hint)
    candidates.append(conv_id)
    with _registry_lock:
        candidates += [t for t in _drivers if t.startswith(f"{conv_id}::form")]
    seen: set[str] = set()
    for tid in candidates:
        if tid in seen:
            continue
        seen.add(tid)
        # 只认属于当前用户的线程；hint 可能来自服务重启前，_get_driver 会按 checkpoint 重建
        try:
            driver, _ = _get_driver(tid, uid)
        except HTTPException:
            continue
        try:
            if driver.suspended():
                return tid
        except Exception:  # noqa: BLE001
            continue
    return None


@app.get("/api/state")
def state(reveal: str = "", request: Request = None):
    """代理 mock_bank 的余额与操作日志；银行服务未启动时优雅降级。

    ★ 按当前登录用户取数：以前四处请求全用 config.DEFAULT_USER_ID，
      导致 B 登录后在侧边栏看到的仍是 A 的余额、A 的操作留痕。

    ★ A1 展示安全：默认（reveal 未传）不返回任何真值金额字段 ——
      balance / today_expense / month_expense / accounts[].balance 一律缺省，
      顶栏初始请求在 Network 面板里看不到真值；主动查询（reveal=1）才下发。
    """
    uid = _resolve_uid(request)
    headers = _bank_headers(request)
    do_reveal = str(reveal).lower() in ("1", "true", "yes")
    out = {"bank_online": False, "balance": None, "accounts": [], "logs": []}
    try:
        r = requests.get(
            f"{config.BANK_API_BASE}/api/balance",
            timeout=3, headers=headers,
        )
        data = r.json()
        if data.get("ok"):
            out.update(bank_online=True, accounts=data["accounts"])
            if do_reveal:
                out["balance"] = float(data["total_balance"])
            else:
                # 脱敏态：账户列表只给名字，绝不带余额
                out["accounts"] = [
                    {k: a.get(k) for k in ("id", "name", "account_type") if k in a}
                    for a in data["accounts"]
                ]
    except Exception:  # noqa: BLE001
        pass
    # 今日 / 本月支出：来自真实流水（银行服务离线时留空，前端显示 —）
    # ★ A1：只在 reveal=1 时下发；脱敏态连"0"都不给
    if do_reveal:
        try:
            today = date.today()
            r = requests.get(
                f"{config.BANK_API_BASE}/api/transactions",
                params={"start": today.isoformat(), "end": today.isoformat()},
                timeout=3, headers=headers,
            )
            d = r.json()
            if d.get("ok"):
                out["today_expense"] = float(d["summary"]["total_expense"])
        except Exception:  # noqa: BLE001
            pass
        try:
            today = date.today()
            month_start = today.replace(day=1)
            r = requests.get(
                f"{config.BANK_API_BASE}/api/transactions",
                params={"start": month_start.isoformat(), "end": today.isoformat()},
                timeout=3, headers=headers,
            )
            d = r.json()
            if d.get("ok"):
                out["month_expense"] = float(d["summary"]["total_expense"])
        except Exception:  # noqa: BLE001
            pass
    _OP_CN = {
        "transfer": "转账",
        "buy_product": "购买理财",
        "redeem": "赎回持仓",
        "freeze_card": "冻结银行卡",
        "unfreeze_card": "解冻银行卡",
        "cancel_subscription": "取消订阅",
        "verify_send": "发送验证码",
        "verify_check": "校验验证码",
        "add_payee": "新增收款人",
    }
    _RES_CN = {"success": "成功", "failed": "失败"}
    try:
        r = requests.get(
            f"{config.BANK_API_BASE}/api/operation_logs",
            params={"limit": 15}, timeout=3, headers=headers,
        )
        data = r.json()
        if data.get("ok"):
            out["logs"] = [
                {
                    "time": str(row.get("created_at", ""))[11:16] or str(row.get("created_at", "")),
                    "op": _OP_CN.get(row.get("action"), row.get("action", "")),
                    "result": _RES_CN.get(row.get("result"), row.get("result", "")),
                }
                for row in reversed(data.get("logs", []))
            ]
    except Exception:  # noqa: BLE001
        pass
    return out










# 其余 /api/* 一律代理到 Mock 银行（账单 / 理财 / 卡片 / 订阅 / 安全中心 等页面的数据接口）
#   —— /api/chat /api/state /api/auth/* /api/profile /api/conversations/* 都有专门路由且定义在前，
#      会优先匹配，不会被此兜底拦截。
#
# ★ 两个关键点：
#   1) 支持写方法（POST/PUT/DELETE）—— 以前只有 GET 代理，前端"申请新卡 / 添加订阅"
#      这类写操作根本到不了银行服务，会被静态挂载吞成 404。
#   2) 必须透传 Authorization —— 否则 mock_bank 解析不到 user_id，
#      所有请求都落回默认演示账号，前端带了 token 也照样串号。
# --------------------------------------------------------------------------- #
#  写操作闸门（代理层）：前端页面直接打 mock_bank 的写接口（卡片/订阅/理财/赎回/转账）
#  若用户未绑定手机号，统一拦截返回 403 —— 读接口（balance/transactions/...）不拦。
#  ★ 与 write_flow.phone_guard 双保险：对话链路走 phone_guard，页面直连走本层。
# --------------------------------------------------------------------------- #
_WRITE_PATH_PREFIXES = (
    "cards/apply", "cards/adjust_limit", "cards/freeze", "cards/unfreeze",
    "accounts/transfer-internal",
    "subscriptions/contract", "subscriptions/discover/confirm", "subscriptions/discover/ignore",
    "products/buy", "redeem", "payees/create", "transfer",
)


def _is_write_api(path: str, method: str) -> bool:
    if method not in ("POST", "PUT", "DELETE"):
        return False
    # 验证码 / 健康检查等基础设施绝不拦（绑定手机本身就要发码）
    if path in ("verify/send", "verify/check", "health"):
        return False
    if path in _WRITE_PATH_PREFIXES:
        return True
    # 带 ID 的资源变更：cards/{id}（DELETE）、subscriptions/{id}[/{cancel|confirm|ignore}]
    if re.fullmatch(r"cards/\d+", path) and method == "DELETE":
        return True
    if re.fullmatch(r"subscriptions/\d+(/(cancel|confirm|ignore))?", path):
        return True
    return False




# --------------------------------------------------------------------------- #
#  P2.1：路由分域（routes/*）—— 拆分只为可维护性，行为与路由顺序保持不变。
#  ★ 注册顺序 = 匹配顺序：具体路由必须排在下方兜底代理 /api/{path:path} 之前。
#    本块紧邻兜底代理上方，使 include 出来的路由仍排在 chat 之后、代理之前。
# --------------------------------------------------------------------------- #
from routes import account as _routes_account, wallet as _routes_wallet
from routes import admin as _routes_admin, runtime as _routes_runtime

app.include_router(_routes_account.router)
app.include_router(_routes_wallet.router)
app.include_router(_routes_admin.router)
app.include_router(_routes_runtime.router)


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_api(path: str, request: Request):
    target = f"{config.BANK_API_BASE}/api/{path}"
    if request.url.query:
        target += "?" + request.url.query
    body = await request.body()
    # 写操作闸门：未绑定手机号直接拦截（读接口与验证码接口放行）
    if _is_write_api(path, request.method):
        try:
            if not profile_db.get_phone_bound(_resolve_uid(request)):
                return JSONResponse(
                    {"ok": False, "error": "请先绑定手机号", "code": "phone_unbound"},
                    status_code=403,
                )
        except Exception:  # noqa: BLE001  DB 不可用时放行，避免误伤（银行侧仍有校验）
            pass
    try:
        r = requests.request(
            request.method,
            target,
            data=body or None,
            headers=_bank_headers(request),
            timeout=8,
        )
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )
    except Exception:  # noqa: BLE001  银行服务离线 → 502，前端 useApi 会显示兜底态
        return JSONResponse({"ok": False, "error": "银行服务暂不可用"}, status_code=502)


# --------------------------------------------------------------------------- #
#  数字柜员前端反向代理：/fay/* → FAY_FRONTEND_URL（同源，避开 CORS 与混合端口）
#  仅 HTTP 透传；Fay 核心 WS 推送由前端直连 localhost，不经此代理。
#  离线 / 未配置 → 返回「数字柜员离线」占位页，iframe 据此显示占位文字。
# --------------------------------------------------------------------------- #
_DH_OFFLINE_HTML = (
    "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
    "<title>数字柜员离线</title><style>html,body{margin:0;height:100%;"
    "background:#0A0F18;color:#9aa4b2;font-family:system-ui,'Microsoft YaHei',sans-serif;"
    "display:flex;align-items:center;justify-content:center;text-align:center}</style></head>"
    "<body><div><div style=\"font-size:14px;letter-spacing:3px\">数字柜员离线</div>"
    "<div style=\"font-size:12px;margin-top:8px;opacity:.6\">Fay 渲染端未启动</div>"
    "</div></body></html>"
)


@app.get("/fay")
async def fay_root_redirect():
    return RedirectResponse(url="/fay/")


@app.api_route("/fay/{path:path}", methods=["GET", "HEAD", "POST", "OPTIONS"])
async def fay_proxy(path: str, request: Request):
    """把 /fay/* 透传到 Fay 前端；HTML 注入 <base href="/fay/"> 修正相对资源前缀。"""
    target = digital_human.proxy(
        path, request.method,
        params=dict(request.query_params),
        headers={"User-Agent": "xiaowei-fay-proxy"},
    )
    if target is None or target.status_code >= 400:
        if target is not None:
            target.close()
        return HTMLResponse(_DH_OFFLINE_HTML, status_code=502)

    content_type = target.headers.get("Content-Type", "text/html; charset=utf-8")
    _skip = {"transfer-encoding", "content-length", "connection", "keep-alive",
             "server", "content-encoding"}
    fwd = {k: v for k, v in target.headers.items() if k.lower() not in _skip}

    def _body_iter():
        try:
            for chunk in target.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                target.close()
            except Exception:  # noqa: BLE001
                pass

    # HTML：把根绝对路径改为 /fay/ 前缀（Fay 前端多用绝对 /static、/api），
    # 否则经子路径代理时会打到 8178 根而 404；再注入 <base> 兜底相对路径。
    if "text/html" in content_type:
        html = b"".join(_body_iter()).decode("utf-8", "replace")
        # src="/xxx" / href="/xxx" / action="/xxx" → 加 /fay；跳过 // 与已是 /fay/ 的
        html = re.sub(r'(src|href|action)="/(?!/|fay/)', r'\1="/fay/', html)
        html = re.sub(r"url\(/(?!/|fay/)", "url(/fay/", html)
        for tag in ("<head>", "<HEAD>"):
            if tag in html:
                html = html.replace(tag, f'{tag}\n<base href="/fay/">', 1)
                break
        return HTMLResponse(html, media_type="text/html; charset=utf-8",
                            status_code=target.status_code)

    return StreamingResponse(_body_iter(), media_type=content_type,
                             status_code=target.status_code, headers=fwd)


# 静态页面（挂在最后，API 路由优先）
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


def _bank_health() -> bool:
    try:
        return requests.get(f"{config.BANK_API_BASE}/api/health", timeout=2).ok
    except Exception:  # noqa: BLE001
        return False


# 启动即补齐认证相关表结构（幂等），避免首次登录 500
# ★ 安全修复（审计 P0.1）：生产环境若仍用默认 AUTH_SECRET / 默认管理员密码，直接拒绝启动，
#   防止 token 伪造与默认凭据在生产暴露。该检查不被下方 try 捕获（刻意 fail-fast）。
config.validate_auth_config()
try:
    profile_db.ensure_auth_schema()
    profile_db.ensure_admin_account()   # C5：管理员种子账号 admin / admin123
except Exception as _exc:  # noqa: BLE001
    print("[warn] ensure_auth_schema 失败（认证功能可能不可用）:", _exc)


# --------------------------------------------------------------------------- #
#  启动自检（后台任务，不阻塞启动）：连接百炼 MCP + 发现工具 + 打印清单
# --------------------------------------------------------------------------- #
def run_mcp_startup_selfcheck() -> None:
    """后台线程跑 MCP 自检：成功 → 打印工具清单并刷新 read_agent 工具快照；
    失败（未配 key / 断网 / 握手失败）仅告警 —— TOOLS 自动降级为纯本地。
    """
    def _job() -> None:
        result = mcp_client.ensure_connected_sync()
        status = result["status"]
        if result.get("ok"):
            tools = result["tools"]
            print(f"[mcp] 外部金融数据源已连接（东方财富妙想 · 百炼 MCP）："
                  f"发现 {len(tools)} 个只读工具")
            for t in tools:
                name = t.get("name", "")
                desc = (t.get("description") or "").strip().splitlines()
                first = (desc[0][:56] + "…") if desc and len(desc[0]) > 56 else (desc[0] if desc else "")
                print(f"      - mcp_{name:<28} {first}")
            try:
                from agent.tools import get_llm_tools
                get_llm_tools()   # 立即把 mcp_ 工具并入 read_agent 清单快照
            except Exception:  # noqa: BLE001
                pass
        else:
            reason = status.get("reason", "unknown")
            detail = f"（{status.get('last_error', '')}）" if status.get("last_error") else ""
            print(f"[mcp] 外部金融数据源未启用：{reason}{detail} → read_agent 保持纯本地工具清单")

    threading.Thread(target=_job, daemon=True, name="mcp-startup-selfcheck").start()


# --------------------------------------------------------------------------- #
#  P1.4：会话 driver 注册表 / SSE 流是**进程内**状态 —— 多 worker 下同一会话可能被路由到
#  不同进程，导致"确认 / 取消"接不上。这里只做启动告警（不改行为）；本工程按单 worker 部署。
# --------------------------------------------------------------------------- #
def warn_if_multiple_workers() -> None:
    import os as _os
    import sys as _sys
    n = _os.environ.get("WEB_CONCURRENCY")
    if not n:
        for _i, _a in enumerate(_sys.argv):
            if _a == "--workers" and _i + 1 < len(_sys.argv):
                n = _sys.argv[_i + 1]
            elif _a.startswith("--workers="):
                n = _a.split("=", 1)[1]
    try:
        if n and int(n) > 1:
            print(f"[warn] 检测到 {n} 个 worker：会话 driver 注册表 / SSE 流为进程内状态，"
                  f"同一会话跨 worker 可能接不上（确认/取消）。建议单 worker 运行。")
    except Exception:  # noqa: BLE001
        pass


# 挂 lifespan：uvicorn（含 `uvicorn run_server:app` 方式）启动时触发一次自检
try:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        warn_if_multiple_workers()    # P1.4：多 worker 会话状态告警
        run_mcp_startup_selfcheck()   # 内部开线程，立即返回，不阻塞启动
        _knowledge_warmup()           # ★ 预热 DNS + 向量表（内部开线程）——实测首次解析要 ~11s
        scheduler.start()             # P1.2：独立调度（订阅到期补扣 + 跨行到期清算）
        try:
            yield
        finally:
            scheduler.stop()

    app.router.lifespan_context = _lifespan
except Exception as _exc:  # noqa: BLE001
    print("[warn] MCP 启动自检挂载失败（不影响服务）:", _exc)


if __name__ == "__main__":
    key_state = "已配置" if config.DEEPSEEK_API_KEY else "未配置（调用 LLM 时会报错）"
    mcp_state = ("已配置" if config.DASHSCOPE_API_KEY
                 else "未配置（外部金融数据源停用，TOOLS 为纯本地）")
    print("=" * 66)
    print(" 小微 · AI 银行助手  Web 服务")
    print(f" 页面地址    : http://127.0.0.1:{SERVER_PORT}")
    print(f" 模型        : {config.DEEPSEEK_MODEL}（API Key {key_state}）")
    print(f" 银行服务    : {config.BANK_API_BASE}（{'在线' if _bank_health() else '离线，请先启动 mock_bank'}）")
    print(f" 外部金融数据: 东方财富妙想 · 百炼 MCP（DASHSCOPE_API_KEY {mcp_state}）")
    print(" 前端模式    : 后端可达 → 真实模式；不可达 → 自动回落静态演示流")
    print("=" * 66)
    uvicorn.run(app, host="127.0.0.1", port=SERVER_PORT, log_level="warning")
