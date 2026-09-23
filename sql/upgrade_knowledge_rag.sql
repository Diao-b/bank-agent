-- =====================================================================
-- upgrade_knowledge_rag.sql —— 知识库 RAG 检索索引（C7 后续：真接线）
--   向量库选型：不引入独立向量库服务（7 篇文档 ≈ 几十个片段，暴力余弦
--   微秒级；Chroma/faiss 在此规模是负资产）。向量以 JSON 存 MySQL，
--   检索在 utils/knowledge.py 内做余弦 top-k。
--   embedding：百炼 text-embedding-v3（复用 DASHSCOPE_API_KEY，HTTP 直调，
--   不需要 SDK）；网络不可用时自动降级为关键词匹配打分。
-- =====================================================================

USE bank_agent;

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    doc_name    VARCHAR(128) NOT NULL,              -- 所属文档（docs/knowledge/*.md）
    heading     VARCHAR(255)          DEFAULT '',   -- 片段标题（## 小节名）
    content     TEXT         NOT NULL,              -- 片段正文
    chunk_index INT          NOT NULL DEFAULT 0,    -- 文档内序号
    embedding   JSON                  DEFAULT NULL, -- 向量（JSON 数组；NULL=关键词降级）
    embed_model VARCHAR(64)           DEFAULT '',    -- 生成向量所用的模型（text-embedding-v3 / keyword）
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_kc_doc (doc_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
