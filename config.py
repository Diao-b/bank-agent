"""
config.py —— 统一从环境变量读取配置（.env + python-dotenv）

约定：
- 所有密钥 / 连接信息只从这里读取，业务代码不得散落 os.getenv
- .env 不入库（见 .gitignore），.env.example 是模板
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录 bank-agent/
BASE_DIR = Path(__file__).resolve().parent

# 显式指定 .env 路径，避免在不同 cwd 下启动时找不到
load_dotenv(BASE_DIR / ".env", override=False)


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


# ---------------------------------------------------------------- LLM (DeepSeek)
DEEPSEEK_API_KEY: str = _get("DEEPSEEK_API_KEY", "") or ""
DEEPSEEK_BASE_URL: str = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL: str = _get("DEEPSEEK_MODEL", "deepseek-chat")
# 所有抽取 / 分类 / 路由调用固定 temperature=0
LLM_TEMPERATURE: float = float(_get("LLM_TEMPERATURE", "0"))
# ★ 单次 LLM 请求超时（秒）。OpenAI SDK 默认 600s —— 上游卡住时聊天会「一直正在输入」，
#   必须显式收紧：超时后抛异常 → SSE 推 error 帧 → 前端如实报错，而不是无限转圈。
LLM_TIMEOUT: float = float(_get("LLM_TIMEOUT", "45"))
# ★ SDK 内置重试次数。默认 2 → 单次调用最坏阻塞 (45s × 3)。压到 1，让失败尽快浮出。
LLM_MAX_RETRIES: int = int(_get("LLM_MAX_RETRIES", "1"))
# ★ read_agent 单轮对话内的「工具调用轮数」硬上限（防止 LLM 连续调工具导致的死循环）。
#   计数口径：最后一条用户消息之后产生的 tool 消息条数；超出即强制收尾出文本。
READ_AGENT_MAX_TOOL_ROUNDS: int = int(_get("READ_AGENT_MAX_TOOL_ROUNDS", "4"))

# ---------------------------------------------------------------- 知识库检索（RAG）
# ★ 相似度下限：低于此值的片段不注入提示词。**不要调回 0.30** ——
#   实测那个值下「你好呀」都能配上「诈骗话术识别」片段：纯噪声，还拖慢生成。
#   0.65 = 噪声上限(0.636) 与 有效下限(0.713) 之间（依据 scripts/_knowledge_check.py）。
KNOWLEDGE_SCORE_FLOOR: float = float(_get("KNOWLEDGE_SCORE_FLOOR", "0.65"))
# ★ 交互路径的嵌入超时（秒）：检索就在回答的关键路径上，宁可超时降级为本地检索，
#   也不能把一次回复拖到几十秒（旧值 30s 是实测"回答好慢"的元凶之一）。
KNOWLEDGE_EMBED_TIMEOUT: float = float(_get("KNOWLEDGE_EMBED_TIMEOUT", "4"))

# ---------------------------------------------------------------- MySQL
DB_HOST: str = _get("DB_HOST", "127.0.0.1")
DB_PORT: int = int(_get("DB_PORT", "3306"))
DB_USER: str = _get("DB_USER", "root")
DB_PASSWORD: str = _get("DB_PASSWORD", "") or ""
DB_NAME: str = _get("DB_NAME", "bank_agent")
# P1.3 连接池：把「每请求新建/关闭连接」改为进程内复用（省 TCP + 认证握手）。
#   池大小上限（稳态最多这么多连接）；借空且超时则开"溢出"连接，绝不因池满失败。
DB_POOL_SIZE: int = int(_get("DB_POOL_SIZE", "16"))
#   空闲超过该秒数的连接回收（防服务端超时空闲连接被误用）。
DB_POOL_IDLE_SECONDS: int = int(_get("DB_POOL_IDLE_SECONDS", "300"))
#   池满时等待可用连接的最长秒数；超时才开溢出连接。
DB_POOL_WAIT_SECONDS: float = float(_get("DB_POOL_WAIT_SECONDS", "5"))

# ---------------------------------------------------------------- Mock 银行服务
BANK_API_HOST: str = _get("BANK_API_HOST", "127.0.0.1")
BANK_API_PORT: int = int(_get("BANK_API_PORT", "8000"))
BANK_API_BASE: str = _get("BANK_API_BASE", "") or f"http://{BANK_API_HOST}:{BANK_API_PORT}"

# ---------------------------------------------------------------- 阿里云百炼 MCP（东方财富妙想 · 外部实时金融数据）
# 百炼 MCP 广场的 SSE 端点与鉴权（Bearer DASHSCOPE_API_KEY）。
DASHSCOPE_API_KEY: str = _get("DASHSCOPE_API_KEY", "") or ""
MCP_ENDPOINT: str = _get(
    "MCP_ENDPOINT",
    "https://dashscope.aliyuncs.com/api/v1/mcps/market-cmgjmcp00074946/mcp",
)
# 单次 tools/call 超时（秒）：该服务平均执行 3.6s，给足余量
MCP_TOOL_TIMEOUT: float = float(_get("MCP_TOOL_TIMEOUT", "30"))
# 连接握手失败重试次数与间隔（秒）
MCP_CONNECT_RETRIES: int = int(_get("MCP_CONNECT_RETRIES", "2"))
MCP_CONNECT_RETRY_DELAY: float = float(_get("MCP_CONNECT_RETRY_DELAY", "3"))
# 连续失败熔断阈值：达到后自动标记外部数据源不可用
MCP_FAIL_THRESHOLD: int = int(_get("MCP_FAIL_THRESHOLD", "5"))
# 慢调用状态提示阈值（秒）：MCP 调用超过该时长仍在进行时，推一条 status 事件
MCP_SLOW_STATUS_AFTER: float = float(_get("MCP_SLOW_STATUS_AFTER", "8"))

# ---------------------------------------------------------------- 其他
# 外部通知通道（可选并行增强）：验证码等通知的主通道永远是站内收件箱，
# 这里配置了才"尽力同发"，失败不影响收件箱必达。
NOTIFY_SMTP_HOST: str = _get("NOTIFY_SMTP_HOST", "") or ""
NOTIFY_SMTP_PORT: int = int(_get("NOTIFY_SMTP_PORT", "465"))
NOTIFY_SMTP_USER: str = _get("NOTIFY_SMTP_USER", "") or ""
NOTIFY_SMTP_PASS: str = _get("NOTIFY_SMTP_PASS", "") or ""
NOTIFY_SMTP_SSL: str = _get("NOTIFY_SMTP_SSL", "1") or "1"
NOTIFY_MAIL_TO: str = _get("NOTIFY_MAIL_TO", "") or ""

# 演示用固定用户（真实项目里来自登录态）
DEFAULT_USER_ID: int = int(_get("DEFAULT_USER_ID", "1"))
# HTTP 调 mock 银行服务的超时（秒）
HTTP_TIMEOUT: int = int(_get("HTTP_TIMEOUT", "10"))

# ---------------------------------------------------------------- 认证
# token 签名密钥。演示用默认值即可；生产请在 .env 里设置 AUTH_SECRET（长随机串）。
AUTH_SECRET: str = _get("AUTH_SECRET", "dev-bank-agent-secret-change-in-prod")
# 是否允许"不带 token"的匿名请求回落 DEFAULT_USER_ID（演示/单账号模式需要）。
# ★ 注意：只影响"完全没带 token"的请求；只要带了 token 但签名/过期不对，一律 401，
#   不会静默降级成默认用户 —— 否则伪造 token 就能糊弄身份。
# ★ 安全修复（审计 R1）：默认 0。库内已是多真实用户，匿名回落等于把
#   演示主账号（DEFAULT_USER_ID）的余额/流水/画像对未登录请求裸奔。
#   确要单机无登录演示时，在 .env 显式设 BANK_ALLOW_ANON=1。
BANK_ALLOW_ANON: bool = (_get("BANK_ALLOW_ANON", "0") or "0").lower() not in ("0", "false", "no")

# ---------------------------------------------------------------- 运行环境与演示开关
# 运行环境：dev / test / production。非 dev 环境会强制收紧安全配置（见 validate_auth_config）。
ENV: str = (_get("ENV", "dev") or "dev").strip().lower()
# 演示游客通道（/api/auth/guest）开关：仅开发态生效，生产环境无条件关闭。
# ★ 安全修复（审计 P0.1）：该端点无需任何凭证即为默认演示账号签发 token，是典型"认证后门"。
#   默认在 dev 开启（演示与基线回归需要），但 production 下即使误设为 1 也会被 auth_guest_enabled() 强制关闭。
#   若要连开发态也关闭，在 .env 显式设 DEV_ALLOW_GUEST=0（此时基线 login_guest 用例需同步改为真实登录）。
DEV_ALLOW_GUEST: bool = (_get("DEV_ALLOW_GUEST", "1") or "1").lower() not in ("0", "false", "no")
# 管理员种子账号密码（ensure_admin_account 使用）。生产必须在 .env 覆盖为强密码。
ADMIN_PASSWORD: str = _get("ADMIN_PASSWORD", "admin123")
# 存量无密码用户的演示回填密码（ensure_auth_schema 使用）。生产建议在 .env 覆盖。
DEFAULT_DEMO_PASSWORD: str = _get("DEFAULT_DEMO_PASSWORD", "123456")
# P0.2 一次性操作授权票据（op_token）的 TTL（秒）。票据签发后该时长内单次有效。
OP_TOKEN_TTL_SECONDS: int = int(_get("OP_TOKEN_TTL_SECONDS", "300"))
# 开发态"转账回滚注入"开关（X-Test-Inject: receiver-fail），仅供基线测试模拟"收款方入账失败"。
# ★ 与 BANK_ALLOW_ANON 解耦：安全闸门（匿名回落）不应兼任测试钩子开关 ——
#   否则 P0.1 把 BANK_ALLOW_ANON 收紧为 0 时，会静默破坏"事务回滚"回归用例。
#   默认非生产开启（与 DEV_ALLOW_GUEST 同口径），production 无条件关闭。
ALLOW_TEST_INJECT: bool = (
    (_get("ALLOW_TEST_INJECT", "1") or "1").lower() not in ("0", "false", "no")
) and ENV != "production"
# P1.2 后台调度器：把「订阅到期补扣 + 跨行到期清算」从读接口惰性触发升级为独立周期任务。
#   默认开（用户不访问也能按时扣款 / 到账 / 退票）；只读演示或特定测试可置 HOUSEKEEPING_ENABLED=0。
HOUSEKEEPING_ENABLED: bool = (_get("HOUSEKEEPING_ENABLED", "1") or "1").lower() not in ("0", "false", "no")
#   调度间隔（秒）。到期语义按「日」计（next_due / expected_at），60s 足够且开销低。
HOUSEKEEPING_INTERVAL_SECONDS: int = int(_get("HOUSEKEEPING_INTERVAL_SECONDS", "60"))
#   单轮最多处理的「到期订阅用户」数（防异常数据导致单轮失控）。
HOUSEKEEPING_BATCH_USERS: int = int(_get("HOUSEKEEPING_BATCH_USERS", "500"))


def auth_guest_enabled() -> bool:
    """游客通道是否开启：仅 dev 环境且显式允许时。生产环境永远 False。"""
    return ENV == "dev" and DEV_ALLOW_GUEST


def validate_auth_config() -> None:
    """生产环境安全基线：非法配置直接拒绝启动，防止关键后门在生产暴露。

    覆盖两项审计 P0.1 风险：
      - AUTH_SECRET 仍为硬编码默认值/空 → token 可被任意伪造；
      - 管理员仍使用默认密码 admin123 → 默认凭据。
    仅 ENV=production 时强制；dev / test 跳过（演示与基线回归需要宽松配置）。
    """
    if ENV != "production":
        return
    default_secret = "dev-bank-agent-secret-change-in-prod"
    if not AUTH_SECRET or AUTH_SECRET == default_secret:
        raise RuntimeError(
            "生产环境（ENV=production）禁止使用默认/空 AUTH_SECRET，"
            "请在 bank-agent/.env 设置强随机 AUTH_SECRET，否则拒绝启动。"
        )
    if ADMIN_PASSWORD == "admin123":
        raise RuntimeError(
            "生产环境（ENV=production）禁止使用默认管理员密码 admin123，"
            "请在 bank-agent/.env 设置 ADMIN_PASSWORD。"
        )


# M2 阶段 write_flow 独立测试用的 sqlite checkpoint 文件（M3 嵌入主图后不再需要）
WRITE_TEST_DB: str = str(BASE_DIR / "write_test.db")

# ---------------------------------------------------------------- 数字柜员（Fay 渲染端）
# Fay 作为独立子进程运行（agaent/fay/Fay），本项目只通过 HTTP 喂话，业务域零改动。
# 未配置 FAY_API_URL 时桥接层全部静默 no-op（演示默认关）。
# ★ 以下路径已按本机 Fay 源码核对（gui/flask_server.py，见 docs/fay_integration.md §3）。
FAY_API_URL: str = _get("FAY_API_URL", "") or ""
FAY_FRONTEND_URL: str = _get("FAY_FRONTEND_URL", "") or ""
# 固定会话名标识「数字柜员」（transparent-pass 的 user 字段）。
# ★ 必须与前端数字人页面连 WS 10002 时注册的 Username 完全一致：
#   Fay 的 WS 服务按 username 精确过滤后定向推送（core/wsa_server.py __producer_handler），
#   名字不一致 → 前端连上了却收不到任何口型/语音帧。
#   mate-human（Live2D 前端）在 src/lappmodel.ts 硬编码注册为 'User'，故默认取 'User'。
FAY_USERNAME: str = _get("FAY_USERNAME", "User") or "User"
# ★ 用 /transparent-pass（透传播放）：让数字人「逐字念出」本项目回复，不让 Fay 二次生成。
#   （/api/send 会把文本当"用户提问"→ Fay 自己再答一遍，不符合"同一句话"要求。）
FAY_SPEAK_PATH: str = _get("FAY_SPEAK_PATH", "/transparent-pass") or "/transparent-pass"
FAY_STOP_PATH: str = _get("FAY_STOP_PATH", "/to-stop-talking") or "/to-stop-talking"
FAY_HEALTH_PATH: str = _get("FAY_HEALTH_PATH", "/api/get-system-status") or "/api/get-system-status"
# 数字人驱动 WebSocket（Fay 的 10002）。「与小微对话」时前端会**旁听**这条通道，
# 用真实音频帧的 Time 累计值算出"它到底说多久"，据此决定何时重开麦克风
# （按文本估时长不准，实测偏差可达 40%）。
FAY_WS_URL: str = _get("FAY_WS_URL", "ws://127.0.0.1:10002") or ""
FAY_API_TIMEOUT: float = float(_get("FAY_API_TIMEOUT", "2"))
# 朗读文本口语化（默认开）：去掉 Markdown、连续列表只念第 1 项并用「等」带过、
# 行内举例只念一个、去 emoji。「0」可关闭（排查/对比用）。
# ★ 只影响喂给 Fay 的朗读文本；聊天区展示的原文不变。
FAY_SPEAK_DIGEST: bool = str(_get("FAY_SPEAK_DIGEST", "1")).strip().lower() not in ("0", "false", "no", "")
# 连续列表最多念几项（其余用「等」带过）。默认 1（按产品要求）；设 2/3 可多念几项；
# ≤0 = 不折叠（把列表全部念完，适合"转账回执"这类关键几项）。
FAY_SPEAK_LIST_ITEMS: int = int(_get("FAY_SPEAK_LIST_ITEMS", "1") or 1)


def db_config(use_db: bool = True) -> dict:
    """返回 pymysql 连接参数，强制 DictCursor。"""
    import pymysql
    from pymysql.cursors import DictCursor

    cfg = {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": False,
    }
    if use_db:
        cfg["database"] = DB_NAME
    return cfg


def require_llm_key() -> str:
    """在真正调用 LLM 前校验密钥，给出可操作的报错。"""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "缺少 DEEPSEEK_API_KEY：请在 bank-agent/.env 里填写 "
            "DEEPSEEK_API_KEY=sk-xxx（可先复制 .env.example）"
        )
    return DEEPSEEK_API_KEY


if __name__ == "__main__":
    print("BASE_DIR        :", BASE_DIR)
    print("DB              :", f"{DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
    print("BANK_API_BASE   :", BANK_API_BASE)
    print("DEEPSEEK_MODEL  :", DEEPSEEK_MODEL, "| temperature =", LLM_TEMPERATURE)
    print("DEEPSEEK_API_KEY:", (DEEPSEEK_API_KEY[:8] + "***") if DEEPSEEK_API_KEY else "(未配置)")
