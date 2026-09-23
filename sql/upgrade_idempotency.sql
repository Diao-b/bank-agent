-- sql/upgrade_idempotency.sql —— 写操作幂等记录表（Phase 4：幂等执行）
--
-- ★ 解决的问题：写端点（转账/申购/赎回/卡/订阅）此前**零幂等保护** ——
--   网络重试、重复点击、Agent 侧重入同一图运行，都会真实重复扣款。
--   现在由客户端带 `Idempotency-Key` 头，服务端按 (user_id, key) 去重：
--     已完成 → 回放原响应；处理中 → IDEMPOTENT_IN_FLIGHT；
--     key 复用但请求体不同 → IDEMPOTENT_KEY_REUSED（防误用同一 key 打不同操作）。
--
-- ★ 本脚本**幂等**（IF NOT EXISTS + FK 存在性判断），可重复执行；
--   由 init_db.py 建库后顺序执行 sql/upgrade_*.sql 自动应用。
--
-- 语义约定：
--   status = 'pending'  → 占位已插、业务未终态（并发抢锁用）
--   status = 'done'     → 业务成功，response_json 为终态回执（可回放）
--   ★ 业务失败**不落 done**：占位会被删除，允许用户修正后带同一 key 重试。
--     （资金类幂等的价值在于"成功的那一笔只发生一次"，失败不该锁死重试。）

CREATE TABLE IF NOT EXISTS idempotency_records (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    idem_key      VARCHAR(64)  NOT NULL COMMENT '客户端幂等键（Idempotency-Key 头）',
    user_id       BIGINT       NOT NULL COMMENT '归属用户（★ 按用户隔离，A 的 key 不影响 B）',
    action        VARCHAR(64)  NOT NULL COMMENT '动作名（同响应协议 action 字段）',
    request_hash  CHAR(64)     NOT NULL COMMENT 'sha256(method+path+body)，防同 key 打不同请求',
    response_json JSON         NULL     COMMENT '终态回执（仅成功写入；NULL=处理中）',
    status        VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending / done',
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at    DATETIME     NOT NULL COMMENT '过期时间（过期后可重新执行）',
    PRIMARY KEY (id),
    -- ★ 去重锚点：同一用户 + 同一 key 只能有一条（并发下由唯一索引串行化）
    UNIQUE KEY uk_idem_user (user_id, idem_key),
    KEY idx_expires (expires_at),
    -- ★ 外键内联在 CREATE TABLE 里：随 CREATE TABLE IF NOT EXISTS 一并生效，
    --   不依赖 SET @ddl + PREPARE 的多语句会话状态，天然幂等。
    CONSTRAINT fk_idem_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='写操作幂等记录（Phase 4）';

-- 外键已内联在上面的 CREATE TABLE（见其注释）；此处无需再 ALTER。
