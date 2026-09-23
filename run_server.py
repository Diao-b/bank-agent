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
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pymysql
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, JSONResponse, FileResponse, StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from mock_bank import db  # 连接收口到单一源（组 R）
import profile_db  # 用户画像数据层（MySQL bank_agent）
import rules  # C6：风控参数单源（system_config）
from agent.driver import MainDriver
from utils import event_bus, inbox, mcp_client, paypass, security
from utils import op_auth as _op_auth
from utils.user_provision import provision_new_user
from utils.digital_human import digital_human  # 数字柜员桥接层（Fay 渲染端，独立进程）
from utils import appearance as appearance_cfg  # 数字人形象配置（人物模型 + 背景）

WEB_DIR = Path(__file__).resolve().parent / "web"
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


class ProfileUpdate(BaseModel):
    real_name: str | None = None
    gender: str | None = None
    occupation: str | None = None
    preferences: dict | None = None


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


# --------------------------------------------------------------------------- #
# 认证（审计项 M3）：原先任意密码可登录、register 不建用户、token 无签名无过期。
#   现在：用户密码走 pbkdf2 哈希校验；token 为 HMAC 签名且 24h 过期；
#   register 真正落库；解析出的 user_id 用于画像 / 会话 / 聊天落库。
#   前端请求失败时会回落本地 mock（登录页 mockAuth 仍要求演示密码 123456）。
# --------------------------------------------------------------------------- #
class _AuthReq(BaseModel):
    username: str = ""
    password: str = ""
    confirm: str = ""


def _resolve_uid(req: Request) -> int:
    """从 Authorization / x-user-token 解析 user_id。

    ★ 安全修复（审计 R1/R2）：库内已是多真实用户，匿名回落已关闭
      （BANK_ALLOW_ANON=0）—— 无有效 token 一律 401；管理员锁定的账号
      连旧 token 也一并 403（否则"锁定"只挡新登录、旧会话还能营业 24h）。
    """
    h = req.headers.get("authorization") or req.headers.get("x-user-token") or ""
    if h.startswith("Bearer "):
        h = h[7:]
    token = h.strip()
    try:
        uid = security.resolve_user_id(token)
    except security.AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    if profile_db.get_user_locked(uid):
        raise HTTPException(status_code=403, detail="账号已被锁定，请联系管理员或柜台处理")
    return uid


def _bank_headers(req: Request | None) -> dict:
    """★ 转发到 Mock 银行时必须带上身份头。

    以前代理只转发 query，token 到不了 mock_bank —— 那边解析不到 user_id，
    所有用户看到的都是默认演示账号的数据。这是"前端带了 token 却仍然串号"的根因。
    """
    if req is None:
        return {}
    out = {}
    for key, value in req.headers.items():
        if key.lower() in ("authorization", "x-user-token", "content-type", "accept",
                           "x-op-token"):
            out[key] = value
    return out


# ---- 登录防爆破（审计 R5）：同账号连续失败 5 次锁 15 分钟（进程内计数）----
_LOGIN_FAILS: dict[str, dict] = {}
_LOGIN_MAX_FAILS, _LOGIN_LOCK_SECONDS = rules.LOGIN_MAX_FAILS, rules.LOGIN_LOCK_SECONDS


@app.post("/api/auth/login")
def auth_login(req: _AuthReq):
    import time as _time
    username = (req.username or "").strip()
    fail = _LOGIN_FAILS.get(username)
    if fail and fail["until"] > _time.time():
        wait = int((fail["until"] - _time.time()) / 60) + 1
        raise HTTPException(status_code=429, detail=f"失败次数过多，请约 {wait} 分钟后再试")
    res = profile_db.authenticate(username, req.password or "")
    if isinstance(res, dict) and res.get("error") == "locked":
        # C6：管理员锁定的账号 —— 明确拒绝（验收：锁定用户 → 该用户登录被拒）
        raise HTTPException(status_code=403, detail="账号已被锁定，请联系管理员或柜台处理")
    if not res:
        n = (fail["n"] + 1) if fail else 1
        if n >= _LOGIN_MAX_FAILS:
            _LOGIN_FAILS[username] = {"n": n, "until": _time.time() + _LOGIN_LOCK_SECONDS}
            raise HTTPException(status_code=429, detail="失败次数过多，账号已临时锁定 15 分钟")
        _LOGIN_FAILS[username] = {"n": n, "until": 0}
        raise HTTPException(status_code=401, detail=f"用户名或密码错误（还可尝试 {_LOGIN_MAX_FAILS - n} 次）")
    _LOGIN_FAILS.pop(username, None)
    if isinstance(res, dict) and res.get("error") == "locked":
        # C6：管理员锁定的账号 —— 明确拒绝（验收：锁定用户 → 该用户登录被拒）
        raise HTTPException(status_code=403, detail="账号已被锁定，请联系管理员或柜台处理")
    if not res:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    # C5：token payload 携带 role；admin 登录后前端据此默认跳 /admin
    role = profile_db.get_user_role(res["user_id"]) or "user"
    return {
        "ok": True,
        "token": profile_db.issue_token(res["user_id"], role=role),
        "user": {"username": res["username"], "user_id": res["user_id"], "role": role},
    }


@app.post("/api/auth/guest")
def auth_guest(req: _AuthReq):
    # ★ 安全修复（审计 P0.1）：演示游客通道是"认证后门"——无需任何凭证即为默认账号签发 token。
    #   仅开发态（ENV=dev 且 DEV_ALLOW_GUEST=1）可用；生产环境无条件关闭，返回 403。
    if not config.auth_guest_enabled():
        raise HTTPException(status_code=403, detail="演示游客通道已关闭，请使用账号登录")
    # 单账号演示：游客映射到默认账号（无需注册）
    uid = config.DEFAULT_USER_ID
    return {
        "ok": True,
        "token": profile_db.issue_token(uid),
        "user": {"username": "guest", "user_id": uid},
    }


@app.post("/api/auth/register")
def auth_register(req: _AuthReq):
    if not req.username or not req.password:
        raise HTTPException(status_code=400, detail="用户名和密码必填")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    res = profile_db.create_user(req.username, req.password)
    if not res:
        raise HTTPException(status_code=409, detail="用户名已存在")

    # ★ uid 复用兜底（2026-09-21）：MySQL 自增值一旦被复用，而上一个主人当初只被手工
    #   `DELETE FROM users`（业务数据没清），新账号名下就会残留别人的账户 / 卡 / 流水；
    #   更糟的是 provision_new_user 的幂等闸门（已有 accounts 就跳过）会**静默跳过初始化**，
    #   于是新用户直接继承死账号的全部数据。这里在「刚建号」这个唯一可信的时点先清残留。
    recycled = 0
    try:
        residue = profile_db.count_user_assets(res["user_id"])
        if residue:
            recycled = residue
            purged = profile_db.purge_user_assets(res["user_id"])
            print(f"[warn] uid={res['user_id']} 命中 uid 复用：上一个用户残留 {residue} 行，"
                  f"已清空 {purged['deleted_total']} 行后重新初始化")
            left = profile_db.count_user_assets(res["user_id"])
            if left:
                raise RuntimeError(f"残留未清干净（剩 {left} 行）")
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 用户 {res['user_id']} 残留清理失败：{exc}")
        raise HTTPException(status_code=500, detail=f"账号创建前的残留数据清理失败：{exc}") from exc

    # ★ 注册即开户：立刻给新用户一套独立的演示数据沙箱（账户 / 卡 / 收款人 / 流水 / 订阅）。
    #   以前 create_user 只 INSERT users，新用户登录后查余额为空、转账报"账户不存在"。
    #   provision_new_user 自带幂等闸门（已有 accounts 记录就跳过），重复注册不会脏数据。
    provision: dict = {}
    try:
        provision = provision_new_user(res["user_id"], req.username)
    except Exception as exc:  # noqa: BLE001
        # 初始化失败不回滚注册：账号已建、能登录，可事后用 scripts/provision_user.py 补。
        # 但必须报出来 —— 静默失败会让"新用户没数据"这个问题再次隐身。
        print(f"[error] 用户 {res['user_id']} 数据沙箱初始化失败：{exc}")
        provision = {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "token": profile_db.issue_token(res["user_id"]),
        "user": {"username": res["username"], "user_id": res["user_id"]},
        "provision": provision,
        # true → 该 uid 曾被占用（自增值复用），已清掉上一个用户的残留数据
        "uid_recycled": bool(recycled),
        "residue_purged": recycled,
    }


# --------------------------------------------------------------------------- #
# 用户画像 / 个性化问候语
#   当前登录用户优先取自签名 token（审计项 M3），缺省回落演示账号。
# --------------------------------------------------------------------------- #
@app.get("/api/profile")
def get_profile(req: Request):
    """返回当前用户的完整画像（含账户概览、用户名、头像占位）。"""
    payload = profile_db.get_profile_payload(_resolve_uid(req))
    if not payload:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, **payload}


@app.put("/api/profile")
def put_profile(req: ProfileUpdate, request: Request):
    """编辑可编辑字段；gender 变更自动重算 title_prefix；返回最新画像（含 title_prefix）。"""
    try:
        payload = profile_db.update_user_profile(
            _resolve_uid(request),
            real_name=req.real_name,
            gender=req.gender,
            occupation=req.occupation,
            preferences=req.preferences,
        )
    except ValueError as exc:
        # 安全不变式违规（如已实名用户改姓名）→ 明确 400，不静默吞掉
        raise HTTPException(status_code=400, detail=str(exc))
    if not payload:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, **payload}


# --------------------------------------------------------------------------- #
#  手机号绑定（渐进式账户：注册即可对话/浏览，绑手机后解锁写操作）
#   流程：bind-request（校验号码 + 发码 + 暂存 phone_pending）
#        → bind-confirm（校验码 + 落 phone + 写 operation_logs）
#   验证码复用既有基础设施（scene='phone_bind'），与转账验证码隔离。
# --------------------------------------------------------------------------- #
class _PhoneBindReq(BaseModel):
    phone: str | None = None
    code: str | None = None


@app.post("/api/profile/phone/bind-request")
def phone_bind_request(req: _PhoneBindReq, request: Request):
    """申请绑定手机号：校验 11 位、1 开头 → 暂存待绑定号 → 发 6 位验证码（5 分钟有效）。

    不回显验证码（收件箱就是"手机"）；只回 expires_in。
    """
    uid = _resolve_uid(request)
    phone = (req.phone or "").strip()
    if not re.fullmatch(r"1\d{10}", phone):
        return JSONResponse(
            {"ok": False, "error": "请输入正确的 11 位手机号（以 1 开头）"}, status_code=400
        )
    # 暂存待绑定号；确认成功才落到 phone 列
    with profile_db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET phone_pending=%s WHERE id=%s", (phone, uid))
        conn.commit()
    from utils import verify_codes
    try:
        r = verify_codes.issue_code(uid, scene="phone_bind", purpose="绑定手机")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"验证码发送失败：{exc}") from exc
    return {
        "ok": True,
        "expires_in": r["expires_in"],
        "message": r["message"],
    }


@app.post("/api/profile/phone/bind-confirm")
def phone_bind_confirm(req: _PhoneBindReq, request: Request):
    """确认绑定：校验验证码（scene='phone_bind'）→ 取暂存号落 phone → 写 operation_logs。"""
    uid = _resolve_uid(request)
    code = (req.code or "").strip()
    if not code:
        return JSONResponse({"ok": False, "error": "请输入验证码"}, status_code=400)
    from utils import verify_codes
    check = verify_codes.check_code(uid, code, scene="phone_bind")
    if not check.get("ok"):
        return JSONResponse(
            {"ok": False, "error": check.get("error", "验证码不正确")}, status_code=400
        )
    # 校验通过：取出暂存的待绑定号（scene 已限定，phone_pending 必然存在）
    with profile_db._conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT phone_pending FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
            pending = (row or {}).get("phone_pending") if row else None
            if not pending:
                return JSONResponse(
                    {"ok": False, "error": "绑定流程已失效，请重新获取验证码"}, status_code=400
                )
            masked = profile_db.mask_phone(pending)
            cur.execute(
                "UPDATE users SET phone=%s, phone_pending=NULL, phone_tail=%s WHERE id=%s",
                (pending, masked, uid),
            )
            db.log_operation(uid, "phone_bind", f"用户绑定手机号 {masked}", "success", cur=cur)
        conn.commit()
    payload = profile_db.get_profile_payload(uid)
    return {"ok": True, "message": "手机号绑定成功", **payload}



@app.get("/api/conversations/{conv_id}/greeting")
def conversation_greeting(conv_id: str, request: Request):
    """新会话首条问候语：首次生成并入库（role='assistant'、node_trace='[]'），
    历史会话直接返回已存问候（幂等，不重复生成）。"""
    text = profile_db.get_or_create_greeting(conv_id, _resolve_uid(request))
    return {"ok": True, "greeting": text, "conversation_id": conv_id}


# --------------------------------------------------------------------------- #
# 会话列表 / 历史回读（审计项 M2）
#   此前这三个能力完全不存在（实测 /api/conversations 等一律 404），
#   对话只活在前端 React 内存里，刷新即丢。
# --------------------------------------------------------------------------- #
@app.get("/api/conversations")
def list_conversations(limit: int = 50, request: Request = None):
    """当前用户的会话列表，按最近活跃倒序。"""
    uid = _resolve_uid(request) if request else config.DEFAULT_USER_ID
    try:
        return {"ok": True, "conversations": profile_db.list_conversations(uid, limit)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "conversations": []}


@app.get("/api/conversations/{conv_id}/messages")
def conversation_messages(conv_id: str, limit: int = 500, request: Request = None):
    """回读指定会话的完整消息（含 greeting），供刷新后恢复现场。

    ★ 归属过滤：只允许读当前用户名下的会话（防知道 conv_id 就扒别人对话）。
    """
    uid = _resolve_uid(request) if request else config.DEFAULT_USER_ID
    try:
        return {"ok": True, "conversation_id": conv_id,
                "messages": profile_db.get_conversation_messages(conv_id, limit, user_id=uid)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "messages": []}


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str, request: Request = None):
    """删除会话及其消息。返回是否命中；LangGraph 的 checkpoint 不在 MySQL，需另行清理。"""
    uid = _resolve_uid(request) if request else config.DEFAULT_USER_ID
    try:
        return {"ok": True, "deleted": profile_db.delete_conversation(conv_id, uid)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "deleted": False}


# 页面路由：/ 与 /login 与 /chat（定义在前，优先于静态挂载）
@app.get("/")
def page_root():
    # 首页即登录门户：运行程序后先到登录页（可选登录 / 游客模式），而非直接裸进聊天页
    return FileResponse(WEB_DIR / "login.html")


@app.get("/login")
def page_login():
    return FileResponse(WEB_DIR / "login.html")


@app.get("/chat")
def page_chat():
    return FileResponse(WEB_DIR / "index.html")


# --------------------------------------------------------------------------- #
# A1. 敏感金额遮蔽：/api/overview 默认脱敏（无任何真值字段）；
#     ?reveal=true 返回真实聚合（总资产=余额合计；今日/本月支出=流水聚合）。
#     ★ 后端全部 Decimal 计算；reveal 不留痕（主动查询不算"泄露事件"，
#       真正的告警留痕在风控/核验路径，见 utils/paypass._log）。
# --------------------------------------------------------------------------- #
@app.get("/api/overview")
def overview(reveal: str = "", request: Request = None):
    from decimal import Decimal
    uid = _resolve_uid(request)
    if not str(reveal).lower() in ("true", "1", "yes"):
        # ★ 脱敏响应：绝不出现任何真值字段（连 0.00 都不给，避免"¥0.00 假值"）
        return {
            "ok": True, "masked": True,
            "total_assets": None, "today_expense": None, "month_expense": None,
            "mask_text": "¥ ··· ··",
            "hint": "金额已脱敏，主动查询（reveal=true）才下发真实聚合",
        }
    # reveal=true：真实聚合（Decimal -> float 仅供 JSON 序列化）
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(balance), 0) AS total FROM accounts "
                    "WHERE user_id=%s AND COALESCE(account_type,'debit') <> 'credit'", (uid,)
                )
                total = Decimal(str((cur.fetchone() or {}).get("total") or 0))
                now = datetime.now()
                cur.execute(
                    "SELECT COALESCE(SUM(t.amount), 0) AS exp FROM transactions t "
                    "JOIN accounts a ON a.id = t.account_id "
                    "WHERE a.user_id=%s AND t.type='expense' AND t.date=CURDATE()",
                    (uid,),
                )
                today = Decimal(str((cur.fetchone() or {}).get("exp") or 0))
                cur.execute(
                    "SELECT COALESCE(SUM(t.amount), 0) AS exp FROM transactions t "
                    "JOIN accounts a ON a.id = t.account_id "
                    "WHERE a.user_id=%s AND t.type='expense' "
                    "AND t.date >= DATE_FORMAT(CURDATE(), '%%Y-%%m-01')",
                    (uid,),
                )
                month = Decimal(str((cur.fetchone() or {}).get("exp") or 0))
        return {
            "ok": True, "masked": False,
            "total_assets": float(total),
            "today_expense": float(today),
            "month_expense": float(month),
            "server_date": now.strftime("%Y-%m-%d"),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"聚合查询失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# C2. 收件箱 API（全部认证 + 用户隔离；不代理到 mock_bank，走本库 notifications）
#     收件箱轮询（顶栏 unread_count）不要用 SSE —— 现有 SSE 是对话流专用，
#     混用易出 bug；前端 5s 轮询最稳。
#     ★ 惰性 housekeeping：收件箱是"跨行到账 / 退票"通知的落点，轮询即触发
#       到期清算（与订阅扣款引擎同一惰性模式），用户打开收件箱就能看到结果。
# --------------------------------------------------------------------------- #
def _run_housekeeping(uid: int) -> None:
    """读接口顺带触发后台惰性任务（订阅补扣 + 跨行到期清算）。
    失败只告警，绝不阻断读接口。"""
    try:
        from utils import settlement_engine

        settlement_engine.process_housekeeping(uid)
    except Exception as exc:  # noqa: BLE001
        print("[warn] housekeeping 失败（不影响读接口）:", exc)


@app.get("/api/inbox")
def inbox_list(unread_only: str = "", limit: int = 50, request: Request = None):
    from utils import inbox as inbox_mod
    uid = _resolve_uid(request)
    _run_housekeeping(uid)
    try:
        items = inbox_mod.list_notifications(
            uid, unread_only=str(unread_only).lower() in ("1", "true", "yes"), limit=limit
        )
        return {"ok": True, "messages": items, "unread_count": inbox_mod.unread_count(uid)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"收件箱读取失败：{exc}") from exc


@app.get("/api/inbox/unread_count")
def inbox_unread_count(request: Request = None):
    from utils import inbox as inbox_mod
    uid = _resolve_uid(request)
    _run_housekeeping(uid)
    return {"ok": True, "count": inbox_mod.unread_count(uid)}


@app.post("/api/inbox/{notification_id}/read")
def inbox_mark_read(notification_id: int, request: Request = None):
    from utils import inbox as inbox_mod
    uid = _resolve_uid(request)
    hit = inbox_mod.mark_read(uid, notification_id)
    if not hit:
        raise HTTPException(status_code=404, detail="消息不存在")
    return {"ok": True, "unread_count": inbox_mod.unread_count(uid)}


@app.post("/api/inbox/read_all")
def inbox_mark_all_read(request: Request = None):
    from utils import inbox as inbox_mod
    uid = _resolve_uid(request)
    return {"ok": True, "marked": inbox_mod.mark_all_read(uid), "unread_count": 0}


# --------------------------------------------------------------------------- #
# C3. 验证码发码/校验收口到本服务（落库 + 收件箱双写同事务）
#     ★ 必须定义在兜底代理（/api/{path:path}）之前，否则会被代理吞掉。
#     mock_bank 的同名端点保留作兼容，但业务链路一律走这里。
# --------------------------------------------------------------------------- #
class _VerifySendReq(BaseModel):
    scene: str = "transfer"


class _VerifyCheckReq(BaseModel):
    code: str


_SCENE_PURPOSE = {
    "transfer": "转账安全验证",
    "buy_product": "理财购买安全验证",
    "redeem": "赎回安全验证",
    "freeze_card": "冻结银行卡安全验证",
    "unfreeze_card": "解冻银行卡安全验证",
    "cancel_subscription": "取消订阅安全验证",
    "realname": "实名认证",
    "phone_bind": "绑定手机",
}


@app.post("/api/verify/send")
def verify_send_v2(req: _VerifySendReq, request: Request = None):
    from utils import verify_codes
    # ★ 安全修复：本接口必须持有效登录态（此前无 token 会回落默认账号真发码），
    #   且 scene 必须在业务白名单内（防未知场景刷码写库）。
    h = (request.headers.get("authorization") or request.headers.get("x-user-token") or "")
    if h.startswith("Bearer "):
        h = h[7:]
    ident = profile_db.decode_token_full(h.strip())
    if not ident:
        raise HTTPException(status_code=401, detail="请先登录后再获取验证码")
    uid = ident[0]
    allowed_scenes = set(_SCENE_PURPOSE) | {"phone_bind", "internal_transfer"}
    if req.scene not in allowed_scenes:
        raise HTTPException(status_code=400, detail=f"非法验证码场景：{req.scene}")
    purpose = _SCENE_PURPOSE.get(req.scene, req.scene or "身份核验")
    try:
        r = verify_codes.issue_code(uid, scene=req.scene, purpose=purpose)
    except Exception as exc:  # noqa: BLE001
        # 双写（验证表+收件箱）任一失败 → 整体失败，绝不返回"已发送"
        raise HTTPException(status_code=500, detail=f"验证码发送失败：{exc}") from exc
    # 演示接口不再回显 code —— 收件箱就是"手机"，用户去收件箱取码
    return {"ok": True, "scene": req.scene, "expires_in": r["expires_in"],
            "notification_id": r["notification_id"], "message": r["message"]}


@app.post("/api/verify/check")
def verify_check_v2(req: _VerifyCheckReq, request: Request = None):
    from utils import verify_codes
    uid = _resolve_uid(request)
    r = verify_codes.check_code(uid, req.code)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "验证码不正确")}
    return {"ok": True, "verified": True}


# --------------------------------------------------------------------------- #
# B3. 支付密码：核验 / 设置
#     ★密码禁入任何持久化记录：核验成功只在进程内放置短 TTL "已通过"标记，
#       write_flow 的 verify 队列据此放行；密码本身不进对话、不进 checkpoint。
# --------------------------------------------------------------------------- #
class _PayPwdReq(BaseModel):
    password: str
    thread_id: str | None = None


@app.post("/api/pay/password/verify")
def pay_password_verify(req: _PayPwdReq, request: Request = None):
    from utils import paypass
    uid = _resolve_uid(request)
    r = paypass.verify(uid, req.password)
    return r


@app.post("/api/pay/password/set")
def pay_password_set(req: _PayPwdReq, request: Request = None):
    from utils import paypass
    uid = _resolve_uid(request)
    r = paypass.set_password(uid, req.password)
    if r.get("ok"):
        paypass.mark_passed(uid)  # 设置完直接视为已通过本次核验
    return r


@app.get("/api/pay/password/status")
def pay_password_status(request: Request = None):
    from utils import paypass
    uid = _resolve_uid(request)
    return {"ok": True, "has_password": paypass.has_password(uid), "locked": paypass.is_locked(uid)}


# --------------------------------------------------------------------------- #
# P0.2：一次性操作授权票据签发端点（op-auth token）
#   ★ 直连写端点（transfer / buy / redeem / freeze / ...）消费本票据；无有效票据 → 4xx。
#   ★ 本端点要求"已通过短信验证码（scene 匹配且未过期）+ 已通过支付密码"，
#     即 walk 真实的 verify/paypass 路径后才发证 —— 生产无任何绕过分支。
#   ★ write_flow 在图内核实通过后，直接在进程内调 _op_auth.issue_op_token 发证，
#     不经过本 HTTP 端点；本端点仅供对话流之外的"真实票据路径"测试 / 外部合规调用使用。
# --------------------------------------------------------------------------- #
class _OpAuthorizeReq(BaseModel):
    scene: str
    code: str | None = None  # 短信验证码（该 scene 需短信核实时必填；由调用方从收件箱取）


@app.post("/api/op/authorize")
def op_authorize(req: _OpAuthorizeReq, request: Request = None):
    uid = _resolve_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail="请先登录")
    # 1) 支付密码必须已通过（进程内标记，由 /api/pay/password/verify 设置）
    from utils import paypass
    if not paypass.has_passed(uid):
        raise HTTPException(
            status_code=401,
            detail="请先完成支付密码核验（/api/pay/password/verify）后再申请操作授权",
        )
    # 2) 该 scene 必须存在一条"已核验且未过期"的验证码（防绕过短信核验直接发证）
    from utils import verify_codes
    try:
        conn = db.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status, verified_at, code_hash FROM verification_codes "
                    "WHERE user_id=%s AND scene=%s AND status='verified' "
                    "AND verified_at > DATE_SUB(NOW(), INTERVAL 5 MINUTE) "
                    "ORDER BY id DESC LIMIT 1",
                    (int(uid), req.scene),
                )
                vc = cur.fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        vc = None
    if not vc:
        raise HTTPException(
            status_code=401,
            detail="本次操作缺少有效的短信验证码核验，请先获取并核验验证码",
        )
    # 2.5) 若调用方带来了验证码，额外比对哈希，确保"确实核验过这一条"
    if req.code:
        if verify_codes._hash(req.code.strip()) != vc["code_hash"]:
            raise HTTPException(status_code=401, detail="短信验证码与已核验记录不符")
    # 3) 签发一次性票据（单次有效、按 scene 绑定、短 TTL）
    r = _op_auth.issue_op_token(uid, req.scene)
    return {
        "ok": True,
        "token": r["token"],
        "expires_in": r["expires_in"],
        "scene": r["scene"],
    }


# --------------------------------------------------------------------------- #
# C3-验收#3：实名认证验证码同样进收件箱
#   旧版"实名"只是填个姓名文本框。现在实名要走验证码（收件箱送达），
#   核验通过才落 users.real_name —— 复用 C3 的双写同事务发码通道。
# --------------------------------------------------------------------------- #
class _RealnameReq(BaseModel):
    real_name: str
    code: str


@app.post("/api/realname/send_code")
def realname_send_code(request: Request = None):
    from utils import verify_codes
    uid = _resolve_uid(request)
    try:
        verify_codes.issue_code(uid, scene="realname", purpose="实名认证")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"验证码发送失败：{exc}") from exc
    return {"ok": True, "message": "验证码已发送至您的收件箱，5 分钟内有效"}


@app.post("/api/realname/confirm")
def realname_confirm(req: _RealnameReq, request: Request = None):
    from utils import verify_codes
    uid = _resolve_uid(request)
    name = (req.real_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写真实姓名")
    with db.get_conn() as conn:
        try:
            with conn.cursor() as cur:
                # ★ 拦截：实名认证一旦完成，禁止在线修改（敏感身份变更必须柜台办理）。
                #   即使前端隐藏了表单，端点也必须再挡一道，防止被直接打 API 绕过。
                cur.execute("SELECT realname_verified, real_name FROM users WHERE id=%s", (uid,))
                row = cur.fetchone()
                if row and int(row.get("realname_verified") or 0) == 1:
                    return {
                        "ok": False,
                        "error": "您已完成实名认证，姓名属于敏感身份信息，"
                                 "如需变更请携带有效证件前往柜台办理。",
                    }
                r = verify_codes.check_code(uid, req.code, scene="realname")
                if not r.get("ok"):
                    return {"ok": False, "error": r.get("error", "验证码不正确")}
                # 核验通过 → 落库 + 标记已认证（同一连接、同一事务）
                cur.execute(
                    "UPDATE users SET real_name=%s, realname_verified=1 WHERE id=%s",
                    (name, uid),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"ok": True, "real_name": name, "message": "实名认证完成"}


# --------------------------------------------------------------------------- #
# C5. 后台管理 /admin（只读管理视图）
#     鉴权铁律：先认证再验 role —— 401（未认证/无效 token）与 403（非管理员）
#     语义分开；admin_guard 不放行任何 user 角色。
# --------------------------------------------------------------------------- #
def _admin_guard(request: Request) -> int:
    """admin_guard：非 admin 一律 403；无效 token 401。返回 admin 的 user_id。"""
    token = security.extract_token(
        request.headers.get("authorization"), request.headers.get("x-user-token")
    )
    got = profile_db.decode_token_full(token)
    if not got:
        raise HTTPException(status_code=401, detail="登录态无效或已过期")
    uid, role = got
    # token 里的 role 只是快速通道；DB 复核为准（防旧 token 提权）
    db_role = profile_db.get_user_role(uid)
    if db_role != "admin" or role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return uid


@app.get("/api/admin/overview")
def admin_overview(request: Request = None):
    """概览统计：全部真实聚合（假数据问题的终结）。

    - 用户数：users 全量
    - 今日消息数：chat_messages 当日新增
    - 累计转账笔数与总金额：operation_logs action=transfer 且 result=success
    - 验证码发送数：verification_codes 总发送行数
    - 拦截/失败核验数：operation_logs result=failed（verify_check/transfer 等）
    """
    _admin_guard(request)
    from decimal import Decimal
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM users")
                users_total = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM chat_messages WHERE DATE(created_at)=CURDATE()"
                )
                today_messages = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM chat_messages"
                )
                messages_total = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(CAST(JSON_EXTRACT(detail, '$.amount') AS DECIMAL(18,2))), 0) AS amt "
                    "FROM operation_logs WHERE action IN ('transfer','transfer_out') AND result='success'"
                )
                row = cur.fetchone() or {}
                transfer_count = int(row.get("n") or 0)
                transfer_amount = Decimal(str(row.get("amt") or 0))
                cur.execute("SELECT COUNT(*) AS n FROM verification_codes")
                codes_sent = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM operation_logs WHERE result='failed'"
                )
                failed_total = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM notifications WHERE type='verify_code'"
                )
                inbox_codes = int((cur.fetchone() or {}).get("n") or 0)
        return {
            "ok": True,
            "users_total": users_total,
            "today_messages": today_messages,
            "messages_total": messages_total,
            "transfer_count": transfer_count,
            "transfer_amount": float(transfer_amount),
            "codes_sent": codes_sent,
            "inbox_codes": inbox_codes,
            "failed_total": failed_total,
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"统计聚合失败：{exc}") from exc


@app.get("/api/admin/users")
def admin_users(request: Request = None):
    """用户管理：列表（账号/称呼/role/实名状态/注册时间/资产概览）。
    ★ 管理员视图可见真实金额（页面顶部注明"管理员视图：含敏感数据"）。"""
    _admin_guard(request)
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT u.id, u.name, u.username, u.role, u.created_at, "
                    "COALESCE(u.real_name, '') AS real_name, "
                    "COALESCE(u.realname_verified, 0) AS realname_verified, "
                    "COALESCE(u.locked, 0) AS locked, "
                    "COALESCE((SELECT SUM(balance) FROM accounts a WHERE a.user_id=u.id "
                          "AND COALESCE(a.account_type,'debit') <> 'credit'), 0) AS assets "
                    "FROM users u ORDER BY u.id ASC LIMIT 500"
                )
                rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "user_id": int(r["id"]),
                "name": r.get("name"),
                "username": r.get("username") or ("user%s" % r["id"]),
                "role": r.get("role") or "user",
                "real_name": r.get("real_name") or "",
                # ★ 是否已完成正式核验：realname_verified=1（与 ③ 基本信息无关）
                "realnamed": bool(r.get("realname_verified")),
                "locked": bool(r.get("locked")),
                "created_at": str(r.get("created_at")),
                "assets": float(r.get("assets") or 0),
            })
        return {"ok": True, "users": out}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"用户列表失败：{exc}") from exc


@app.get("/api/admin/users/{user_id}")
def admin_user_detail(user_id: int, request: Request = None):
    """用户详情：账户/卡/订阅/持仓概览（只读）。"""
    _admin_guard(request)
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, username, role, created_at, real_name, "
                            "COALESCE(locked,0) AS locked FROM users WHERE id=%s",
                            (user_id,))
                user = cur.fetchone()
                if not user:
                    raise HTTPException(status_code=404, detail="用户不存在")
                def q(sql, args=()):
                    cur.execute(sql, args)
                    return cur.fetchall()
                accounts = q("SELECT id, name, balance, account_type FROM accounts WHERE user_id=%s", (user_id,))
                cards = q("SELECT id, bank, card_name, status FROM cards WHERE user_id=%s", (user_id,))
                subs = q("SELECT id, service_name, fee, cycle, status FROM subscriptions WHERE user_id=%s", (user_id,))
                holdings = q(
                    "SELECT h.id, p.name, h.amount, h.status FROM holdings h "
                    "LEFT JOIN products p ON p.id=h.product_id WHERE h.user_id=%s", (user_id,)
                )
        return {"ok": True, "user": {
            "user_id": int(user["id"]), "name": user.get("name"),
            "username": user.get("username") or ("user%s" % user["id"]),
            "role": user.get("role"), "created_at": str(user.get("created_at")),
            "real_name": user.get("real_name") or "",
            "locked": bool(user.get("locked")),
        },
            "accounts": [{**a, "balance": float(a["balance"])} for a in accounts],
            "cards": cards, "subscriptions": subs, "holdings": holdings,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"用户详情失败：{exc}") from exc


@app.get("/api/admin/verify-codes")
def admin_verify_codes(limit: int = 100, request: Request = None):
    """验证码记录：发送记录列表（用户/用途/时间/状态）。明文只在 /code 端点给。"""
    _admin_guard(request)
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT v.id, v.user_id, COALESCE(u.username, CONCAT('user', v.user_id)) AS username, "
                    "v.scene, v.status, v.attempts, v.expires_at, v.verified_at, v.created_at "
                    "FROM verification_codes v LEFT JOIN users u ON u.id = v.user_id "
                    "ORDER BY v.id DESC LIMIT %s", (max(1, min(int(limit), 500)),)
                )
                rows = cur.fetchall()
        now = datetime.now()
        out = []
        for r in rows:
            status = r.get("status")
            if status == "unused" and r.get("expires_at") and now > r["expires_at"]:
                status = "expired"
            out.append({
                "id": int(r["id"]), "user_id": int(r["user_id"]), "username": r.get("username"),
                "scene": r.get("scene"), "status": status,
                "attempts": int(r.get("attempts") or 0),
                "expires_at": str(r["expires_at"]) if r.get("expires_at") else None,
                "verified_at": str(r["verified_at"]) if r.get("verified_at") else None,
                "created_at": str(r.get("created_at")),
            })
        return {"ok": True, "records": out}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"验证码记录失败：{exc}") from exc


@app.get("/api/admin/verify-codes/{vc_id}/code")
def admin_verify_code_plain(vc_id: int, request: Request = None):
    """查看码（管理员可见明文）——真实银行客服后台即如此：受控后台视角可查码，
    用于客服核实"用户说收不到码"类工单；所有调用都落 admin 审计留痕。"""
    admin_uid = _admin_guard(request)
    try:
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, code_plain, status, expires_at FROM verification_codes WHERE id=%s",
                    (vc_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="记录不存在")
                db.log_operation(admin_uid, "admin_view_code", {"vc_id": vc_id},
                                 "success", cur=cur)
            conn.commit()
        return {"ok": True, "id": int(row["id"]), "code": row["code_plain"],
                "status": row["status"], "expires_at": str(row["expires_at"])}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"查看码失败：{exc}") from exc


@app.get("/api/admin/logs")
def admin_logs(user_id: int | None = None, action: str = "", result: str = "",
               limit: int = 200, request: Request = None):
    """操作日志：全局时间线（全用户所有 action），筛选器：用户/类型/结果。"""
    _admin_guard(request)
    try:
        sql = (
            "SELECT o.id, o.user_id, o.action, o.detail, o.result, o.created_at, "
            "COALESCE(u.username, CONCAT('user', o.user_id)) AS username "
            "FROM operation_logs o LEFT JOIN users u ON u.id = o.user_id WHERE 1=1"
        )
        args: list = []
        if user_id:
            sql += " AND o.user_id=%s"
            args.append(int(user_id))
        if action:
            sql += " AND o.action=%s"
            args.append(action)
        if result:
            sql += " AND o.result=%s"
            args.append(result)
        sql += " ORDER BY o.id DESC LIMIT %s"
        args.append(max(1, min(int(limit), 1000)))
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rows = cur.fetchall()
        return {"ok": True, "logs": [
            {
                "id": int(r["id"]), "user_id": int(r["user_id"]), "username": r.get("username"),
                "action": r.get("action"), "detail": r.get("detail"),
                "result": r.get("result"), "created_at": str(r.get("created_at")),
            } for r in rows
        ]}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"日志查询失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# C6. 可运营后台：管理端写操作 API
#  —— 三条铁律：
#     1) 每一次写操作都写 admin_logs（who / what / old→new / when）；
#     2) 可改范围严格限定在白名单（风控参数 / 用户登录与角色 / 产品目录 /
#        知识库同步 / 公告）—— 用户资产（余额 / 流水 / 持仓 / 订阅）
#        不设任何管理端修改接口，金融系统的管理员也不能动客户的钱；
#     3) 涉及 RAG 知识文档数字的风控参数，修改即打 pending_sync 标记，
#        知识文档人工确认更新后徽标才消失。
# --------------------------------------------------------------------------- #
import secrets as _secrets

from fastapi import Body

KNOWLEDGE_DIR = Path(config.BASE_DIR).parent / "docs" / "knowledge"


class _ConfigUpdate(BaseModel):
    value: str


class _UserLockReq(BaseModel):
    locked: bool


class _AppearanceUpdate(BaseModel):
    model: str
    background: str


class _UserRoleReq(BaseModel):
    role: str


class _ProductUpdate(BaseModel):
    name: str | None = None
    term: str | None = None
    risk_level: int | None = None
    annual_rate: float | None = None
    min_amount: float | None = None
    status: str | None = None      # on | off


class _AnnouncementReq(BaseModel):
    title: str
    content: str


def _log_admin(admin_id: int, action: str, target_type: str = "",
               target_id: str = "", detail: dict | None = None,
               pending_sync: bool = False) -> int:
    """管理员操作留痕（admin_logs）。所有管理端写操作必经此口。"""
    db.execute(
        "INSERT INTO admin_logs (admin_id, action, target_type, target_id, detail, pending_sync) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (int(admin_id), action, target_type, str(target_id),
         json.dumps(detail or {}, ensure_ascii=False, default=str),
         1 if pending_sync else 0),
    )
    row = db.fetch_one("SELECT LAST_INSERT_ID() AS id")
    return int(row["id"])


def _pending_sync_rows():
    """未确认的 RAG 同步义务（知识库"待同步"徽标数据源）。"""
    return db.fetch_all(
        "SELECT l.id, l.action, l.target_id, l.detail, l.created_at, l.admin_id, "
        "       u.username AS admin_name "
        "FROM admin_logs l LEFT JOIN users u ON u.id = l.admin_id "
        "WHERE l.pending_sync = 1 AND l.sync_done_at IS NULL "
        "ORDER BY l.id DESC LIMIT 200"
    )


# ---- 1) 风控参数（system_config 单源） ------------------------------------- #
@app.get("/api/admin/config")
def admin_config_list(request: Request = None):
    admin_uid = _admin_guard(request)
    items = rules.all_rules()
    for it in items:
        it["pending_sync"] = bool(db.fetch_one(
            "SELECT id FROM admin_logs WHERE target_type='config' AND target_id=%s "
            "AND pending_sync=1 AND sync_done_at IS NULL LIMIT 1", (it["key"],)))
    return {"ok": True, "config": items, "pending": _pending_sync_rows(),
            "actor": admin_uid}


@app.put("/api/admin/config/{key}")
def admin_config_update(key: str, req: _ConfigUpdate, request: Request = None):
    admin_uid = _admin_guard(request)
    spec = rules.RULE_SPECS.get(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知参数：{key}")
    try:
        result = rules.set_rule_value(key, req.value, updated_by=admin_uid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未知参数：{key}")
    # ★ 同步义务强制化：涉及知识文档数字的参数 → pending_sync 红标
    _log_admin(admin_uid, "config_update", "config", key,
               detail={"old": str(result["old"]), "new": str(result["new"]),
                       "label": spec["label"], "docs": spec["docs"]},
               pending_sync=bool(spec["knowledge"]))
    return {"ok": True, "key": key, "old": str(result["old"]), "new": str(result["new"]),
            "affects_knowledge": bool(spec["knowledge"]),
            "docs_ref": spec["docs"],
            "message": ("参数已生效。注意：" + spec["docs"] + " 知识文档需同步更新"
                        if spec["knowledge"] else "参数已生效。")}


@app.get("/api/admin/config/{key}/history")
def admin_config_history(key: str, limit: int = 50, request: Request = None):
    _admin_guard(request)
    rows = db.fetch_all(
        "SELECT l.id, l.detail, l.pending_sync, l.sync_done_at, l.created_at, "
        "       u.username AS admin_name "
        "FROM admin_logs l LEFT JOIN users u ON u.id = l.admin_id "
        "WHERE l.action='config_update' AND l.target_id=%s "
        "ORDER BY l.id DESC LIMIT %s", (key, min(int(limit), 200)))
    return {"ok": True, "history": rows}


@app.post("/api/admin/config/{key}/sync-confirm")
def admin_config_sync_confirm(key: str, request: Request = None):
    """知识文档人工更新完成后确认 → 清"待同步"标记（记录确认动作本身）。"""
    admin_uid = _admin_guard(request)
    rows = db.fetch_all(
        "SELECT id FROM admin_logs WHERE target_type='config' AND target_id=%s "
        "AND pending_sync=1 AND sync_done_at IS NULL", (key,))
    if not rows:
        return {"ok": True, "cleared": 0, "message": "该参数没有待同步标记"}
    db.execute(
        "UPDATE admin_logs SET sync_done_at=NOW() WHERE target_type='config' "
        "AND target_id=%s AND pending_sync=1 AND sync_done_at IS NULL", (key,))
    _log_admin(admin_uid, "knowledge_sync_confirm", "config", key,
               detail={"cleared": len(rows), "docs": rules.RULE_SPECS[key]["docs"]})
    return {"ok": True, "cleared": len(rows)}


# ---- 2) 用户管理（白名单：锁定 / 重置支付密码 / 角色。禁改资产） ---------- #
@app.post("/api/admin/users/{user_id}/lock")
def admin_user_lock(user_id: int, req: _UserLockReq, request: Request = None):
    admin_uid = _admin_guard(request)
    row = db.fetch_one("SELECT id, username, role FROM users WHERE id=%s", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["role"] == "admin" and req.locked:
        raise HTTPException(status_code=400, detail="不允许锁定管理员账号")
    db.execute("UPDATE users SET locked=%s WHERE id=%s",
               (1 if req.locked else 0, int(user_id)))
    _log_admin(admin_uid, "user_lock" if req.locked else "user_unlock", "user", user_id,
               detail={"username": row["username"]})
    return {"ok": True, "locked": bool(req.locked),
            "message": (f"已锁定 {row['username']}（登录将被拒绝）"
                        if req.locked else f"已解锁 {row['username']}")}


@app.post("/api/admin/users/{user_id}/reset-paypass")
def admin_user_reset_paypass(user_id: int, request: Request = None):
    """重置支付密码为随机 6 位码 —— 明文只通过站内信送达本人（不落库、不回显管理端）。"""
    admin_uid = _admin_guard(request)
    row = db.fetch_one("SELECT id, username FROM users WHERE id=%s", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    new_pwd = "".join(_secrets.choice("0123456789") for _ in range(6))
    try:
        paypass.set_password(int(user_id), new_pwd)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"重置失败：{exc}")
    # ★ 只保留最新一条重置通知：旧码随删除立即失效语义，避免明文长期躺在收件箱
    # ★ LIKE 里的 % 字面量须转义成 %%（pymysql 会做 % 格式化）
    db.execute(
        "DELETE FROM notifications WHERE user_id=%s AND title LIKE '%%支付密码已重置%%'",
        (int(user_id),))
    inbox.push_notification(
        int(user_id), "notice", "【小微银行】支付密码已重置",
        "管理员已应您的要求重置支付密码。新支付密码为：" + new_pwd +
        "（6 位数字，此前的重置码已作废）。请登录后立即在「安全中心」修改为你自己的密码，"
        "切勿告知他人；本条通知阅后建议删除。")
    # ★ 重置码明文绝不落审计 / 日志：只送达本人收件箱
    _log_admin(admin_uid, "user_reset_paypass", "user", user_id,
               detail={"username": row["username"], "notification": "sent"})
    return {"ok": True,
            "message": f"已重置 {row['username']} 的支付密码，新密码已通过站内信送达本人"}


@app.post("/api/admin/users/{user_id}/role")
def admin_user_role(user_id: int, req: _UserRoleReq, request: Request = None):
    admin_uid = _admin_guard(request)
    if req.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role 只能是 user 或 admin")
    row = db.fetch_one("SELECT id, username, role FROM users WHERE id=%s", (user_id,))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["id"] == admin_uid:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")
    if row["role"] == req.role:
        return {"ok": True, "message": "角色未变化"}
    db.execute("UPDATE users SET role=%s WHERE id=%s", (req.role, int(user_id)))
    _log_admin(admin_uid, "user_role", "user", user_id,
               detail={"username": row["username"], "old": row["role"], "new": req.role})
    return {"ok": True, "message": f"已将 {row['username']} 的角色从 {row['role']} 调整为 {req.role}"}


@app.delete("/api/admin/users/{user_id}")
def admin_user_delete(user_id: int, confirm_username: str = "", allow_admin: bool = False,
                      request: Request = None):
    """物理删除用户**及其全部业务数据**（单事务级联）。

    历史背景：本库一直没有外键，早先只能手工 `DELETE FROM users`，结果是
    accounts / cards / transactions / subscriptions 全留成孤儿；一旦 MySQL 自增值
    被复用，新注册用户会静默继承死账号的数据（`provision_new_user` 的幂等闸门会跳过初始化）。
    现在改为走本接口：外键 `ON DELETE CASCADE`（sql/upgrade_delete_cascade.sql）
    + 应用层显式单事务删除，双保险。

    安全护栏：
      - 必须传 `confirm_username` 且与目标账号名完全一致（防删错人）；
      - 不允许删除自己；不允许删除库里最后一个管理员；删管理员必须显式 `allow_admin=true`；
      - `admin_logs` 审计留痕不删，删除动作本身写入审计。
    正式环境请改用 users.deleted_at 软删（资金流水按监管应留痕）。
    """
    admin_uid = _admin_guard(request)
    if int(user_id) == int(admin_uid):
        raise HTTPException(status_code=400, detail="不能删除自己的账号")
    row = db.fetch_one("SELECT id, username, role FROM users WHERE id=%s", (int(user_id),))
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    uname = row["username"] or ("user%s" % row["id"])
    if not confirm_username or confirm_username.strip() != uname:
        raise HTTPException(
            status_code=400,
            detail=f"请传 confirm_username 且与目标账号名完全一致（当前目标为 {uname}）以确认删除")
    try:
        result = profile_db.delete_user_cascade(int(user_id), allow_admin=bool(allow_admin))
    except profile_db.DeleteUserError as exc:
        status = {"not_found": 404, "last_admin": 400}.get(exc.code, 400)
        raise HTTPException(status_code=status, detail=exc.message) from exc

    _log_admin(admin_uid, "user_delete", "user", user_id,
               detail={"username": uname, "role": result["role"],
                       "deleted": result["deleted"],
                       "deleted_total": result["deleted_total"]})
    return {"ok": True, "deleted": result["deleted"],
            "deleted_total": result["deleted_total"],
            "message": f"已删除 {uname}（#{result['user_id']}）及其 "
                       f"{result['deleted_total']} 行业务数据，操作已计入审计"}


# ---- 3) 理财产品管理（上架 / 下架 / 编辑；下架 → 用户端不可见不可买） ------ #
@app.get("/api/admin/products")
def admin_products(request: Request = None):
    _admin_guard(request)
    rows = db.fetch_all(
        "SELECT p.id, p.name, p.term, p.risk_level, p.annual_rate, p.min_amount, p.status, "
        "       (SELECT COUNT(*) FROM holdings h WHERE h.product_id=p.id) AS holders "
        "FROM products p ORDER BY p.id ASC")
    return {"ok": True, "products": rows}


@app.put("/api/admin/products/{product_id}")
def admin_product_update(product_id: int, req: _ProductUpdate, request: Request = None):
    admin_uid = _admin_guard(request)
    row = db.fetch_one("SELECT * FROM products WHERE id=%s", (product_id,))
    if not row:
        raise HTTPException(status_code=404, detail="产品不存在")
    changes: dict = {}
    if req.name is not None and req.name.strip():
        changes["name"] = req.name.strip()
    if req.term is not None and req.term.strip():
        changes["term"] = req.term.strip()
    if req.risk_level is not None:
        if req.risk_level not in (1, 2, 3):
            raise HTTPException(status_code=400, detail="风险等级只能为 1/2/3")
        changes["risk_level"] = int(req.risk_level)
    if req.annual_rate is not None:
        if not (0 <= float(req.annual_rate) <= 20):
            raise HTTPException(status_code=400, detail="年化收益率需在 0 ~ 20% 之间")
        changes["annual_rate"] = float(req.annual_rate)
    if req.min_amount is not None:
        if float(req.min_amount) < 0:
            raise HTTPException(status_code=400, detail="起购金额不能为负")
        changes["min_amount"] = float(req.min_amount)
    if req.status is not None:
        if req.status not in ("on", "off"):
            raise HTTPException(status_code=400, detail="status 只能是 on（上架）/ off（下架）")
        changes["status"] = req.status
    if not changes:
        return {"ok": True, "message": "没有需要修改的字段"}
    sets = ", ".join(f"{k}=%s" for k in changes)
    db.execute(f"UPDATE products SET {sets} WHERE id=%s",
               (*changes.values(), int(product_id)))
    _log_admin(admin_uid, "product_update", "product", product_id,
               detail={"name": row["name"],
                       "old": {k: str(row[k]) for k in changes},
                       "new": {k: str(v) for k, v in changes.items()}})
    return {"ok": True, "message": f"产品「{row['name']}」已更新", "changes": changes}


# ---- 4) 知识库（docs/knowledge：列表 / 查看 / 待同步 / 重建索引） ---------- #
@app.get("/api/admin/knowledge")
def admin_knowledge(request: Request = None):
    _admin_guard(request)
    docs = []
    if KNOWLEDGE_DIR.exists():
        for f in sorted(KNOWLEDGE_DIR.glob("*.md")):
            if f.name == "README.md":
                continue   # README 是索引本体，不作为知识文档列出
            docs.append({
                "name": f.name, "size": f.stat().st_size,
                "updated_at": datetime.fromtimestamp(
                    f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return {"ok": True, "docs": docs, "pending": _pending_sync_rows(),
            "dir": str(KNOWLEDGE_DIR)}


@app.get("/api/admin/knowledge/{doc_name}")
def admin_knowledge_doc(doc_name: str, request: Request = None):
    _admin_guard(request)
    if "/" in doc_name or "\\" in doc_name or not doc_name.endswith(".md"):
        raise HTTPException(status_code=400, detail="非法文档名")
    f = KNOWLEDGE_DIR / doc_name
    if not f.exists():
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"ok": True, "name": doc_name, "content": f.read_text(encoding="utf-8")}


@app.post("/api/admin/knowledge/{doc_name}/sync-confirm")
def admin_knowledge_sync_confirm(doc_name: str, request: Request = None):
    """人工更新文档后点「已同步」→ 清掉指向该文档的 pending_sync 标记。"""
    admin_uid = _admin_guard(request)
    rows = db.fetch_all(
        "SELECT id, target_id, detail FROM admin_logs WHERE pending_sync=1 AND sync_done_at IS NULL")
    cleared = 0
    for r in rows:
        ref = ""
        try:
            ref = (json.loads(r["detail"] or "{}") or {}).get("docs", "")
        except Exception:  # noqa: BLE001
            ref = ""
        if ref == doc_name or r["target_id"] == doc_name:
            db.execute("UPDATE admin_logs SET sync_done_at=NOW() WHERE id=%s", (r["id"],))
            cleared += 1
    if cleared:
        _log_admin(admin_uid, "knowledge_sync_confirm", "knowledge", doc_name,
                   detail={"cleared": cleared})
    return {"ok": True, "cleared": cleared,
            "message": (f"已确认 {doc_name} 同步完成，清除 {cleared} 条待同步标记"
                        if cleared else f"{doc_name} 没有待同步标记")}


@app.post("/api/admin/knowledge/rebuild")
def admin_knowledge_rebuild(request: Request = None):
    """重建 RAG 知识索引（重扫 docs/knowledge/*.md → 刷新 README.md 索引表）。"""
    admin_uid = _admin_guard(request)
    try:
        import sys as _sys
        sys_root = str(Path(__file__).resolve().parent / "scripts")
        if sys_root not in _sys.path:
            _sys.path.insert(0, sys_root)
        from rebuild_knowledge_index import rebuild as _rebuild_readme
        readme_stats = _rebuild_readme()          # README 索引表（人读）
        from utils import knowledge as _knowledge
        rag_stats = _knowledge.rebuild()          # ★ 检索索引（Agent 真正检索的 chunks + 向量）
        stats = {**readme_stats, "chunks": rag_stats["chunks"], "embed_model": rag_stats["model"]}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"重建失败：{exc}")
    _log_admin(admin_uid, "knowledge_rebuild", "knowledge", "index", detail=stats)
    return {"ok": True, "stats": stats,
            "message": (f"检索索引已重建：{stats.get('docs', 0)} 篇文档 / "
                        f"{stats.get('chunks', 0)} 个片段（embed：{stats.get('embed_model', '')}）")}


# ---- 5) 系统公告：全员收件箱群发（复用通知体系 type=notice） --------------- #
@app.post("/api/admin/announcement")
def admin_announcement(req: _AnnouncementReq, request: Request = None):
    admin_uid = _admin_guard(request)
    if not req.title.strip() or not req.content.strip():
        raise HTTPException(status_code=400, detail="标题与内容必填")
    users = db.fetch_all("SELECT id FROM users")
    for u in users:
        inbox.push_notification(int(u["id"]), "notice",
                                req.title.strip()[:100], req.content)
    _log_admin(admin_uid, "notice_send", "notice", "all",
               detail={"title": req.title.strip(), "recipients": len(users)})
    return {"ok": True, "recipients": len(users),
            "message": f"公告已发送至 {len(users)} 位用户的收件箱"}


# ---- 6) 操作审计：管理员操作日志（admin_logs 独立视图） -------------------- #
@app.get("/api/admin/admin-logs")
def admin_admin_logs(action: str = "", admin_id: int | None = None,
                     limit: int = 200, request: Request = None):
    _admin_guard(request)
    where, args = ["1=1"], []
    if action:
        where.append("l.action=%s"); args.append(action)
    if admin_id:
        where.append("l.admin_id=%s"); args.append(admin_id)
    args.append(min(int(limit), 500))
    rows = db.fetch_all(
        "SELECT l.*, u.username AS admin_name FROM admin_logs l "
        "LEFT JOIN users u ON u.id=l.admin_id WHERE " + " AND ".join(where) +
        " ORDER BY l.id DESC LIMIT %s", tuple(args))
    return {"ok": True, "logs": rows, "pending": _pending_sync_rows()}


# --------------------------------------------------------------------------- #
# C5. /admin 独立页面（独立路由 + 独立布局；与用户端 SPA 分离）
# --------------------------------------------------------------------------- #
@app.get("/admin")
def page_admin():
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/admin/login")
def page_admin_login():
    # C6：运营管理后台独立登录入口（黑金体系 · 独立视觉变体）
    return FileResponse(WEB_DIR / "admin_login.html")


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
#  数字柜员状态（同源查询）：供前端面板判断「启用 / 在线 / 前端地址」
#  ★ 必须注册在下方 /api/{path:path} 兜底代理【之前】：
#    否则请求会先被兜底代理转发给 mock_bank，由其返回 404 {"detail":"Not Found"}。
#  ★ 前端据此拿 FAY_FRONTEND_URL 并以 iframe 直连（Live2D 前端含根绝对资源路径，
#    经子路径代理不可靠，故此处只回报状态与地址，不做页面代理）。
# --------------------------------------------------------------------------- #
@app.get("/api/digital-human/status")
def digital_human_status():
    ap = appearance_cfg.get_appearance()
    return JSONResponse({
        "enabled": digital_human.enabled,
        "ready": digital_human.is_ready(),
        "frontend_url": digital_human.frontend_url,
        # 数字人驱动 WS：前端「与小微对话」旁听它，按真实音频时长决定何时重开麦克风
        "ws_url": digital_human.ws_url,
        "username": digital_human.username,
        # 当前生效的人物模型 + 背景（前端面板据此拼 ?model=&bg=，让全局形象设置真正落地）
        "model": ap["model"],
        "background": ap["background"],
    })


@app.post("/api/digital-human/interrupt")
def digital_human_interrupt():
    """让数字柜员立即停止播报（「与小微对话」模式里的「打断」）。

    桥接层 stop() 本身静默失败：Fay 未启动 / 异常也不报错，前端照常继续对话。
    同样必须注册在 /api/{path:path} 兜底代理之前。
    """
    digital_human.stop()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
#  数字人形象配置（人物模型 + 背景）
#  ★ 必须注册在 /api/{path:path} 兜底代理【之前】，否则被转发给 mock_bank → 404。
#  读对登录用户开放；写（PUT / 上传背景）仅管理员。
# --------------------------------------------------------------------------- #
@app.get("/api/appearance")
def appearance_get(request: Request = None):
    ap = appearance_cfg.get_appearance()
    return JSONResponse({
        "ok": True,
        "model": ap["model"],
        "background": ap["background"],
        "models": appearance_cfg.list_models(),
        "backgrounds": appearance_cfg.list_backgrounds(),
        # Live2D 前端地址（5173）：前端预览 iframe 直接拼 ?model=&bg= 直连
        "frontend_url": digital_human.frontend_url,
    })


@app.put("/api/appearance")
def appearance_update(req: _AppearanceUpdate, request: Request = None):
    admin_uid = _admin_guard(request)
    try:
        cfg = appearance_cfg.set_appearance(req.model, req.background)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _log_admin(admin_uid, "appearance_update", "appearance", "global",
               detail={"model": cfg["model"], "background": cfg["background"]})
    return JSONResponse({
        "ok": True,
        "model": cfg["model"],
        "background": cfg["background"],
        "message": f"数字人形象已更新：人物={cfg['model']}，背景={cfg['background']}",
    })


@app.post("/api/appearance/background")
async def appearance_upload_background(request: Request):
    """上传新背景图（管理员）。落位到 mate-human dist/Resources，引用只用文件名。"""
    admin_uid = _admin_guard(request)
    form = await request.form()
    f = form.get("file") if form else None
    if not f or not getattr(f, "filename", None):
        raise HTTPException(status_code=400, detail="未收到文件")
    content = await f.read()
    if not content or len(content) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件为空或超出 8MB 上限")
    try:
        filename = appearance_cfg.save_background_file(f.filename, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _log_admin(admin_uid, "appearance_background_upload", "appearance", filename,
               detail={"bytes": len(content)})
    return JSONResponse({
        "ok": True,
        "filename": filename,
        "backgrounds": appearance_cfg.list_backgrounds(),
        "message": f"背景图已上传：{filename}",
    })


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


# 挂 lifespan：uvicorn（含 `uvicorn run_server:app` 方式）启动时触发一次自检
try:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        run_mcp_startup_selfcheck()   # 内部开线程，立即返回，不阻塞启动
        _knowledge_warmup()           # ★ 预热 DNS + 向量表（内部开线程）——实测首次解析要 ~11s
        yield

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
