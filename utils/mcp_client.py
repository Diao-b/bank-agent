"""
utils/mcp_client.py —— 阿里云百炼 MCP（东方财富妙想）单例客户端

职责（对外只暴露三个同步能力 + 一个异步入口，线程安全）：
  - connect()            懒连接：mcp SDK 的 streamable_http_client 握手；
                         失败按 MCP_CONNECT_RETRIES 重试（间隔 MCP_CONNECT_RETRY_DELAY），
                         仍失败 → 熔断标记不可用
  - discover_tools()     tools/list，返回 [{name, description, inputSchema}]
  - call_tool()          tools/call，30s 超时；content 只聚合 text；异常 → {"error": ...}
                         连续失败 ≥ MCP_FAIL_THRESHOLD 自动熔断（标记不可用）

线程模型（★ 已知坑：禁止嵌套 asyncio.run）：
  - 每个进程只建一条后台事件循环线程（daemon），所有 async 协程都跑在它上面；
  - 现有 execute_tool 是同步的，通过 run_coroutine_threadsafe(...).result(timeout)
    桥接 —— 同步侧阻塞在调用线程（LangGraph worker 线程 / 请求线程），
    FastAPI 的事件循环线程从不被占用，不存在 RuntimeError("asyncio.run() cannot be
    called from a running event loop") 的触发条件；
  - 阿里云百炼 MCP 端点是 StreamableHTTP（不是纯 SSE）。每次工具调用使用一条
    短会话，完成 initialize → 目标调用 → 立即释放，避免长连接被网关/服务侧空闲断开。

鉴权：Header "Authorization: Bearer $DASHSCOPE_API_KEY"。key 未配置时不做任何网络请求，
      available=False（Agent 端自动退回纯本地工具清单）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import config

logger = logging.getLogger("mcp_client")

try:  # mcp SDK 缺失时优雅降级：功能全关但不影响 Agent 主链路
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import TextContent

    _MCP_SDK_IMPORT_ERROR: str | None = None
except Exception as exc:  # noqa: BLE001
    _MCP_SDK_IMPORT_ERROR = str(exc)
    httpx2 = None  # type: ignore[assignment]
    ClientSession = None  # type: ignore[assignment]
    streamable_http_client = None  # type: ignore[assignment]
    TextContent = ()  # type: ignore[assignment]


def _as_text(block: Any) -> str | None:
    """MCP content 块可能 text/image/embedded 混合 —— 只取 text。"""
    if isinstance(block, TextContent):
        return getattr(block, "text", None)
    # 老版本 mcp SDK 用普通 dict 表示
    if isinstance(block, dict):
        return block.get("text") if block.get("type") == "text" else None
    if hasattr(block, "type") and getattr(block, "type") == "text":
        return getattr(block, "text", None)
    return None


class _StreamableSession:
    """一条短会话：transport + ClientSession + httpx client 的上下文管理包装。"""

    def __init__(self, endpoint: str, api_key: str, timeout: float = 15.0):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self._http_client: Any = None
        self._transport_ctx: Any = None
        self._session: Any = None

    async def __aenter__(self):
        if not httpx2:
            raise RuntimeError("mcp SDK 未安装")
        self._http_client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx2.Timeout(self.timeout, connect=self.timeout),
            follow_redirects=True,
        )
        self._transport_ctx = streamable_http_client(
            self.endpoint,
            http_client=self._http_client,
            terminate_on_close=False,
        )
        read, write = await self._transport_ctx.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        for mgr_name in ("_session", "_transport_ctx", "_http_client"):
            mgr = getattr(self, mgr_name, None)
            if mgr is None:
                continue
            try:
                await mgr.__aexit__(exc_type, exc, tb)
            except Exception:  # noqa: BLE001
                pass
            setattr(self, mgr_name, None)


class MCPClient:
    """单例（模块底部 _client）。同一进程内共享同一熔断状态与工具缓存。"""

    def __init__(self, endpoint: str | None = None, api_key: str | None = None):
        self.endpoint = endpoint or config.MCP_ENDPOINT
        self.api_key = api_key if api_key is not None else config.DASHSCOPE_API_KEY
        self.timeout: float = config.MCP_TOOL_TIMEOUT
        self.connect_retries: int = config.MCP_CONNECT_RETRIES
        self.connect_delay: float = config.MCP_CONNECT_RETRY_DELAY
        self.fail_threshold: int = config.MCP_FAIL_THRESHOLD

        self.available: bool = False          # 服务可用（能出工具清单 / 能调用）
        self.reason: str = "not_started"      # 便于日志与自检：ok / no_key / no_sdk / connect_failed / circuit_open
        self.last_error: str = ""

        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._state_lock = threading.Lock()   # 保护同步侧读到的状态
        self._connect_attempted: bool = False
        self._fail_streak: int = 0
        self._tools_cache: list[dict] = []

        # 启动前即判定不可恢复的配置问题（不做网络请求，秒回）
        if _MCP_SDK_IMPORT_ERROR:
            self.available = False
            self.reason = "no_sdk"
            self.last_error = f"mcp SDK 未安装或导入失败：{_MCP_SDK_IMPORT_ERROR}"
            logger.warning("[mcp] %s", self.last_error)
        elif not self.api_key:
            self.available = False
            self.reason = "no_key"
            self.last_error = "DASHSCOPE_API_KEY 未配置（.env），外部金融数据源已停用"

    # ------------------------------------------------------------------ #
    #  事件循环线程
    # ------------------------------------------------------------------ #
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=loop.run_forever, daemon=True,
                                 name="mcp-event-loop")
            t.start()
            self._loop, self._loop_thread = loop, t
        return self._loop

    def _run_sync(self, coro_factory, timeout: float) -> Any:
        """同步桥接：把协程丢到后台事件循环，阻塞等结果（上限 timeout 秒）。

        coro_factory: 无参可调用对象，返回一个协程对象；这样未 awaited 的协程
        不会在 guard 失败前被创建，从而避免 RuntimeWarning: coroutine never awaited。
        """
        if _MCP_SDK_IMPORT_ERROR or not self.api_key:
            raise RuntimeError(self.last_error or "MCP 不可用")
        loop = self._ensure_loop()
        coro = coro_factory()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=timeout)
        except TimeoutError:
            fut.cancel()  # 尽力取消，避免协程悬挂在后台循环里
            raise TimeoutError(f"MCP 调用超时（>{timeout}s）")
        except Exception:
            raise

    # ------------------------------------------------------------------ #
    #  async 核心 —— 每条调用新建一条短 StreamableHTTP 会话
    # ------------------------------------------------------------------ #
    async def _with_session(self, coro_factory, *, connect_timeout: float = 15.0):
        """新建一条短会话，initialize 后执行 coro_factory(session)。"""
        async with _StreamableSession(
            self.endpoint, self.api_key, timeout=connect_timeout
        ) as session:
            return await coro_factory(session)

    async def connect(self) -> bool:
        """尝试一次 tools/list 验证连通性；成功即认为可用。

        失败自动重试；全部失败抛出并记录 reason=connect_failed。
        """
        with self._state_lock:
            self._connect_attempted = True
        attempts = 1 + max(0, self.connect_retries)
        last_err: Exception | None = None
        for i in range(attempts):
            try:
                await self.discover_tools()
                with self._state_lock:
                    self.available = True
                    self.reason = "ok"
                    self.last_error = ""
                    self._fail_streak = 0
                logger.info("[mcp] 连接成功（endpoint=%s）", self.endpoint)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if i < attempts - 1:
                    logger.warning("[mcp] 连接失败（第 %d/%d 次）：%s；%ss 后重试",
                                 i + 1, attempts, exc, self.connect_delay)
                    await asyncio.sleep(self.connect_delay)
        with self._state_lock:
            self.available = False
            self.reason = "connect_failed"
            self.last_error = str(last_err)
        raise RuntimeError(f"MCP 连接失败（重试 {self.connect_retries} 次后放弃）：{last_err}")

    async def discover_tools(self) -> list[dict]:
        """tools/list → [{name, description, inputSchema}]；结果缓存供同步侧直接读。"""

        async def _list(session):
            result = await session.list_tools()
            tools: list[dict] = []
            for t in result.tools:
                schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None) or {}
                if not isinstance(schema, dict):
                    schema = {"type": "object"}
                tools.append({
                    "name": getattr(t, "name"),
                    "description": getattr(t, "description", "") or "",
                    "inputSchema": schema,
                })
            with self._state_lock:
                self._tools_cache = tools
            logger.info("[mcp] tools/list 共发现 %d 个工具", len(tools))
            return tools

        return await self._with_session(_list, connect_timeout=15.0)

    async def _call_once(self, session: Any, name: str, args: dict) -> Any:
        return await asyncio.wait_for(
            session.call_tool(name, arguments=args or {}),
            timeout=self.timeout,
        )

    async def call_tool(self, name: str, args: dict | None = None) -> dict:
        """调 tools/call。

        - 成功：聚合 content 里的 text → 返回 {"ok": True, "content": <text>,
          "data": <json解析结果或None>}
        - 服务端 isError / 超时 / 异常：返回 {"error": "..."}，连续失败计数，
          达阈值熔断（available=False, reason=circuit_open）
        """
        async def _call(session):
            return await self._call_once(session, name, args or {})

        try:
            result = await self._with_session(_call, connect_timeout=15.0)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            return self._record_failure(name, f"调用超时（>{self.timeout}s）：{exc}")
        except Exception as exc:  # noqa: BLE001
            return self._record_failure(name, str(exc))
        return self._handle_result(result, name)

    def _handle_result(self, result: Any, name: str) -> dict:
        text_parts: list[str] = []
        for block in getattr(result, "content", None) or []:
            txt = _as_text(block)
            if txt:
                text_parts.append(str(txt))
        text = "\n".join(text_parts).strip()

        is_error = bool(getattr(result, "isError", False))
        if is_error or not text:
            msg = text or "服务端未返回任何内容"
            return self._record_failure(name, msg)
        with self._state_lock:
            self._fail_streak = 0
        data: Any = None
        try:
            import json as _json
            data = _json.loads(text)
        except Exception:  # noqa: BLE001  非 JSON 文本原样返回
            pass
        return {"ok": True, "content": text, "data": data}

    def _record_failure(self, name: str, msg: str) -> dict:
        with self._state_lock:
            self._fail_streak += 1
            self.last_error = msg
            if self._fail_streak >= self.fail_threshold:
                self.available = False
                self.reason = "circuit_open"
                logger.warning("[mcp] 连续 %d 次失败，熔断：外部金融数据源标记不可用（%s）",
                               self._fail_streak, msg)
        return {"error": msg}

    # ------------------------------------------------------------------ #
    #  同步对外接口（execute_tool / 启动自检 / test 脚本用）
    # ------------------------------------------------------------------ #
    def connect_sync(self, timeout: float = 30.0) -> bool:
        return self._run_sync(lambda: self.connect(), timeout=timeout)

    def discover_tools_sync(self, timeout: float = 40.0) -> list[dict]:
        return self._run_sync(lambda: self.discover_tools(), timeout=timeout)

    def call_tool_sync(self, name: str, args: dict | None = None,
                       timeout: float | None = None) -> dict:
        if not self.available:
            return {"error": "外部数据源暂时不可用，请稍后再试"}
        return self._run_sync(
            lambda: self.call_tool(name, args),
            timeout=timeout or (self.timeout + 10),
        )

    # ------------------------------------------------------------------ #
    #  只读快照
    # ------------------------------------------------------------------ #
    def get_tools_cache(self) -> list[dict]:
        """同步侧拿已发现的工具清单（不需要连网，未发现为空）。"""
        with self._state_lock:
            return list(self._tools_cache)

    def status(self) -> dict:
        with self._state_lock:
            return {
                "available": self.available,
                "reason": self.reason,
                "last_error": self.last_error,
                "tools": len(self._tools_cache),
                "fail_streak": self._fail_streak,
                "connect_attempted": self._connect_attempted,
            }


# 单例：整个进程共用同一熔断状态与工具缓存
client = MCPClient()


def ensure_connected_sync() -> dict:
    """启动自检 / 首次调用前使用：尽力连接并发现工具。

    返回 {"ok": bool, "tools": [...], "status": {...}}；绝不抛异常（失败仅告警）。
    """
    if not client.api_key:
        return {"ok": False, "status": client.status(), "tools": []}
    if client.available and client.get_tools_cache():
        return {"ok": True, "status": client.status(), "tools": client.get_tools_cache()}
    try:
        client.connect_sync(timeout=60.0)   # 内含 2 次重试 + 3s 间隔
        tools = client.discover_tools_sync(timeout=60.0)
        return {"ok": True, "status": client.status(), "tools": tools}
    except Exception as exc:  # noqa: BLE001  失败仅告警，不阻断启动 / 调用方
        logger.warning("[mcp] 自检失败：%s", exc)
        return {"ok": False, "status": client.status(), "tools": []}
