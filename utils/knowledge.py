"""utils/knowledge.py —— 知识库 RAG 检索层（真接线）

向量库选型：不引入独立向量库服务。7 篇文档按 ## 小节切分 ≈ 几十个片段，
向量以 JSON 存 MySQL knowledge_chunks 表，检索做暴力余弦 top-k ——
此规模下任何向量数据库（Chroma/faiss）都是负资产。

embedding：百炼 text-embedding-v3（复用 DASHSCOPE_API_KEY，requests 直调，
不需要 SDK）。网络不可用 / key 未配置时，rebuild 落库 embedding=NULL，
search 自动降级为「query 与片段的字符 bigram 重合度」打分 —— 检索永不中断。

对外接口：
    knowledge.rebuild()            —— 重切分全部文档 + 重新嵌入（管理端"重建索引"）
    knowledge.search(query, top_k) —— 检索片段 [{doc, heading, content, score, model}]
"""
from __future__ import annotations

import json
import math
import re
import socket
import threading
import time
from collections import OrderedDict
from pathlib import Path

import requests

import config
from mock_bank import db  # 连接收口到单一源（组 R）

KNOWLEDGE_DIR = Path(config.BASE_DIR).parent / "docs" / "knowledge"

EMBED_MODEL = "text-embedding-v3"
EMBED_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
EMBED_DIM = 1024
CHUNK_MAX_CHARS = 1200          # 单片段上限（超长再按空行二切）

# ★ 相似度下限。原来 0.30 太低 —— text-embedding-v3 的余弦整体偏高，实测「你好呀」
#   这种寒暄都能拿到 0.46~0.47（命中「诈骗话术识别」「不代答清单」等完全不相关的片段），
#   白塞几百字进提示词：既污染口径，又拖慢生成。
#   选值依据（scripts/_knowledge_check.py 实测）：噪声类最高 0.636（「查一下我的余额」命中「余额不足处理」），
#   真规则类最低 0.713，0.65 正好卡在两者之间 —— 12 个用例 0 误判。
SCORE_FLOOR = float(getattr(config, "KNOWLEDGE_SCORE_FLOOR", 0.65))

# ★ 交互路径的嵌入超时：检索是**在回答的关键路径上**的同步调用，30s 太长 ——
#   宁可 4s 超时降级关键词检索，也不能把一次回复拖到半分钟。
EMBED_QUERY_TIMEOUT = float(getattr(config, "KNOWLEDGE_EMBED_TIMEOUT", 4))
EMBED_REBUILD_TIMEOUT = 30      # 重建索引是管理端动作，可以耐心等

# ★ 连接复用：以前每次 requests.post 都新建连接（TCP + TLS 握手），
#   实测单次检索 350ms 起步、首次可达 11s。Session 复用后降到几十毫秒。
_SESSION = requests.Session()

# 纯寒暄 / 客套：这类输入不可能命中知识库，直接跳过检索 —— 省掉一次网络嵌入调用
_SKIP_RE = re.compile(
    r"^\s*(你好|您好|hi|hello|哈喽|嗨|在吗|在么|在不在|早上好|下午好|晚上好|早|"
    r"谢谢|多谢|感谢|好的|好嘞|嗯|哦|ok|收到|再见|拜拜|没事了)[呀啊哦呢哈啦呗吧么嘛。，!！~～\s]*$",
    re.I,
)


# --------------------------------------------------------------------------- #
#  切分：按 ## 小节 → 片段；超长片段按空行二切
# --------------------------------------------------------------------------- #
def _chunk_text(name: str, text: str) -> list[dict]:
    doc_title = name.replace(".md", "")
    parts = re.split(r"\n(?=## )", text)
    chunks: list[dict] = []
    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        heading_m = re.match(r"^##\s+(.+)", part)   # 只取标题行（勿加 $ 锚，正文随行后）
        heading = heading_m.group(1).strip() if heading_m else (doc_title if i == 0 else f"{doc_title}·概述")
        # 超长片段二次切分
        segs = [part]
        if len(part) > CHUNK_MAX_CHARS:
            segs, buf = [], ""
            for para in part.split("\n\n"):
                if len(buf) + len(para) > CHUNK_MAX_CHARS and buf:
                    segs.append(buf)
                    buf = para
                else:
                    buf = (buf + "\n\n" + para).strip()
            if buf:
                segs.append(buf)
        for j, seg in enumerate(segs):
            chunks.append({
                "doc_name": name,
                "heading": heading + (f"（{j + 1}）" if len(segs) > 1 else ""),
                "content": seg,
                "chunk_index": len(chunks),
            })
    # 过短碎片（<80 字，通常是纯文档标题行）并入下一个片段，避免检索噪声
    merged: list[dict] = []
    for c in chunks:
        if len(c["content"]) < 80 and merged:
            merged[-1]["content"] = c["content"] + chr(10) + merged[-1]["content"]
            merged[-1]["heading"] = c["heading"] + " · " + merged[-1]["heading"]
        elif len(c["content"]) < 80:
            continue
        else:
            merged.append(c)
    for k, c in enumerate(merged):
        c["chunk_index"] = k
    return merged


# --------------------------------------------------------------------------- #
#  embedding：百炼 text-embedding-v3（HTTP 直调）；失败返回 None → 降级
# --------------------------------------------------------------------------- #
def _embed_batch(texts: list[str], timeout: float = EMBED_REBUILD_TIMEOUT) -> list[list[float]] | None:
    """批量嵌入。timeout：交互检索路径请传 EMBED_QUERY_TIMEOUT（短超时 + 降级）。"""
    key = config.DASHSCOPE_API_KEY
    if not key:
        return None
    try:
        resp = _SESSION.post(                      # ★ Session 复用连接（省 TCP+TLS 握手）
            EMBED_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": EMBED_MODEL,
                  "input": {"texts": texts[:10]},           # 接口单批上限 10
                  "parameters": {"dimension": EMBED_DIM}},
            timeout=timeout,
        )
        data = resp.json()
        out = data.get("output", {}).get("embeddings")
        if resp.status_code != 200 or not out:
            print(f"[knowledge] embedding 失败：{str(data)[:160]}")
            return None
        arrs = [None] * len(out)
        for item in out:
            arrs[item["text_index"]] = item["embedding"]
        return arrs
    except Exception as exc:  # noqa: BLE001
        print(f"[knowledge] embedding 异常（降级关键词检索）：{exc}")
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _bigrams(t: str) -> set[str]:
    t = re.sub(r"\s", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def _keyword_score(query: str, content: str) -> float:
    """降级检索：query 与片段的字符 bigram 重合率（0~1）。"""
    if not query:
        return 0.0
    qb = _bigrams(query)
    if not qb:
        return 0.0
    hits = sum(1 for g in qb if g in content)
    return hits / len(qb)


# --------------------------------------------------------------------------- #
#  rebuild：重切分 + 重新嵌入 + 落库（管理端"重建索引"的真身）
# --------------------------------------------------------------------------- #
def rebuild() -> dict:
    if not KNOWLEDGE_DIR.exists():
        raise FileNotFoundError(f"知识库目录不存在：{KNOWLEDGE_DIR}")

    chunks: list[dict] = []
    for md in sorted(KNOWLEDGE_DIR.glob("*.md")):
        if md.name == "README.md":
            continue
        chunks.extend(_chunk_text(md.name, md.read_text(encoding="utf-8")))

    # 批量嵌入（分批 ≤10）；失败 → 全部落 NULL，search 走关键词降级
    # ★ 嵌入文本 = 文档名 + 小节标题 + 正文：结构化信号显著提升检索精度
    #   （否则泛化"概述"片段会在余弦排序里压过正主题节）
    embeddings: list[list[float] | None] = [None] * len(chunks)
    model_used = "keyword"
    for start in range(0, len(chunks), 10):
        batch = [c["doc_name"] + " " + c["heading"] + chr(10) + c["content"][:2000]
                 for c in chunks[start:start + 10]]
        arrs = _embed_batch(batch)
        if arrs:
            model_used = EMBED_MODEL
            for k, arr in enumerate(arrs):
                embeddings[start + k] = arr

    db.execute("DELETE FROM knowledge_chunks")
    for c, emb in zip(chunks, embeddings):
        db.execute(
            "INSERT INTO knowledge_chunks (doc_name, heading, content, chunk_index, embedding, embed_model) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (c["doc_name"], c["heading"], c["content"], c["chunk_index"],
             json.dumps(emb) if emb else None, model_used if emb else "keyword"),
        )

    stats = {"docs": len({c['doc_name'] for c in chunks}), "chunks": len(chunks), "model": model_used}
    print(f"[knowledge] 检索索引已重建：{stats}")
    return stats


# --------------------------------------------------------------------------- #
#  search：query → 向量（或关键词降级）→ 余弦 top-k
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  进程内缓存：向量全表 + query→结果
#  ★ 为什么必须缓存：read_agent 的 _llm_node **每一轮工具调用都会调一次检索**
#    （一次提问常见 2~3 轮），每轮原先都要
#      (a) 全表 SELECT + json.loads 33 条 × 1024 维向量
#      (b) 一次到百炼的嵌入 HTTP（每次都新建连接）
#    缓存后：同一轮的第 2/3 次完全免费，跨轮问同样的话也免费。
# --------------------------------------------------------------------------- #
_chunk_cache: list[dict] = []
_chunk_cache_at = 0.0
_chunk_cache_count = -1
_CHUNK_TTL_S = 30.0              # 兜底刷新（管理端重建索引后最多 30s 生效）
_query_cache: "OrderedDict[str, list[dict]]" = OrderedDict()
_QUERY_CACHE_MAX = 256
# query → 向量（单独一层：prefetch 可以只算向量，search 直接复用）
_qemb_cache: "OrderedDict[str, list[float] | None]" = OrderedDict()
_QEMB_MAX = 128


def _query_embedding(query: str) -> list[float] | None:
    """取 query 向量（带缓存）。

    ★ 实测：`dashscope.aliyuncs.com` 的**首次 DNS 解析要 ~11 秒**（之后 1ms），
      首次 TCP+TLS + 请求也要 200~280ms。不预热的话这十几秒会结结实实砸在
      用户的**第一句话**上（用户反馈"问一句要等一大会儿"）。
      故：① 进程启动 warmup() 预热 DNS；② 每句话在后台 prefetch() 提前算向量；
      ③ 缓存，同一轮里 read_agent 的 2~3 次检索只付一次网络代价。
    """
    key = query[:2000]
    if key in _qemb_cache:
        _qemb_cache.move_to_end(key)
        return _qemb_cache[key]
    arr = _embed_batch([key], timeout=EMBED_QUERY_TIMEOUT)
    emb = arr[0] if arr else None
    _qemb_cache[key] = emb            # ★ 失败也缓存（None），本轮别再反复重试拖时间
    while len(_qemb_cache) > _QEMB_MAX:
        _qemb_cache.popitem(last=False)
    return emb


def prefetch(query: str) -> None:
    """★ 预热一句话的检索（后台线程调用）。

    把"算这段文本的向量"提前到**回答开始之前**，这样等 read_agent 真正需要检索时，
    向量已在缓存里 —— 网络耗时被藏到「意图理解」那次 LLM 调用背后，用户无感。
    """
    q = (query or "").strip()
    if not q or _SKIP_RE.match(q):
        return
    try:
        _query_embedding(q)
        _chunks()                     # 顺带把向量表也读进内存
    except Exception:  # noqa: BLE001
        pass


def warmup() -> None:
    """进程启动时后台预热：解析 DNS + 建一次连接 + 载入向量表。

    实测首次 `getaddrinfo("dashscope.aliyuncs.com")` ≈ 11s（之后 1ms）——
    必须抢在用户第一句话之前化解掉。失败静默，绝不影响启动。
    """
    def _run() -> None:
        try:
            socket.getaddrinfo("dashscope.aliyuncs.com", 443)
        except Exception:  # noqa: BLE001
            pass
        try:
            _embed_batch(["warmup"], timeout=8)
        except Exception:  # noqa: BLE001
            pass
        try:
            _chunks()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_run, daemon=True, name="knowledge-warmup").start()


def _chunks() -> list[dict]:
    """片段 + 已解析向量（进程内缓存；条数变化或超过 TTL 则刷新）。"""
    global _chunk_cache, _chunk_cache_at, _chunk_cache_count
    try:
        row = db.fetch_one("SELECT COUNT(*) AS n FROM knowledge_chunks")
        n = int((row or {}).get("n") or 0)
    except Exception:  # noqa: BLE001
        n = -1
    if _chunk_cache and (time.time() - _chunk_cache_at < _CHUNK_TTL_S) and n == _chunk_cache_count:
        return _chunk_cache
    rows = db.fetch_all("SELECT doc_name, heading, content, embedding FROM knowledge_chunks")
    out: list[dict] = []
    for r in rows:
        emb = None
        if r.get("embedding"):
            try:
                emb = json.loads(r["embedding"])
            except Exception:  # noqa: BLE001
                emb = None
        out.append({"doc_name": r["doc_name"], "heading": r["heading"],
                    "content": r["content"], "emb": emb})
    _chunk_cache, _chunk_cache_at, _chunk_cache_count = out, time.time(), n
    _query_cache.clear()          # 索引变了 → 历史检索结果全部失效
    return out


def search(query: str, top_k: int = 3) -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    # ★ 纯寒暄 / 客套直接跳过：不可能命中知识库，省掉一次网络嵌入
    #   （实测这类请求白等 0.35s~11s，还会塞进一堆不相干片段）
    if _SKIP_RE.match(query):
        return []

    ck = f"{query}\x00{top_k}"
    cached = _query_cache.get(ck)
    if cached is not None:
        _query_cache.move_to_end(ck)
        return cached

    chunks = _chunks()
    if not chunks:
        return []

    # 只有这里才发网络请求，且用**短超时**；连不上就退化成本地 bigram 打分（检索永不中断）
    t0 = time.perf_counter()
    q_emb = _query_embedding(query)
    scored: list[dict] = []
    for r in chunks:
        if q_emb and r.get("emb"):
            try:
                score = _cosine(q_emb, r["emb"])
                model = "vector"
            except Exception:  # noqa: BLE001
                score = _keyword_score(query, r["content"])
                model = "keyword"
        else:
            score = _keyword_score(query, r["content"])
            model = "keyword"
        scored.append({
            "doc": r["doc_name"], "heading": r["heading"],
            "content": r["content"], "score": round(score, 4), "model": model,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = [s for s in scored[:top_k] if s["score"] >= SCORE_FLOOR]
    _query_cache[ck] = top
    while len(_query_cache) > _QUERY_CACHE_MAX:
        _query_cache.popitem(last=False)
    # 只在「慢到值得注意」时记一行 —— 检索卡在回答关键路径上，出问题要一眼看得到
    took_ms = (time.perf_counter() - t0) * 1000
    if took_ms > 150:
        print(f"[knowledge] 检索 {took_ms:.0f}ms（未命中缓存/网络嵌入）query={query[:24]}")
    return top


def snippets_for_prompt(query: str, top_k: int = 3) -> str:
    """拼装注入系统提示词的知识片段（空结果返回空串）。"""
    try:
        hits = search(query, top_k=top_k)
    except Exception:  # noqa: BLE001
        return ""
    if not hits:
        return ""
    blocks = "\n---\n".join(
        f"【{h['doc']}·{h['heading']}】\n{h['content']}" for h in hits
    )
    return (
        "【知识库检索片段（回答事实类问题时以下列原文为口径，禁止编造）】\n" + blocks
    )
