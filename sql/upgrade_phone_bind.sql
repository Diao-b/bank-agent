-- ============================================================================
--  AI 银行智能体 —— 渐进式账户：手机号绑定体系增量脚本（MySQL 8.0）
--  配套需求：未绑手机号的用户可对话/浏览，但资金类写操作一律拦截
--
--  执行方式（幂等，可重复跑）：
--    mysql -u root -p bank_agent < sql/upgrade_phone_bind.sql
--  或：
--    python init_db.py --sql sql/upgrade_phone_bind.sql
--
--  设计要点：
--    1) users 新增两列（幂等 ALTER）：
--         phone          VARCHAR(20) NULL  —— 完整手机号，**仅后端持有**，
--                                              任何对外响应只回脱敏 138****8888
--         phone_pending  VARCHAR(20) NULL  —— 绑定流程中暂存的待绑定号
--                                              （bind-confirm 成功才落 phone）
--       完整手机号为敏感字段，绝不进入对外 JSON；脱敏号由 phone 派生。
--    2) 存量演示用户（DEFAULT_USER_ID = 1）写入完整号 13800008888（视为已绑定）。
--    3) 新注册用户：provision_new_user / create_user 不初始化 phone（保持 NULL），
--       页面可浏览（账户/卡/流水照旧初始化），但资金写操作被 phone_guard 拦截。
-- ============================================================================

USE bank_agent;

-- --------------------------------------------------------------------------- --
--  1) users 表增量字段（幂等：先查 information_schema，缺哪列加哪列）
-- --------------------------------------------------------------------------- --
DROP PROCEDURE IF EXISTS add_phone_bind_columns;
DELIMITER //
CREATE PROCEDURE add_phone_bind_columns()
BEGIN
    -- phone：完整手机号，仅后端持有，对外只用脱敏号
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'users' AND column_name = 'phone'
    ) THEN
        ALTER TABLE users ADD COLUMN phone VARCHAR(20) NULL COMMENT '完整手机号，仅后端持有，对外只回脱敏号';
    END IF;

    -- phone_pending：绑定流程暂存（bind-request 写入，bind-confirm 成功则清空并落 phone）
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'users' AND column_name = 'phone_pending'
    ) THEN
        ALTER TABLE users ADD COLUMN phone_pending VARCHAR(20) NULL COMMENT '绑定中暂存的待绑定手机号';
    END IF;
END //
DELIMITER ;
CALL add_phone_bind_columns();
DROP PROCEDURE IF EXISTS add_phone_bind_columns;

-- --------------------------------------------------------------------------- --
--  2) 演示用户种子（DEFAULT_USER_ID = 1）：写入完整号，视为「已绑定」
--     脱敏号（phone_tail / phone_masked）由 13800008888 派生为 138****8888。
--     phone_pending 保持 NULL（不在绑定流程中）。
-- --------------------------------------------------------------------------- --
UPDATE users SET
    phone          = '13800008888',
    phone_pending  = NULL,
    phone_tail     = '138****8888'
WHERE id = 1;

-- --------------------------------------------------------------------------- --
--  3) 自检：确认识别列就位、演示用户已绑定
-- --------------------------------------------------------------------------- --
SELECT id, name AS username, phone, phone_pending, phone_tail
FROM users WHERE id = 1;
