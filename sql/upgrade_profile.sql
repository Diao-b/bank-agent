-- ============================================================================
--  AI 银行智能体 —— 用户信息体系增量脚本（MySQL 8.0）
--  配套需求：个人信息管理 / Agent 感知用户 / 个性化问候语
--
--  执行方式（幂等，可重复跑）：
--    mysql -u root -p bank_agent < sql/upgrade_profile.sql
--  或：
--    python init_db.py --sql sql/upgrade_profile.sql
--
--  幂等设计：
--    1) users 新字段：先查 information_schema，缺哪列加哪列（ADD COLUMN 不自带
--       IF NOT EXISTS，故用存储过程 + 条件判断保证可重复执行）。
--    2) chat_messages 表：CREATE TABLE IF NOT EXISTS。
--    3) 演示种子：UPDATE ... WHERE id=1（存在即更新，不会重复插入）。
-- ============================================================================

USE bank_agent;

-- --------------------------------------------------------------------------- --
--  1) users 表增量字段
-- --------------------------------------------------------------------------- --
DROP PROCEDURE IF EXISTS add_profile_columns;
DELIMITER //
CREATE PROCEDURE add_profile_columns()
BEGIN
    -- real_name：真实姓名（可空；未填写为 NULL，禁止再用 '张同学' 之类的占位默认值兜底）
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'users' AND column_name = 'real_name'
    ) THEN
        ALTER TABLE users ADD COLUMN real_name VARCHAR(50) NULL;
    END IF;

    -- gender：male / female / other（可空；用户未选择时为 NULL，不得默认 male）
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'users' AND column_name = 'gender'
    ) THEN
        ALTER TABLE users ADD COLUMN gender VARCHAR(10) NULL;
    END IF;

    -- title_prefix：称呼前缀，由 gender 推导（male→先生 / female→女士）
    --   可空：gender 未选时不应凭空给出称呼
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'users' AND column_name = 'title_prefix'
    ) THEN
        ALTER TABLE users ADD COLUMN title_prefix VARCHAR(20) NULL;
    END IF;

    -- phone_tail：展示用尾号（可空；禁止编造 '****8888' 之类占位号，也禁止完整手机号入库）
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'users' AND column_name = 'phone_tail'
    ) THEN
        ALTER TABLE users ADD COLUMN phone_tail VARCHAR(20) NULL;
    END IF;

    -- occupation：职业，可空（学生）
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'users' AND column_name = 'occupation'
    ) THEN
        ALTER TABLE users ADD COLUMN occupation VARCHAR(50) NULL;
    END IF;

    -- preferences：JSON {risk_preference, interests[]}
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'users' AND column_name = 'preferences'
    ) THEN
        ALTER TABLE users ADD COLUMN preferences JSON NULL;
    END IF;

    -- updated_at：画像变更时间
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'users' AND column_name = 'updated_at'
    ) THEN
        ALTER TABLE users ADD COLUMN updated_at DATETIME NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP;
    END IF;
END //
DELIMITER ;
CALL add_profile_columns();
DROP PROCEDURE IF EXISTS add_profile_columns;

-- 【刻意不给任何画像字段设列级默认值】
--   画像类字段（real_name / gender / title_prefix / phone_tail / occupation / preferences）
--   一律"可空 + 无默认值"：用户没填就是 NULL，绝不替用户生成。
--   历史教训：这里曾给 real_name='张同学'、gender='male'、title_prefix='先生'、phone_tail='****8888'，
--   而 profile_db.create_user() 的 INSERT 不带这些字段 → 新用户凭空拥有
--   "姓名 + 性别 + 称呼 + 手机号"，等于编造用户数据。
--   另注：只 DROP DEFAULT 而保留 NOT NULL，INSERT 会改报 MySQL 1364
--   "Field 'xxx' doesn't have a default value"，务必把 NULL 一并放开。
--
-- 存量库归一：把早期定义为 NOT NULL DEFAULT 'xxx' 的画像列幂等改回"可空 + 无默认值"。
--   不显式写 COLLATE，让它继承表默认，兼容 MySQL 5.7 / 8.0。
ALTER TABLE users
    MODIFY COLUMN real_name    VARCHAR(50) CHARACTER SET utf8mb4 NULL,
    MODIFY COLUMN gender       VARCHAR(10) CHARACTER SET utf8mb4 NULL,
    MODIFY COLUMN title_prefix VARCHAR(20) CHARACTER SET utf8mb4 NULL,
    MODIFY COLUMN phone_tail   VARCHAR(20) CHARACTER SET utf8mb4 NULL;

-- --------------------------------------------------------------------------- --
--  2) chat_messages 表（个性化问候语 / 未来聊天记录持久化）
--     greeting 消息：role='assistant'，node_trace='[]'（无管道动画）
-- --------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS chat_messages (
    id               BIGINT       NOT NULL AUTO_INCREMENT,
    conversation_id  VARCHAR(64)  NOT NULL,
    role             VARCHAR(16)  NOT NULL COMMENT 'user/assistant/system',
    content          TEXT         NOT NULL,
    node_trace       JSON         NOT NULL DEFAULT ('[]') COMMENT '管道/节点轨迹，普通消息为空数组',
    kind             VARCHAR(20)  NOT NULL DEFAULT 'chat' COMMENT 'chat=普通 / greeting=问候语',
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_cm_conv  (conversation_id, kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- --------------------------------------------------------------------------- --
--  3) 演示用户种子（DEFAULT_USER_ID = 1）
--     username 保持 init.sql 的 '张小明'，real_name 设为演示姓名 '张明远'
-- --------------------------------------------------------------------------- --
UPDATE users SET
    real_name    = '张明远',
    gender       = 'male',
    title_prefix = '先生',
    phone_tail   = '138****8888',
    occupation   = '学生',
    preferences  = '{"risk_preference":"稳健","interests":["账单分析","订阅管理"]}',
    updated_at   = NOW()
WHERE id = 1;

-- 自检：确认演示用户的画像字段已就位
SELECT id, name AS username, real_name, gender, title_prefix, phone_tail, occupation, preferences
FROM users WHERE id = 1;
