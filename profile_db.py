"""
profile_db.py —— 用户画像数据层（MySQL bank_agent）

提供：
  - 画像读取：get_user_row / get_accounts_overview / get_profile_payload
  - Agent 注入用画像摘要：get_user_context / build_user_context_system_prompt
  - 画像编辑：update_user_profile（gender 变更自动重算 title_prefix）
  - 个性化问候语：generate_greeting / get_or_create_greeting（入库幂等）

设计约定：
  - gender 取值 male / female / other
  - title_prefix 推导：male→先生，female→女士，other→用户
  - 称呼（address）= 姓氏(real_name[0]) + title_prefix，如 '张先生'
  - 绝不把完整手机号写入对外字段；对外只用 phone_masked 脱敏号（由 phone 派生）
  - 任何异常都向上抛，由调用方（API / agent）收敛成友好错误
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime
from typing import Any

import pymysql

import config
from mock_bank import db  # 连接收口到单一源（组 R）

# --------------------------------------------------------------------------- #
#  映射表
# --------------------------------------------------------------------------- #
GENDER_TO_TITLE = {"male": "先生", "female": "女士", "other": "用户"}
GENDER_TO_CN = {"male": "男", "female": "女", "other": "其他"}

# 用户未设置偏好时的空值。
# 严禁在此预填 {"risk_preference": "稳健", "interests": ["理财","账单分析"]} 之类的推测值：
#   旧实现会把这套值当作"默认偏好"塞给每个新用户，用户只是打开个人中心点一下保存，
#   凭空编造的投资偏好就落库成真了，AI 还会照着它推荐产品。
EMPTY_PREFERENCES: dict = {}


# --------------------------------------------------------------------------- #
#  手机号脱敏 / 绑定态
# --------------------------------------------------------------------------- #
def mask_phone(phone: str | None) -> str:
    """完整手机号 → 脱敏 138****8888（仅展示用）。

    ★ 完整号是敏感字段，绝不直接进任何对外 JSON；所有展示一律走这里派生。
    """
    p = (phone or "").strip()
    if len(p) == 11 and p.isdigit():
        return f"{p[:3]}****{p[-4:]}"
    return p or ""


def get_phone_bound(user_id: int) -> bool:
    """是否已绑定手机号（users.phone 非空即视为已绑定）。

    写操作闸门：未绑定 → 资金类写操作（转账/理财/卡片/订阅）一律拦截。
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT phone FROM users WHERE id=%s", (int(user_id),))
            row = cur.fetchone()
    return bool(row and row.get("phone"))


# --------------------------------------------------------------------------- #
#  连接
# --------------------------------------------------------------------------- #
def _conn():
    return db.get_conn()


def _as_json(v: Any) -> dict:
    if v is None:
        return {}
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


def _now_cn() -> str:
    week = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    d = datetime.now()
    return f"{d.year}-{d.month:02d}-{d.day:02d} {d.hour:02d}:{d.minute:02d} {week[d.weekday()]}"


# --------------------------------------------------------------------------- #
#  读取
# --------------------------------------------------------------------------- #
def get_user_row(user_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, real_name, realname_verified, gender, title_prefix, phone, "
                "phone_tail, occupation, preferences, updated_at FROM users WHERE id=%s",
                (user_id,),
            )
            return cur.fetchone()


def get_accounts_overview(user_id: int) -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, balance FROM accounts WHERE user_id=%s "
                "AND COALESCE(account_type,'debit') <> 'credit' ORDER BY id", (user_id,)
            )
            rows = cur.fetchall()
    return [{"name": r["name"], "balance": float(r["balance"])} for r in rows]


def get_profile_payload(user_id: int) -> dict | None:
    """GET /api/profile 的完整返回。"""
    row = get_user_row(user_id)
    if not row:
        return None
    return {
        "real_name": row.get("real_name") or "",
        # ★ 是否已完成正式实名认证（仅 /api/realname/confirm 核验通过才会置 1）。
        #   ③ 基本信息「真实姓名」保存不算 —— 它只是展示名，无验证码核验。
        #   前端 ④ 卡片据此切换「已实名认证」只读态 / 填写态；后端 confirm 也再挡一次。
        "realname_verified": bool(row.get("realname_verified")),
        # 未填写就如实返回空。旧实现兜成 'male' / '先生'，前端表单会据此预选，
        # 用户只是打开个人中心点一下保存，凭空的性别就被写进库了。
        # （前端 index.html 自己有 `|| '先生'` 之类的展示兜底，后端不需要代劳。）
        "gender": row.get("gender") or "",
        "title_prefix": row.get("title_prefix") or "",
        "phone_tail": row.get("phone_tail") or "",
        # 脱敏号由完整 phone 派生（兼容未迁移用户：phone 为空时退回旧 phone_tail）
        "phone_masked": mask_phone(row.get("phone")) or (row.get("phone_tail") or ""),
        "phone_bound": bool(row.get("phone")),
        "occupation": row.get("occupation") or "",
        "preferences": _as_json(row.get("preferences")) or dict(EMPTY_PREFERENCES),
        "accounts": get_accounts_overview(user_id),
        "username": row.get("name") or "",
        "avatar": None,
    }


def get_address(real_name: str, title_prefix: str) -> str:
    """称呼 = 姓氏 + 称呼前缀，如 '张先生'。

    ★ 2026-09-21：**不再用「张」兜底**。以前 `(real_name or "张")[0]` 会让
      没有真实姓名的用户被拼成「张先生」—— 冒用了别人的姓。
      没有姓名时返回中性称呼（空串由调用方处理）。
    """
    surname = (real_name or "").strip()[:1]
    title = (title_prefix or "").strip()
    if surname:
        return f"{surname}{title}" if title else surname
    return title or ""


def _display_address(row: dict) -> tuple[str, str, bool]:
    """统一的「称呼」推导 —— **绝不编造身份**。返回 (称呼, 姓名, 是否已设置资料)。

    ★ 2026-09-21 修「跨用户身份串号」：以前 real_name / gender / title_prefix 缺失时
      分别兜成「张同学 / male / 先生」，再经 get_address 的 (real_name or "张")[0]
      拼成「张先生」——结果**所有没填过资料的账号都被当成同一个人**：
      小微对谁都喊「张先生」，还认为对方姓名是「张同学」。
      现在：填了真实姓名才用「姓+称呼」；没填就回落到**该用户自己的账号名**，
      连账号名都没有才用中性的「您」。
    """
    real_name = (row.get("real_name") or "").strip()
    gender = (row.get("gender") or "").strip()
    title_prefix = (row.get("title_prefix") or "").strip()
    account_name = (row.get("name") or row.get("username") or "").strip()
    if real_name:
        title = title_prefix or GENDER_TO_TITLE.get(gender, "")
        # 有称呼前缀 →「姓+前缀」（张先生）；没填性别 → 用**全名**（刁紫豫），
        # 绝不编造「先生/女士」，也不能只喊姓（"刁，您的余额…"很失礼）。
        return (get_address(real_name, title) if title else real_name), real_name, True
    return (account_name or "您"), account_name, False


def get_user_context(user_id: int) -> dict:
    """Agent 注入用的用户画像摘要（只含画像摘要，无敏感字段）。"""
    row = get_user_row(user_id) or {}
    address, real_name, profile_set = _display_address(row)
    gender = (row.get("gender") or "").strip()
    prefs = _as_json(row.get("preferences")) or dict(EMPTY_PREFERENCES)
    return {
        "称呼": address,
        "姓名": real_name,
        "性别": GENDER_TO_CN.get(gender, ""),
        # ★ 是否填过资料：没填时提示词要明确要求「不得猜测姓名/性别」，而不是替他编一个
        "已设置资料": profile_set,
        "账户": get_accounts_overview(user_id),
        "偏好": {
            # 未设置就是 None —— 不要兜成"稳健"，那是替用户编造投资偏好
            "risk_preference": prefs.get("risk_preference"),
            "interests": prefs.get("interests", []),
        },
        # 写操作闸门：未绑定手机号时，转账/购买/冻结/取消等写操作会被拦截，
        # Agent 应主动提示用户先去绑定手机，而不是假装能办。
        "手机已绑定": get_phone_bound(user_id),
        "当前时间": _now_cn(),
    }


def build_user_context_system_prompt(user_id: int) -> str:
    """拼装『用户画像』系统消息（注入到 read_agent 的 messages 头部）。"""
    try:
        ctx = get_user_context(user_id)
    except Exception:
        # DB 不可用时兜底：不影响主对话链路
        return ""
    address = ctx["称呼"]
    profile_set = bool(ctx.get("已设置资料"))
    full_name = (ctx.get("姓名") or "").strip()
    has_title = bool(address) and address != full_name     # 「刁紫豫」= 无敬称，别硬加先生/女士
    risk = ctx["偏好"]["risk_preference"]
    # ★ 不能写死「（如'张先生'）」当例子：模型会把例子当指令，对谁都喊「张先生」，
    #   这正是"不同用户被当成同一个人"的直接原因之一。
    if profile_set and has_title:
        address_rule = f"沟通规则：称呼用户为「{address}」，语气尊敬但不过分正式；"
    elif profile_set and address:
        address_rule = (
            f"沟通规则：称呼用户为「{address}」（该用户未填写性别，"
            "**不要自行加「先生 / 女士」**）；"
        )
    else:
        address_rule = (
            "沟通规则：该用户尚未设置姓名 / 称呼，统一用「您」称呼，"
            "**严禁猜测或编造其姓名、姓氏、性别**；"
            "画像里的账号名只用于你识别身份，不要当成姓名去称呼；"
        )
    # 用户没设过风险偏好时不要替他假设"稳健"——否则 AI 会照着编造的偏好推荐产品
    risk_rule = (
        f"用户风险偏好为「{risk}」，推荐理财时优先匹配该偏好；"
        if risk
        else "用户尚未设置风险偏好，推荐理财产品前必须先询问其风险承受能力，不得自行假设；"
    )
    json_str = json.dumps(ctx, ensure_ascii=False)
    # 写操作门槛提示：用户手机未绑定时，主动引导先绑定，而不是照办写操作
    bind_rule = (
        "用户尚未绑定手机号。涉及转账、购买理财、冻结/解冻卡片、取消订阅、签约代扣等"
        "『资金或权限变更』类请求时，不要直接执行或假装已办，应明确告知『需先绑定手机号"
        "（在「个人中心」完成，约 1 分钟）才能办理资金业务』并给出前往个人中心的引导；"
        "查询类（余额/流水/账单/持仓）请求正常处理，不拦截。"
        if not ctx.get("手机已绑定")
        else "用户已绑定手机号，涉及资金/权限变更的写操作按现有安全流程正常办理。"
    )
    return (
        "【用户画像】" + json_str + "\n"
        + address_rule
        + risk_rule +
        bind_rule +
        "禁止向用户复述完整手机号，只可用尾号。"
    )


# --------------------------------------------------------------------------- #
#  编辑
# --------------------------------------------------------------------------- #
def update_user_profile(
    user_id: int,
    *,
    real_name: str | None = None,
    gender: str | None = None,
    occupation: str | None = None,
    preferences: dict | None = None,
) -> dict | None:
    """更新可编辑字段。gender 变化时自动重算 title_prefix；写入 updated_at。

    返回更新后的完整 payload（含最新 title_prefix）。
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT gender, title_prefix, real_name, "
                "COALESCE(realname_verified, 0) AS realname_verified FROM users WHERE id=%s",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur_gender = row["gender"]
            cur_title = row["title_prefix"]

        sets: list[str] = []
        args: list[Any] = []

        # ★ 安全不变式：已实名用户的姓名不可在线修改（须柜台办理）。
        #   API 层强制防线——前端藏入口只是体验层，这里才是权威校验。
        if real_name is not None:
            if row["realname_verified"] and real_name != (row["real_name"] or ""):
                raise ValueError(
                    "该账号已完成实名认证，姓名不可在线修改；"
                    "如需变更请携带有效证件前往柜台办理"
                )
            sets.append("real_name=%s")
            args.append(real_name)

        if gender is not None and gender != cur_gender:
            new_title = GENDER_TO_TITLE.get(gender, "先生")
            sets.append("gender=%s")
            args.append(gender)
            sets.append("title_prefix=%s")
            args.append(new_title)
        elif gender is not None:
            sets.append("gender=%s")
            args.append(gender)

        if occupation is not None:
            sets.append("occupation=%s")
            args.append(occupation)

        if preferences is not None:
            sets.append("preferences=%s")
            args.append(json.dumps(preferences, ensure_ascii=False))

        if sets:
            sets.append("updated_at=NOW()")
            sql = "UPDATE users SET " + ", ".join(sets) + " WHERE id=%s"
            with _conn() as conn2:
                with conn2.cursor() as cur:
                    cur.execute(sql, args + [user_id])
                conn2.commit()

    # 返回最新完整画像（含重算后的 title_prefix）
    return get_profile_payload(user_id)


# --------------------------------------------------------------------------- #
#  个性化问候语
# --------------------------------------------------------------------------- #
def _period_greeting() -> str:
    h = datetime.now().hour
    if 5 <= h < 11:
        return "早上好"
    if 11 <= h < 13:
        return "中午好"
    if 13 <= h < 18:
        return "下午好"
    if 18 <= h < 23:
        return "晚上好"
    return "凌晨好"


def generate_greeting(user_id: int) -> str:
    """模板生成问候语（稳定零成本，不调 LLM）。

    ★ 2026-09-21：问候语与 get_user_context 用**同一口径**推导称呼。
      以前这里也写死 `real_name or "张同学"` / `title_prefix or "先生"`，
      于是每个新用户的**第一句话就是「尊敬的张先生」**，而且这行会落库到
      chat_messages —— 编造的身份被当成事实存了下来。没填资料一律用中性问候。
    """
    row = get_user_row(user_id) or {}
    address, _name, profile_set = _display_address(row)
    prefs = _as_json(row.get("preferences")) or dict(EMPTY_PREFERENCES)
    interests = prefs.get("interests") or []
    top_interest = interests[0] if interests else None
    period = _period_greeting()
    # 用户没设兴趣时不要编造"本月您在理财上有些新动态"—— 那是在替用户编业务事实，
    # 而且 greeting 会落库到 chat_messages，编造内容就真存进数据库了。
    tail = (
        f"本月您在{top_interest}上有些新动态，可以问我“这个月账单有什么变化”。"
        if top_interest
        else "我可以帮您查账单、管订阅、看余额；也可以先帮您设置一下风险偏好。"
    )
    who = f"尊敬的{address}" if (profile_set and address) else "您好"
    return f"{who}，{period}。我是小微，您的专属银行助手。" + tail


def conversation_has_greeting(conversation_id: str) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM chat_messages "
                "WHERE conversation_id=%s AND kind='greeting' LIMIT 1",
                (conversation_id,),
            )
            return cur.fetchone() is not None


def get_or_create_greeting(conversation_id: str, user_id: int) -> str:
    """对新会话首次生成并入库；历史会话直接返回已存问候（幂等，不重复生成）。

    走与真实消息同一个 save_message 写入路径，顺带确保 conversations 行存在，
    避免"有问候语但会话列表里查不到"的孤儿态。
    """
    if conversation_has_greeting(conversation_id):
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT content FROM chat_messages "
                    "WHERE conversation_id=%s AND kind='greeting' ORDER BY id DESC LIMIT 1",
                    (conversation_id,),
                )
                r = cur.fetchone()
        if r:
            ensure_conversation(conversation_id, user_id)
            return r["content"]
    text = generate_greeting(user_id)
    save_message(conversation_id, "assistant", text, user_id=user_id, kind="greeting")
    return text


# --------------------------------------------------------------------------- #
#  会话与消息持久化（审计项 M1 / M2）
#
#  背景：audit_data.md 实测确认 POST /api/chat 的对话只进 LangGraph 的 SQLite
#  checkpoint，MySQL 里 chat_messages 长期只有 kind='greeting' 的问候语。
#  这里补上真实对话的写入与回读能力。
#
#  表结构以 sql/upgrade_chat.sql 为准；此处 ensure_schema() 做一层幂等兜底，
#  避免脚本漏执行时整个会话列表 500。
# --------------------------------------------------------------------------- #
_SCHEMA_READY = False

_CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS conversations (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    conversation_id VARCHAR(64)  NOT NULL COMMENT '前端 thread_id，如 web-9k3k4e',
    user_id         BIGINT       NOT NULL,
    title           VARCHAR(128) NOT NULL DEFAULT '新会话' COMMENT '首条用户消息截取',
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_conv_id (conversation_id),
    KEY idx_conv_user (user_id, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def ensure_schema() -> None:
    """幂等补齐会话持久化所需的表与列（进程内只跑一次）。

    ADD COLUMN 无 IF NOT EXISTS，故先查 information_schema 再决定是否 ALTER。
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_CONVERSATIONS_DDL)

            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='chat_messages' AND column_name='seq'",
                (config.DB_NAME,),
            )
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE chat_messages "
                    "ADD COLUMN seq INT NOT NULL DEFAULT 0 COMMENT '会话内消息序号'"
                )

            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='chat_messages' AND column_name='user_id'",
                (config.DB_NAME,),
            )
            if not cur.fetchone():
                cur.execute("ALTER TABLE chat_messages ADD COLUMN user_id BIGINT NULL")

        conn.commit()

    _SCHEMA_READY = True


def _make_title(text: str, limit: int = 20) -> str:
    """会话标题 = 首条用户消息截取（压缩空白，超出加省略号）。"""
    flat = " ".join((text or "").split())
    if not flat:
        return "新会话"
    return flat if len(flat) <= limit else flat[:limit] + "…"


def ensure_conversation(conversation_id: str, user_id: int) -> None:
    """会话不存在则创建（标题默认『新会话』，等首条用户消息到达时自动改名）。"""
    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO conversations (conversation_id, user_id) VALUES (%s, %s)",
                (conversation_id, user_id),
            )
        conn.commit()


def conversation_owner(conversation_id: str) -> int | None:
    """返回会话归属的 user_id；会话不存在返回 None。

    供写入型接口（如结构化任务提交）做归属校验：知道 conversation_id 也不能
    往别人的会话里塞消息 / 借别人的会话续办挂起流程。
    """
    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM conversations WHERE conversation_id=%s LIMIT 1",
                (conversation_id,),
            )
            row = cur.fetchone()
    return int(row["user_id"]) if row and row.get("user_id") is not None else None


def save_message(
    conversation_id: str,
    role: str,
    content: str,
    user_id: int | None = None,
    kind: str = "chat",
    node_trace: Any = None,
) -> int:
    """写入一条消息，返回自增 id。

    seq = 同会话内当前最大序号 + 1，保证回读顺序稳定（不依赖 created_at 秒级精度）。
    首条 user 消息落库时顺便把默认标题『新会话』改成消息摘要。
    """
    ensure_schema()
    ensure_conversation(conversation_id, user_id or config.DEFAULT_USER_ID)
    trace = json.dumps(node_trace if node_trace is not None else [], ensure_ascii=False)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM chat_messages WHERE conversation_id=%s",
                (conversation_id,),
            )
            seq = int(cur.fetchone()["m"]) + 1

            cur.execute(
                "INSERT INTO chat_messages "
                "(conversation_id, user_id, role, content, node_trace, kind, seq, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())",
                (conversation_id, user_id, role, content, trace, kind, seq),
            )
            new_id = int(cur.lastrowid)

            if role == "user":
                cur.execute(
                    "UPDATE conversations SET title=%s, updated_at=NOW() "
                    "WHERE conversation_id=%s AND (title='新会话' OR title='')",
                    (_make_title(content), conversation_id),
                )
            else:
                cur.execute(
                    "UPDATE conversations SET updated_at=NOW() WHERE conversation_id=%s",
                    (conversation_id,),
                )
        conn.commit()

    return new_id


def get_conversation_messages(
    conversation_id: str,
    limit: int = 500,
    user_id: int | None = None,
) -> list[dict]:
    """回读一个会话的完整消息（按 seq 排序；greeting 与真实对话一并返回）。

    ★ 隔离：会话属于某个 user（conversations.user_id）。传 user_id 时只允许读
      本人会话 —— 防止知道 conversation_id 就扒别人对话内容（水平越权）。
    """
    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.role, m.content, m.kind, m.node_trace, m.created_at "
                "FROM chat_messages m "
                "JOIN conversations c ON c.conversation_id = m.conversation_id "
                "WHERE m.conversation_id=%s"
                + (" AND c.user_id=%s" if user_id is not None else "")
                + " ORDER BY m.seq ASC, m.id ASC LIMIT %s",
                (conversation_id, user_id, int(limit))
                if user_id is not None
                else (conversation_id, int(limit)),
            )
            rows = cur.fetchall()

    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "role": r["role"],
                "text": r["content"],
                "kind": r.get("kind") or "chat",
                "node_trace": _as_json(r.get("node_trace")),
                "time": str(r.get("created_at") or ""),
            }
        )
    return out


def list_conversations(user_id: int, limit: int = 50) -> list[dict]:
    """会话列表：按最近活跃倒序，带首条用户消息摘要与消息条数。"""
    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.conversation_id, c.title, c.created_at, c.updated_at, "
                "       (SELECT COUNT(*) FROM chat_messages m "
                "        WHERE m.conversation_id=c.conversation_id AND m.kind='chat') AS msg_count "
                "FROM conversations c "
                "WHERE c.user_id=%s "
                "ORDER BY c.updated_at DESC, c.id DESC LIMIT %s",
                (user_id, int(limit)),
            )
            rows = cur.fetchall()

    return [
        {
            "conversation_id": r["conversation_id"],
            "title": r["title"],
            "msg_count": int(r["msg_count"]),
            "created_at": str(r.get("created_at") or ""),
            "updated_at": str(r.get("updated_at") or ""),
        }
        for r in rows
    ]


def delete_conversation(conversation_id: str, user_id: int) -> bool:
    """删除会话及其消息（返回是否真的删掉了）。线程级 checkpoint 不在 MySQL，需另行清理。"""
    ensure_schema()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM chat_messages WHERE conversation_id=%s", (conversation_id,)
            )
            cur.execute(
                "DELETE FROM conversations WHERE conversation_id=%s AND user_id=%s",
                (conversation_id, user_id),
            )
            affected = cur.rowcount
        conn.commit()
    return bool(affected)


# --------------------------------------------------------------------------- #
#  认证（审计项 M3）：users 表原本无 password，任意密码可登录、register 不建用户
#
#  设计：
#  - 密码用 pbkdf2-hmac-sha256（Python 标准库，无第三方依赖），salt 随哈希入库
#  - token 为 HMAC 签名的 {uid}.{exp} 载荷，无第三方 JWT 库也能校验、且可防篡改
#  - 存量演示用户一次性回填：用户名 + 演示密码 123456（与登录页 mock 兜底一致）
# --------------------------------------------------------------------------- #
PBKDF2_ITERS = 100_000


def hash_password(pw: str, salt: bytes | None = None) -> str:
    """返回 'pbkdf2$sha256$iters$salt_b64$dk_b64'。salt 不存在则随机生成。"""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ITERS)
    return "pbkdf2$sha256$%d$%s$%s" % (
        PBKDF2_ITERS, base64.b64encode(salt).decode(), base64.b64encode(dk).decode(),
    )


def verify_password(pw: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    try:
        algo, sha, iters, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2" or sha != "sha256":
        return False
    try:
        salt = base64.b64decode(salt_b64)
        dk = base64.b64decode(dk_b64)
    except Exception:
        return False
    test = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, int(iters))
    return hmac.compare_digest(test, dk)


def _sign(payload: str) -> str:
    return hmac.new(config.AUTH_SECRET.encode("utf-8"), payload.encode("utf-8"), "sha256").hexdigest()[:32]


def issue_token(user_id: int, ttl_sec: int = 86400, role: str | None = None) -> str:
    """签发 HMAC 签名 token：base64(uid.exp[.role]|sig)，24 小时默认过期。

    role 可选（C5 后台管理）：payload 含 role 的 token 才会随登录响应下发；
    服务端 admin_guard 同时做 DB 复核，token 里的 role 只作快速通道。
    """
    payload = "%d.%d" % (user_id, int(time.time()) + ttl_sec)
    if role:
        payload += "." + role
    return base64.urlsafe_b64encode(("%s|%s" % (payload, _sign(payload))).encode()).decode()


def decode_token(token: str | None) -> int | None:
    """校验签名与过期，成功返回 user_id，失败返回 None。"""
    got = decode_token_full(token)
    return got[0] if got else None


def decode_token_full(token: str | None) -> tuple[int, str] | None:
    """校验签名与过期，返回 (user_id, role)。role 取 token 里的值，缺省 'user'。"""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = raw.rsplit("|", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        parts = payload.split(".")
        if len(parts) < 2:
            return None
        uid, exp = int(parts[0]), int(parts[1])
        if exp < time.time():
            return None
        role = parts[2] if len(parts) >= 3 else "user"
        return uid, role
    except Exception:
        return None


def ensure_auth_schema() -> None:
    """幂等补齐认证字段，并为存量无密码用户回填演示账号（username + 123456）。"""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='users' AND column_name='username'",
                (config.DB_NAME,),
            )
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE users "
                    "ADD COLUMN username VARCHAR(64) NULL COMMENT '登录账号', "
                    "ADD COLUMN password_hash VARCHAR(255) NULL COMMENT 'pbkdf2-sha256 哈希', "
                    "ADD UNIQUE KEY uk_users_username (username)"
                )
            cur.execute(
                "SELECT id, username, COALESCE(password_hash, '') AS ph FROM users"
            )
            rows = cur.fetchall()
            for row in rows:
                if row["username"] is None or not row["ph"]:
                    uname = row["username"] or ("user%d" % row["id"])
                    cur.execute(
                        "UPDATE users SET username=%s, password_hash=%s WHERE id=%s",
                        (uname, hash_password(config.DEFAULT_DEMO_PASSWORD), row["id"]),
                    )
        conn.commit()


def authenticate(username: str, password: str) -> dict | None:
    """校验用户名 + 密码；成功返回 {user_id, username}，失败返回 None。

    C6：管理员锁定的账号（users.locked=1）一律拒绝 —— "锁定 → 登录被拒"。
    返回 {"error": "locked"} 供上层区分提示（普通失败仍返回 None）。
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, password_hash, locked FROM users WHERE username=%s",
                (username,),
            )
            row = cur.fetchone()
    if not row:
        return None
    if row.get("locked"):
        return {"error": "locked"}
    if not verify_password(password, row.get("password_hash") or ""):
        return None
    return {"user_id": row["id"], "username": row["username"]}


def create_user(username: str, password: str) -> dict | None:
    """创建用户（username 唯一）；成功返回 {user_id, username}，冲突返回 None。

    ★ 2026-09-21 安全修复：**入库前必须 strip，并按去空格后判重**。
      以前注册原样入库、而登录会 strip，偏偏 MySQL 的 `utf8mb4_0900_ai_ci` 是
      **NO PAD**（尾部空格参与比较），于是「diao 」能绕过 `uk_users_username`
      建出第二个"同名"账号，后果：
        - 变体账号用**自己的密码永远登不进去**（登录被 strip 成「diao」，命中另一个人的行）；
        - 用「diao 」+ 别人密码登录，会**直接落到别人的账号**里去。
      唯一索引挡不住空格变体，所以这里显式按 `TRIM(username)` 查一遍。
    """
    name = (username or "").strip()
    if not name or len(name) > 64:
        return None
    with _conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT id FROM users WHERE TRIM(username)=%s LIMIT 1", (name,))
                if cur.fetchone():
                    return None
                cur.execute(
                    "INSERT INTO users (name, username, password_hash) VALUES (%s, %s, %s)",
                    (name, name, hash_password(password)),
                )
                uid = int(cur.lastrowid)
                conn.commit()
                return {"user_id": uid, "username": name}
            except pymysql.err.IntegrityError:
                conn.rollback()
                return None


ADMIN_USERNAME = "admin"


class DeleteUserError(RuntimeError):
    """删除用户的业务拒绝（由路由映射成 4xx）。code 供路由区分状态码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# 删除用户时按「子表 → 父表」清空的业务表
_CASCADE_TABLES = [
    "chat_messages", "conversations", "verification_codes", "operation_logs",
    "notifications", "pending_settlements", "holdings", "subscriptions",
    "payees", "cards", "accounts", "idempotency_records", "transfer_daily_quota",
]


def _purge_assets(cur, uid: int) -> dict:
    """删除某 uid 名下**全部业务数据**（不含 users 行），返回逐表行数。

    调用方负责事务边界与用户行校验。抽出来是为了让三处共用同一套表清单：
      - admin 删用户（delete_user_cascade）
      - 注册时清理「uid 复用」残留（purge_user_assets / count_user_assets）
      - 排查用的行数统计
    ★ `transactions` 没有 user_id 列，靠账户 id 收口；
      `chat_messages.user_id` 允许为 NULL，靠会话 id 收口。
    """
    uid = int(uid)
    counts: dict[str, int] = {}

    cur.execute("SELECT id FROM accounts WHERE user_id=%s", (uid,))
    acct_ids = [int(r["id"]) for r in cur.fetchall()]
    cur.execute("SELECT conversation_id FROM conversations WHERE user_id=%s", (uid,))
    conv_ids = [r["conversation_id"] for r in cur.fetchall()]

    if acct_ids:
        ph = ",".join(["%s"] * len(acct_ids))
        cur.execute(f"DELETE FROM transactions WHERE account_id IN ({ph})", acct_ids)
        counts["transactions"] = cur.rowcount
    if conv_ids:
        ph = ",".join(["%s"] * len(conv_ids))
        cur.execute(f"DELETE FROM chat_messages WHERE conversation_id IN ({ph})", conv_ids)
        counts["chat_messages"] = cur.rowcount

    for t in _CASCADE_TABLES:
        cur.execute(f"DELETE FROM {t} WHERE user_id=%s", (uid,))
        counts[t] = counts.get(t, 0) + cur.rowcount

    return {k: v for k, v in counts.items() if v}


def count_user_assets(user_id: int) -> int:
    """该 uid 名下业务数据总行数（0 = 干净）。注册时用来判断「uid 复用残留」。"""
    uid = int(user_id)
    total = 0
    with _conn() as conn:
        with conn.cursor() as cur:
            for t in ("accounts", "cards", "payees", "holdings", "subscriptions",
                      "pending_settlements", "notifications", "operation_logs",
                      "verification_codes", "conversations", "idempotency_records",
                      "transfer_daily_quota"):
                cur.execute(f"SELECT COUNT(*) AS n FROM {t} WHERE user_id=%s", (uid,))
                total += int(cur.fetchone()["n"])
            cur.execute(
                "SELECT COALESCE(SUM(c),0) AS n FROM ("
                "  SELECT COUNT(*) AS c FROM transactions t JOIN accounts a ON a.id=t.account_id"
                "   WHERE a.user_id=%s"
                "  UNION ALL SELECT COUNT(*) FROM chat_messages m WHERE m.user_id=%s"
                "   OR m.conversation_id IN (SELECT conversation_id FROM conversations"
                "                             WHERE user_id=%s)) x",
                (uid, uid, uid))
            total += int(cur.fetchone()["n"])
    return total


def purge_user_assets(user_id: int) -> dict:
    """清空某 uid 的全部业务数据但**保留 users 行**（单事务）。

    ★ 用途：注册时发现「全新账号却已有账户记录」= 上一个主人被删除时数据没清干净
      （uid 被 MySQL 复用）。必须先把残留清空，否则新用户会继承死账号的余额、
      卡、收款人、流水、订阅 —— 也就是 `provision_new_user` 幂等闸门静默跳过的那个坑。
    """
    uid = int(user_id)
    with _conn() as conn:
        with conn.cursor() as cur:
            counts = _purge_assets(cur, uid)
        conn.commit()
    return {
        "user_id": uid,
        "deleted": counts,
        "deleted_total": sum(counts.values()),
    }


def delete_user_cascade(user_id: int, *, allow_admin: bool = False) -> dict:
    """物理删除用户及其全部业务数据（单事务），返回逐表删除行数。

    为什么要在应用层再显式删一遍，而不是只 `DELETE FROM users`：
      1) 外键（`sql/upgrade_delete_cascade.sql`）只在**执行过该脚本的库**上生效，
         显式删除让本函数在未迁移的旧库上同样正确；
      2) 逐表行数可用于接口回执与审计（admin_logs 记录删了什么）；
      3) 「最后一个管理员 / 管理员需显式确认」这类保护必须写在代码里，别指望 DB。

    ★ 刻意不动的表（平台级 / 审计留痕）：
      `admin_logs`（无 user_id，审计不可删）、`bank_directory`、`products`、
      `knowledge_chunks`、`system_config`、`external_accounts`。
    """
    uid = int(user_id)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, username, role FROM users WHERE id=%s", (uid,))
            row = cur.fetchone()
            if not row:
                raise DeleteUserError("not_found", "用户不存在")
            role = row.get("role") or "user"
            if role == "admin":
                cur.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'")
                if int(cur.fetchone()["n"]) <= 1:
                    raise DeleteUserError(
                        "last_admin", "这是库里最后一个管理员账号，删除后将无人能登录运营后台")
                if not allow_admin:
                    raise DeleteUserError(
                        "admin_confirm", "目标账号是管理员，需显式确认（allow_admin=true）才能删除")

            counts = _purge_assets(cur, uid)
            cur.execute("DELETE FROM users WHERE id=%s", (uid,))
            counts["users"] = cur.rowcount
        conn.commit()

    return {
        "user_id": uid,
        "username": row.get("username") or ("user%s" % uid),
        "name": row.get("name"),
        "role": role,
        "deleted": counts,
        "deleted_total": sum(counts.values()),
    }


def ensure_admin_account() -> None:
    """C5：幂等补齐管理员种子账号 admin / admin123（role=admin）。

    SQL 升级脚本（sql/upgrade_inbox_admin.sql）只负责把已有 admin 的 role 拉回
    admin；哈希必须由代码生成（pbkdf2 随机盐）。仅演示环境使用，README 已注明。
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, password_hash, role FROM users WHERE username=%s",
                        (ADMIN_USERNAME,))
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO users (name, username, password_hash, role) "
                    "VALUES (%s, %s, %s, 'admin')",
                    ("管理员", ADMIN_USERNAME, hash_password(config.ADMIN_PASSWORD)),
                )
                print("[auth] 已创建管理员种子账号 admin / admin123（演示环境）")
            else:
                if row.get("role") != "admin":
                    cur.execute("UPDATE users SET role='admin' WHERE id=%s", (row["id"],))
                    print(f"[auth] 用户 {ADMIN_USERNAME}(#{row['id']}) 已提升为 admin")
        conn.commit()


def get_user_locked(user_id: int) -> bool:
    """C6 安全修复配套：用户是否被管理员锁定（锁定 → 旧 token 也一律拒绝）。"""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(locked, 0) AS locked FROM users WHERE id=%s", (int(user_id),)
            )
            row = cur.fetchone()
    return bool(row and row["locked"])


def get_user_role(user_id: int) -> str | None:
    """读取用户角色（admin_guard 用）。用户不存在返回 None。"""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE id=%s", (int(user_id),))
            row = cur.fetchone()
    return (row or {}).get("role")


if __name__ == "__main__":
    import os
    uid = int(os.getenv("USER_ID", config.DEFAULT_USER_ID))
    print("profile payload:", json.dumps(get_profile_payload(uid), ensure_ascii=False, indent=2))
    print("user_context   :", json.dumps(get_user_context(uid), ensure_ascii=False, indent=2))
    print("greeting       :", generate_greeting(uid))
