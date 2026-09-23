-- ============================================================================
--  AI 银行智能体 —— 会话持久化增量脚本（MySQL 8.0）
--  配套审计项：M1（真实对话落库）/ M2（会话列表）
--
--  执行方式（幂等，可重复跑）：
--    python init_db.py --sql sql/upgrade_chat.sql
--  或：
--    mysql -u root -p bank_agent < sql/upgrade_chat.sql
--
--  背景：
--    audit_data.md 实测确认 —— POST /api/chat 返回的对话只进了 LangGraph 的
--    SQLite checkpoint，MySQL 完全不知情；chat_messages 表里长期只有
--    kind='greeting' 的问候语。本脚本补齐"会话列表"所需的 conversations 表。
--
--  注意：
--    本脚本不含 DELIMITER 块，以便 init_db.py 的语句切分器直接执行。
--    chat_messages 的 seq / user_id 增量列由 profile_db.ensure_schema() 在
--    应用启动时幂等补齐（ADD COLUMN 无 IF NOT EXISTS，Python 侧判断更稳）。
-- ============================================================================

USE bank_agent;

-- --------------------------------------------------------------------------- --
--  1) conversations：会话元数据（一条会话 = 一个前端 thread_id）
--     title 由首条用户消息自动截取；用户未发言前保持默认「新会话」
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS conversations (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    conversation_id VARCHAR(64)  NOT NULL COMMENT '前端 thread_id，如 web-9k3k4e',
    user_id         BIGINT       NOT NULL,
    title           VARCHAR(128) NOT NULL DEFAULT '新会话' COMMENT '首条用户消息截取，≤20 字',
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_conv_id (conversation_id),
    KEY idx_conv_user (user_id, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- --------------------------------------------------------------------------- --
--  2) 回填：把已有 greeting 记录登记进 conversations，避免成为孤儿会话
--     标题沿用默认「新会话」，等首条真实用户消息到达时自动改名
-- --------------------------------------------------------------------------- --
INSERT IGNORE INTO conversations (conversation_id, user_id, title)
SELECT DISTINCT cm.conversation_id, 1, '新会话'
FROM chat_messages cm
WHERE cm.conversation_id NOT IN (SELECT conversation_id FROM conversations);

-- --------------------------------------------------------------------------- --
--  自检
-- --------------------------------------------------------------------------- --
SELECT '--- conversations 表 ---' AS check_item;
SELECT id, conversation_id, user_id, title, created_at FROM conversations ORDER BY id;
