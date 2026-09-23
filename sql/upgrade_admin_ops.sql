-- =====================================================================
-- upgrade_admin_ops.sql —— 可运营后台（C6）
--   1) system_config：风控参数单源（rules.py 常量迁入 DB，启动时加载，
--      rules.get_rule(key) 运行时读取 —— 单一来源从"文件"变为"DB 表"）
--   2) admin_logs：管理员写操作审计（who / what / old→new / when，
--      pending_sync 标记 RAG 知识文档同步义务）
--   3) users.locked：管理员锁定登录（锁定后 authenticate 拒绝）
--   4) products.status：理财上架/下架（下架 → 用户端不可见不可买，
--      持仓不受影响）
-- 红线：用户资产（余额 / 流水 / 持仓 / 订阅）不设任何管理端修改通道。
-- =====================================================================

USE bank_agent;

-- ---------------------------------------------------------------- 风控参数
CREATE TABLE IF NOT EXISTS system_config (
    `key`             VARCHAR(64)  NOT NULL,
    value             VARCHAR(128) NOT NULL,
    label             VARCHAR(128) NOT NULL,
    value_type        VARCHAR(16)  NOT NULL DEFAULT 'int',      -- int | decimal
    min_value         DECIMAL(18,2)         DEFAULT NULL,       -- 允许范围（越界拒绝）
    max_value         DECIMAL(18,2)         DEFAULT NULL,
    unit              VARCHAR(24)           DEFAULT '',
    affects_knowledge TINYINT(1)   NOT NULL DEFAULT 0,           -- ★ 改动需同步 RAG 知识文档
    docs_ref          VARCHAR(255)          DEFAULT '',         -- 对应知识文档（相对 knowledge 目录）
    updated_by        BIGINT                DEFAULT NULL,       -- 最后修改的管理员 user_id
    updated_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------ 管理操作审计
CREATE TABLE IF NOT EXISTS admin_logs (
    id           BIGINT      NOT NULL AUTO_INCREMENT,
    admin_id     BIGINT      NOT NULL,
    action       VARCHAR(64) NOT NULL,            -- config_update / user_lock / user_unlock /
                                                  -- user_role / user_reset_paypass /
                                                  -- product_update / notice_send /
                                                  -- knowledge_sync_confirm / knowledge_rebuild
    target_type  VARCHAR(32) NOT NULL DEFAULT '', -- config / user / product / knowledge / notice
    target_id    VARCHAR(64)          DEFAULT '',
    detail       TEXT                DEFAULT NULL, -- JSON：old→new 等结构化留痕
    pending_sync TINYINT(1)   NOT NULL DEFAULT 0, -- ★ 1=关联知识文档待人工同步
    sync_done_at DATETIME            DEFAULT NULL, -- 待同步确认时间
    created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_admin_logs_action (action),
    KEY idx_admin_logs_target (target_type, target_id),
    KEY idx_admin_logs_pending (pending_sync)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- 用户锁定 / 商品上下架
-- MySQL 8.0 没有 ADD COLUMN IF NOT EXISTS，统一用 information_schema 条件判断。
SET @locked_exists := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'locked'
);
SET @sql := IF(@locked_exists = 0,
    'ALTER TABLE users ADD COLUMN locked TINYINT(1) NOT NULL DEFAULT 0 COMMENT ''管理员锁定登录''',
    'SELECT ''users.locked already exists'' AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @product_status_exists := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'products' AND column_name = 'status'
);
SET @sql := IF(@product_status_exists = 0,
    'ALTER TABLE products ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT ''on'' COMMENT ''on=上架 off=下架''',
    'SELECT ''products.status already exists'' AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ------------------------------------------------- 风控参数种子（rules.py 迁入）
-- value 与 rules.py 默认值一致；后续改动一律走管理端（写 system_config + admin_logs）
INSERT INTO system_config
    (`key`, value, label, value_type, min_value, max_value, unit, affects_knowledge, docs_ref)
VALUES
    ('transfer_verify_threshold',        '2000',   '转账验证阈值（超过需验证码+支付密码）', 'int',    '100',  '50000',  '元', 1, 'transfer-rules.md'),
    ('transfer_big_threshold',           '20000',  '转账大额警示阈值',                     'int',    '1000', '200000', '元', 1, 'transfer-rules.md'),
    ('transfer_big_unfrequent_threshold','5000',   '非常用收款人大额警示阈值',             'int',    '1000', '100000', '元', 1, 'transfer-rules.md'),
    ('wealth_verify_threshold',          '10000',  '理财大额核验阈值（超过需支付密码）',   'int',    '1000', '200000', '元', 1, 'wealth-products.md'),
    ('transfer_daily_limit',             '50000',  '转账单笔 / 单日累计限额',              'int',    '1000', '1000000','元', 1, 'transfer-rules.md'),
    ('paypass_max_attempts',             '3',      '支付密码最大尝试次数（超过锁定）',     'int',    '1',    '10',     '次', 1, 'account-security.md'),
    ('verify_code_ttl_seconds',          '300',    '验证码有效期',                         'int',    '60',   '1800',   '秒', 1, 'account-security.md'),
    ('verify_code_max_attempts',         '3',      '验证码最大尝试次数（超过作废）',       'int',    '1',    '10',     '次', 1, 'account-security.md'),
    ('debit_card_max',                   '3',      '储蓄卡持有上限',                       'int',    '1',    '10',     '张', 0, ''),
    ('credit_card_max',                  '2',      '信用卡持有上限',                       'int',    '1',    '10',     '张', 0, ''),
    ('cards_per_account_max',            '2',      '单账户挂靠卡片上限',                   'int',    '1',    '10',     '张', 0, ''),
    ('card_default_credit_limit',        '5000',   '信用卡默认额度',                       'decimal','500', '50000',  '元', 0, ''),
    ('card_limit_max',                   '50000',  '信用卡调额上限',                       'decimal','1000','500000', '元', 0, ''),
    ('settle_delay_minutes',             '5',      '跨行清算演示时长',                     'int',    '1',    '120',    '分钟', 0, '')
ON DUPLICATE KEY UPDATE label = VALUES(label);
