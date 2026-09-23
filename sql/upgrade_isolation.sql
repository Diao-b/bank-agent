-- ============================================================================
--  AI 银行智能体 —— 多用户数据隔离增量脚本（MySQL 8.0）
--
--  配套需求：全量数据隔离 / 注册即初始化 / 资产 CRUD
--
--  执行方式（幂等，可重复跑）：
--    mysql -u root -p bank_agent < sql/upgrade_isolation.sql
--  或：
--    python init_db.py --sql sql/upgrade_isolation.sql
--
--  幂等设计：
--    ADD COLUMN / ADD INDEX 在 MySQL 里都没有 IF NOT EXISTS，
--    故统一用存储过程 + information_schema 条件判断，保证重复执行不报错。
--
--  ★ 铁律：products（理财目录）是平台级商品，本脚本【不】给它加 user_id，
--    也【不】加任何按用户过滤的索引。用户的理财资产在 holdings 表。
-- ============================================================================

USE bank_agent;

DROP PROCEDURE IF EXISTS upgrade_isolation;
DELIMITER //
CREATE PROCEDURE upgrade_isolation()
BEGIN
    -- ------------------------------------------------------------------ --
    --  1) cards.account_id：申请新卡时"挂到主账户"
    --     NULL 允许：历史卡片（init.sql 种子）没有归属账户概念，保持 NULL 不回填
    -- ------------------------------------------------------------------ --
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'cards' AND column_name = 'account_id'
    ) THEN
        ALTER TABLE cards
            ADD COLUMN account_id BIGINT NULL COMMENT '关联账户（申请新卡时挂主账户）' AFTER user_id;
    END IF;

    -- ------------------------------------------------------------------ --
    --  2) cards.deleted_at：软删除留痕（status='deleted'）
    -- ------------------------------------------------------------------ --
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'cards' AND column_name = 'deleted_at'
    ) THEN
        ALTER TABLE cards
            ADD COLUMN deleted_at DATETIME NULL COMMENT '软删除时间（status=deleted）';
    END IF;

    -- ------------------------------------------------------------------ --
    --  3) subscriptions.source：'system'=系统初始化 / 'manual'=用户手动录入
    --     前端据此给手动添加的订阅打"手动"徽标
    -- ------------------------------------------------------------------ --
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'subscriptions' AND column_name = 'source'
    ) THEN
        ALTER TABLE subscriptions
            ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'system'
            COMMENT 'system=系统初始化 manual=用户手动录入';
    END IF;

    -- ------------------------------------------------------------------ --
    --  4) 隔离查询热路径索引
    --     transactions 自身没有 user_id，靠 (account_id, date) 走，再由
    --     accounts.user_id 收敛范围，已有 idx_txn_account_date，这里只补 cards
    -- ------------------------------------------------------------------ --
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = 'bank_agent' AND table_name = 'cards' AND index_name = 'idx_cards_user_status'
    ) THEN
        ALTER TABLE cards ADD KEY idx_cards_user_status (user_id, status);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = 'bank_agent' AND table_name = 'operation_logs' AND index_name = 'idx_oplog_user'
    ) THEN
        ALTER TABLE operation_logs ADD KEY idx_oplog_user (user_id, created_at);
    END IF;
END //
DELIMITER ;

CALL upgrade_isolation();
DROP PROCEDURE IF EXISTS upgrade_isolation;

-- ---------------------------------------------------------------------------
--  自检：确认列已就位（重复执行结果一致）
-- ---------------------------------------------------------------------------
SELECT '--- cards 新增列 ---' AS check_item;
SELECT column_name, column_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'bank_agent' AND table_name = 'cards'
  AND column_name IN ('account_id', 'deleted_at')
ORDER BY column_name;

SELECT '--- subscriptions 新增列 ---' AS check_item;
SELECT column_name, column_type, column_default
FROM information_schema.columns
WHERE table_schema = 'bank_agent' AND table_name = 'subscriptions'
  AND column_name = 'source';

SELECT '--- products 应保持无 user_id ---' AS check_item;
SELECT COUNT(*) AS user_id_column_count
FROM information_schema.columns
WHERE table_schema = 'bank_agent' AND table_name = 'products' AND column_name = 'user_id';
