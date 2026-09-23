"""数字柜员「小微」桥接层（Fay 渲染端）。

设计铁律（来自 CONTRIBUTING.md 与集成规范）：
- 本项目为主、Fay 为「说话的皮」：本文件是本项目与 Fay 进程通信的唯一入口，
  业务域（agent/、utils 各域、mock_bank/）零改动。
- 所有方法 try/except 静默：Fay 未启动 / 挂掉 / 接口不符 → 直接跳过，绝不抛异常影响主流程。
- 配置全部来自 config（.env），禁止硬编码端口 / 路径。
- 物理隔离：Fay 跑在独立进程、独立 venv，HTTP 通信（见 docs/fay_integration.md）。

★ 接口契约（**已按本机 Fay 源码核对并实测**：gui/flask_server.py，Flask 监听 0.0.0.0:5000）：
- 喂话（逐字念出，verbatim）POST {FAY_API_URL}/transparent-pass
    form 字段 data = JSON 字符串：{"user": <str>, "text": <str>}
    返回 {"code":200,"message":"成功"}
    → 走 Fay 的「透传播放」通道：直接把 text 交给 TTS 播报，**不让 Fay 二次生成**。
      （对照：/api/send 是"用户提问→Fay 作答"，会把文本当提问、Fay 自己再答一遍，故不用。）
    → 该通道**无 emotion 入参**；情绪由 Fay 侧依据文本判定（Sentiment/Action，见下）。
- 打断 POST {FAY_API_URL}/to-stop-talking
    JSON 体：{"username": <str>}   → 清空该用户的播报音频流（stream_manager）
- 探活 GET  {FAY_API_URL}/api/get-system-status  → {"server":true,...}
- 情绪：Fay 的 HTTP 喂话接口不接收 emotion；其内部按文本调用情感 API（可选百度，
  需 baidu_emotion key）决定表情。故本文件的 `emotion` 参数仅用于本项目**本地日志/调试**，
  不随请求下发；EMOTION_MAP 保留为「如需改走 Fay 其它带情绪通道」的预留映射。
"""
from __future__ import annotations

import json
import logging
import re
import threading

import requests

import config

logger = logging.getLogger("digital_human")

# --------------------------------------------------------------------------- #
#  朗读文本口语化：把 Markdown 回复转成「适合念出来」的中文口语
#  ★ 只作用于**喂给 Fay 的朗读文本**；聊天区展示的原文一字不改。
#  实测依据（本机 edge_tts「晓晓」）：一次性喂全文口播覆盖率 95%，按句分块+queue 仅 35%，
#  故**必须单次喂话**，靠本函数把文本压短变顺，而不是分块。
# --------------------------------------------------------------------------- #
_MD_FENCE = re.compile(r"```.*?```", re.S)
_MD_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_TABLE = re.compile(r"^\s*\|.*\|\s*$", re.M)
_MD_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.M)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_MD_EMPHASIS = re.compile(r"\*\*|__")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF\u2190-\u21FF]")

# ---- 日期口语化：Markdown/表格里是 2026-09-01 ~ 09-20，直接念数字很难听 ----
# ★ 只认「0 补齐的两位月-日」（09-03 / 09-20）与「-」分隔：
#   放宽成 1-2 位会把「版本 1-2」「第 3-4 季度」误念成日期（实测踩到）。
_DATE_FULL = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_DATE_MD = re.compile(r"(?<![\d-])(\d{2})-(\d{2})(?![\d-])")
_RANGE_SEP = re.compile(r"\s*[~～]\s*")
# 金额/账户尾号等：¥ 要转成「元」并去掉无意义的小数 0（否则念成「点零零」）
_AMOUNT = re.compile(r"[¥￥]\s*([0-9][0-9,]*(?:\.\d+)?)")
# 千分位逗号：8,788 → 8788（否则会被念成「八逗号七八八」）
# 只认严格的「三位一组」，避免误伤「2026, 2027」这类并列数字
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
# ---- 表格转朗读 ----
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$")
_TABLE_MAX_ROWS = 6      # 超过则折叠成「等」，避免超长表格把播报拖到几十秒


def _amount_to_spoken(m: "re.Match") -> str:
    """¥8788.00 → 8788元；¥2969.34 → 2969.34元。

    ★ 不能只把 ¥ 去掉：`8788.00` 会被念成「八千七百八十八点零零」，
      `¥30.0` 念成「三十点零」。小数部分全为 0 时直接去掉小数。
    """
    num = m.group(1)
    if "." in num:
        ip, dec = num.split(".", 1)
        if set(dec) <= {"0"}:
            num = ip
    return f"{num}元"


def _date_to_spoken(m: "re.Match") -> str:
    """2026-09-01 → 2026年9月1日；09-20 → 9月20日（月/日非法则原样返回）。"""
    g = m.groups()
    if len(g) == 3:
        y, mo, d = int(g[0]), int(g[1]), int(g[2])
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return m.group(0)
        return f"{y}年{mo}月{d}日"
    mo, d = int(g[0]), int(g[1])
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return m.group(0)
    return f"{mo}月{d}日"


def _table_cells(line: str) -> list:
    """把 `| a | b |` 切成 ['a', 'b'] —— ★**保留空单元格**，以便按列序对齐表头。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _tables_to_speech(text: str) -> str:
    """Markdown 表格 → 可朗读文本。

    ★ 修正 1：以前是整段丢弃（`_MD_TABLE.sub("", …)`），结果「这个月钱都花哪了」回复里的
      **分类明细在语音里全没了**，用户反馈「语句说不完全」。现在改成逐行念。
    ★ 修正 2：**每格带上自己的表头**。只念值会出现「微众卡，8729，8788 元」——
      听众不知道 8729 是什么。改成「微众卡，尾号 8729，余额 8788 元」。
      规则：首列当行主语（不加表头），其余列前面挂上对应表头；表头为空则只念值。
      空单元格按**原始列序**跳过（如「合计」行的尾号是空的，不能让后面的余额错位）。
    """
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if _TABLE_ROW.match(lines[i]) or _TABLE_SEP.match(lines[i]):
            block = []
            while i < len(lines) and (_TABLE_ROW.match(lines[i]) or _TABLE_SEP.match(lines[i])):
                block.append(lines[i])
                i += 1
            sep_at = next((k for k, ln in enumerate(block) if _TABLE_SEP.match(ln)), -1)
            # 表头行：有分隔行时取分隔行之前那行；若无分隔行则把第一行当表头
            head_at = sep_at - 1 if sep_at > 0 else 0
            header = _table_cells(block[head_at]) if 0 <= head_at < len(block) else []
            rows = []
            for k, ln in enumerate(block):
                if _TABLE_SEP.match(ln) or k == head_at:
                    continue
                cells = _table_cells(ln)
                parts = []
                for ci, cell in enumerate(cells):
                    if not cell:
                        continue                      # 空格子跳过，但保留列序（不能像以前那样先过滤再拼）
                    label = header[ci].strip() if ci < len(header) else ""
                    parts.append(cell if (ci == 0 or not label) else f"{label} {cell}")
                if parts:
                    rows.append("，".join(parts) + "。")
            if rows:
                if len(rows) > _TABLE_MAX_ROWS:
                    rows = rows[:_TABLE_MAX_ROWS] + ["等。"]
                out.append("".join(rows))
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)
# 列表项：- / * / + / 1. / 1、 / 1) / 1．
_LIST_ITEM = re.compile(r"^\s*(?:[-*+•]|\d{1,3}\s*[.、)．])\s+(.*)$")
# 行内举例：比如"1"或者"看看这个月账单" → 只留第一个 + 等
# ★ 注意：这里存的是「引号字符本身」，不是已经带方括号的字符类（否则嵌进 [^...] 会写坏正则）
_Q_CHARS = "「『“\"'‘"
_QE_CHARS = "」』”\"'’"
_EXAMPLE = re.compile(
    r"(比如|例如|如)(\s*[" + _Q_CHARS + r"][^" + _QE_CHARS + r"]{1,24}[" + _QE_CHARS + r"])"
    r"(?:\s*(?:或者|还是|、|，|/)\s*[" + _Q_CHARS + r"][^" + _QE_CHARS + r"]{1,24}[" + _QE_CHARS + r"])+"
)

# 朗读节奏（本机实测：edge_tts 中文女声 ≈ 4 字/秒，含分块间隙约 260ms/字）
_SPEAK_MS_PER_CHAR = 260
_SPEAK_TAIL_MS = 1500


def to_speakable(text: str, max_list_items: int = 1) -> str:
    """Markdown 回复 → 适合朗读的口语文本。

    规则（应产品要求）：
    - 去掉 Markdown 标记（加粗 / 标题 / 引用 / 代码块 / 水平线 / 链接）
    - **表格转可朗读文本**：丢弃表头与分隔行，数据行用「，」连接（★以前是整段丢弃，
      导致账单分类明细在语音里全没了 → 用户反馈「语句说不完全」）
    - **日期口语化**：`2026-09-01 ~ 09-20` → `2026年9月1日到9月20日`
    - 去掉 emoji、残留方括号
    - **列表折叠**：连续列表项只念第 1 项，其余用「等」带过（`max_list_items` 可调；≤0 = 不折叠）
    - **行内举例折叠**：`比如"1"或者"看看这个月账单"` → `比如"1"等`
    - `——` 转逗号、压缩空行与重复标点
    展示用原文不受影响（本函数只用于喂话）。
    """
    if not text:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = _MD_FENCE.sub(" ", s)          # 代码块整体丢弃（念出来没意义）
    s = _MD_INLINE_CODE.sub(r"\1", s)
    s = _tables_to_speech(s)           # ★ 表格 → 逐行可朗读文本（不再整段丢弃）
    s = _MD_RULE.sub("", s)
    s = _MD_LINK.sub(r"\1", s)
    s = _MD_HEAD.sub("", s)
    s = _MD_QUOTE.sub("", s)
    s = _MD_EMPHASIS.sub("", s)
    s = _EMOJI.sub("", s)
    s = s.replace("——", "，").replace("—", "，").replace("–", "，")
    s = s.replace("[", "").replace("]", "").replace("【", "").replace("】", "")
    s = _DATE_FULL.sub(_date_to_spoken, s)     # 2026-09-01 → 2026年9月1日
    s = _DATE_MD.sub(_date_to_spoken, s)       # 09-20 → 9月20日
    s = _RANGE_SEP.sub("到", s)                # 「2026年9月1日 ~ 9月20日」→「…到…」
    s = _AMOUNT.sub(_amount_to_spoken, s)      # ¥8788.00 → 8788元（顺带去掉 .00）
    s = _THOUSANDS.sub("", s)                  # 8,788 → 8788

    # 列表折叠：连续列表项 → 只留前 max_list_items 项 + 「等」
    lines = s.split("\n")
    out, i = [], 0
    while i < len(lines):
        m = _LIST_ITEM.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        j, cnt = i + 1, 1
        while j < len(lines):
            if _LIST_ITEM.match(lines[j]):
                cnt += 1
                j += 1
            elif not lines[j].strip() and j + 1 < len(lines) and _LIST_ITEM.match(lines[j + 1]):
                j += 1                      # 列表项之间夹空行也算同一列表
            else:
                break
        keep = cnt if max_list_items <= 0 else min(cnt, max_list_items)
        k, picked, p = i, [], 0
        while k < j and p < keep:
            mm = _LIST_ITEM.match(lines[k])
            if mm:
                item = re.sub(r"[\s，,；;]+$", "", mm.group(1).strip())
                if item:
                    picked.append(item)
                    p += 1
            k += 1
        if picked:
            tail = "等。" if cnt > len(picked) else "。"
            out.append("，".join(picked) + tail)
        i = j

    s = "\n".join(out)
    s = _EXAMPLE.sub(r"\1\2等", s)                     # 行内举例只念第一个
    s = re.sub(r"\n{2,}", "。", s)                     # 段落 → 句号
    s = s.replace("\n", "，")                          # 换行 → 逗号
    s = re.sub(r"[（(]\s*[)）]", "", s)
    s = re.sub(r"[，,]{2,}", "，", s)
    s = re.sub(r"[。]{2,}", "。", s)
    s = re.sub(r"([：:])\s*[。]+", r"\1", s)           # 「如下：。」→「如下：」（段落转句号留下的）
    s = re.sub(r"[。]+\s*([：:])", r"\1", s)
    s = re.sub(r"([？。！；：])\s*[，,、]+", r"\1", s)    # 问号/句号后不留顿逗
    s = re.sub(r"。\s*[，,、]+", "。", s)
    s = re.sub(r"[，,、]\s*[。]{1,}", "。", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[ ]*([，。])[ ]*", r"\1", s)
    return s.strip(" \t，,。；;")


def strip_reply_emoji(text: str) -> str:
    """剥离模型回复里偶发的 emoji（全站 0 emoji 0 icon 铁律）。

    ★ 与 to_speakable 里的 `_EMOJI.sub` 不同：这里**保留项目自有的文字符号**
      （✓ ★ → 等，README 明确允许用文字字符承载状态），只清真正的表情符号。
      实测模型的回复里出现过「都给您查好了 👇」，语音侧本就念不出来，展示也不该留。
    """
    if not text:
        return ""
    allowed = set("✓✔✗✘★☆→←↑↓")
    out = []
    for ch in str(text):
        if ch not in allowed and _EMOJI.match(ch):
            continue
        out.append(ch)
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def estimate_speak_ms(text: str) -> int:
    """按「实际会念出来的字数」估算播报时长（毫秒），供前端决定何时重开麦克风。"""
    n = len(re.sub(r"\s+", "", to_speakable(text)))
    if n <= 0:
        return 0
    return int(min(60000, max(2000, n * _SPEAK_MS_PER_CHAR + _SPEAK_TAIL_MS)))

# 本项目情绪语义 → 预留映射（当前 Fay HTTP 喂话接口无 emotion 字段，仅供本地日志/未来扩展）。
EMOTION_MAP = {
    "normal": "normal",
    "happy": "happy",
    "serious": "serious",
    "sorry": "sorry",
}

# 情绪启发式关键词（不侵入业务域；按回执文本推导数字柜员语气，用于本地日志/调试）。
# 优先级：serious > happy > sorry > normal。
# 注意：勿放「余额」等泛词（余额查询是正常回复）；「余额不足」由「不足」命中。
_SERIOUS_KW = ("风控", "拦截", "无法", "不能", "拒绝", "异常", "失败", "不足",
               "错误", "未通过", "限额", "锁定", "锁")
_HAPPY_KW = ("成功", "已到账", "已完成", "到账", "办理成功", "已提交", "已为您",
             "已开通", "已绑定", "已解冻", "欢迎", "好的")
_SORRY_KW = ("抱歉", "对不起", "未能", "很遗憾", "请原谅", "请您")


class DigitalHumanClient:
    """Fay 渲染端客户端（单例）。所有对外调用静默失败。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        self.api_url = (config.FAY_API_URL or "").rstrip("/")
        self.frontend_url = (config.FAY_FRONTEND_URL or "").rstrip("/")
        self.ws_url = (getattr(config, "FAY_WS_URL", "") or "").rstrip("/")
        self.username = config.FAY_USERNAME or "xiaowei"
        self.speak_path = (config.FAY_SPEAK_PATH or "/api/send").rstrip("/")
        self.stop_path = (config.FAY_STOP_PATH or "/to-stop-talking").rstrip("/")
        self.health_path = (config.FAY_HEALTH_PATH or "/api/get-system-status").rstrip("/")
        self.timeout = float(config.FAY_API_TIMEOUT or 2)
        # 朗读文本口语化开关（默认开）：去 Markdown、列表只念前 N 项 + 「等」（≤0 = 不折叠）
        self.digest = bool(getattr(config, "FAY_SPEAK_DIGEST", True))
        self.list_items = int(getattr(config, "FAY_SPEAK_LIST_ITEMS", 1) or 0)
        # 总开关：未配置 FAY_API_URL → 全部 no-op（演示默认关）。
        self.enabled = bool(self.api_url)

    # ---- 公共 API（driver 层调用）----
    def is_ready(self) -> bool:
        """健康检查：GET /api/get-system-status，2s 超时。"""
        if not self.enabled:
            return False
        try:
            r = requests.get(self.api_url + self.health_path, timeout=min(2.0, self.timeout))
            return r.status_code < 500
        except Exception:
            return False

    def spoken_text(self, text: str) -> str:
        """真正会被念出来的文本（口语化后）。原文不受影响。"""
        if not text:
            return ""
        return to_speakable(text, self.list_items) if self.digest else str(text).strip()

    def strip_reply_emoji(self, text: str) -> str:
        """剥离模型回复里的 emoji（全站 0 emoji 0 icon 铁律）。

        ★ 与 spoken_text 里的去 emoji 不同：这里保留项目自有的文字符号（✓ ★ →），
          只清真正的表情符号 —— 展示文本要干净，但不能误伤状态字符。
        """
        return strip_reply_emoji(text)

    def estimate_speak_ms(self, text: str) -> int:
        """按**实际朗读文本**估算播报时长（毫秒）；0 = 无内容可念。

        前端「与小微对话」据此决定何时重开麦克风 —— 按原文长度估算会偏，
        而"麦克风开太早 → 听到它自己的尾音"正是误识别的根因，故由后端单点计算。
        """
        n = len(re.sub(r"\s+", "", self.spoken_text(text)))
        if n <= 0:
            return 0
        return int(min(60000, max(2000, n * _SPEAK_MS_PER_CHAR + _SPEAK_TAIL_MS)))

    def speak(self, text: str, emotion: str = "normal") -> None:
        """让数字柜员「逐字念出」text（verbatim）。Fay 未启动 / 异常 → 静默跳过。

        Fay 接口：POST /transparent-pass（透传播放），
        form 字段 data = {"user": <str>, "text": <str>} 的 JSON 字符串。
        用透传而非 /api/send：后者会把文本当"用户提问"，Fay 会自己再答一遍，不是"同一句话"。
        emotion 仅本地记录（Fay 侧按文本自判 Sentiment / 动作表情）。
        """
        if not self.enabled or not text or not text.strip():
            return
        # ★ 朗读文本口语化（去 Markdown / 列表折叠为「前 N 项 + 等」/ 去 emoji）。
        #   聊天区展示的原文不受影响；FAY_SPEAK_DIGEST=0 可关闭（排查用）。
        spoken = self.spoken_text(text)
        if not spoken:
            return
        try:
            payload = json.dumps({"user": self.username, "text": spoken})
            requests.post(
                self.api_url + self.speak_path,
                data={"data": payload},
                timeout=self.timeout,
            )
        except Exception:
            # 静默：Fay 未启动 / 挂掉 / 接口不符，不影响主流程
            pass

    def stop(self) -> None:
        """打断当前播报：POST /to-stop-talking，JSON {"username": ...}。"""
        if not self.enabled:
            return
        try:
            requests.post(
                self.api_url + self.stop_path,
                json={"username": self.username},
                timeout=self.timeout,
            )
        except Exception:
            pass

    # ---- SSE / 回复触发（driver 层调用）----
    def maybe_speak_event(self, item, dh_on: bool) -> None:
        """SSE 事件帧触发：仅 final reply（done.payload.reply）才开口。"""
        if not (dh_on and self.enabled) or not isinstance(item, dict):
            return
        if item.get("type") != "done":
            return
        reply = (item.get("payload") or {}).get("reply")
        if reply and isinstance(reply, str):
            self._speak_async(reply, self.derive_emotion(reply))

    def maybe_speak_reply(self, reply: str, dh_on: bool) -> None:
        """非流式 /api/chat 回复合口。"""
        if not (dh_on and self.enabled) or not reply or not isinstance(reply, str):
            return
        self._speak_async(reply, self.derive_emotion(reply))

    def _speak_async(self, text: str, emotion: str) -> None:
        """后台线程喂话，绝不阻塞调用方（SSE 生成器 / 请求处理）。"""
        threading.Thread(target=self.speak, args=(text, emotion), daemon=True).start()

    @staticmethod
    def derive_emotion(text: str) -> str:
        """按回执文本启发式推导情绪（不侵入业务域；用于本地日志/调试）。"""
        if not text:
            return "normal"
        for kw in _SERIOUS_KW:
            if kw in text:
                return "serious"
        for kw in _HAPPY_KW:
            if kw in text:
                return "happy"
        for kw in _SORRY_KW:
            if kw in text:
                return "sorry"
        return "normal"

    # ---- /fay 反向代理（run_server 调用）----
    def proxy(self, path: str, method: str = "GET", params: dict | None = None,
              headers: dict | None = None):
        """反向代理到 Fay 前端；不可达返回 None（调用方回落离线占位页）。"""
        if not self.frontend_url:
            return None
        url = self.frontend_url.rstrip("/") + "/" + (path or "").lstrip("/")
        try:
            return requests.request(
                method, url, params=params, headers=headers or {},
                stream=True, timeout=max(5.0, self.timeout * 3),
            )
        except Exception:
            return None


# 全局单例
digital_human = DigitalHumanClient()
