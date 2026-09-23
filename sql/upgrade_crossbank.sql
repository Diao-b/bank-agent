-- ============================================================================
--  upgrade_crossbank.sql —— 跨行转账「在途 · 清算 · 退票」数据层迁移（幂等）
--
--  一、设计原则（真实银行业务约束）
--  ────────────────────────────────────────────────────────────────────────
--  跨行转账本行无法实时校验对方账户是否存在（对方行的账，本行看不到）。
--  因此真实做法是：资金先扣款进入「在途（pending）」状态，交由清算网络处理，
--  清算结果只有两种 —— 到账（settled）或退票全额退回（returned）。
--
--      ★ 账实始终一致：在途资金已离开可用余额，退票时原路全额退回，
--        任何情况下都不会出现「钱扣了、既没到账也没退回」的悬空资金。
--
--  演示环境没有真实清算网络，故：
--    1) 清算用「惰性触发」（复用订阅扣款引擎模式）—— 访问任意数据接口时
--       顺带把到期的在途清算处理掉，不需要常驻定时器；
--    2) external_accounts 表模拟「他行可清算账户库」：
--         清算时查这张表 —— 在库 = 对方账户存在 → 清算成功（到账）；
--                             不在库 = 对方行退票 → 全额退回。
--       ★ 登记时机仅在「新增收款人」（用户提供账户经基础校验，演示口径视为
--         可清算；生产由收款行核验）。绝不在转账成功时登记 —— 否则幽灵卡号
--         第一次转账就会「成功」，退票闭环形同虚设。
--
--  二、本脚本做四件事（全部幂等，可重复执行）
--  ────────────────────────────────────────────────────────────────────────
--    1) transactions 新增 settle_status（NULL=无需清算；pending/settled/returned）
--    2) 新表 pending_settlements（在途清算台账）
--    3) 新表 external_accounts（他行可清算账户库，演示用）
--    4) 把现有「已核验收款人」（收款人簿内的跨行收款人）逐条登记进
--       external_accounts —— 与 payees 现有卡号一致
--
--  说明：MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS（MariaDB 语法），
--        故列/表的存在性判断统一走 information_schema（见 init_db 约定）。
-- ============================================================================

USE bank_agent;
SET NAMES utf8mb4;

-- --------------------------------------------------------------------------- #
--  1) transactions.settle_status
--     NULL      = 无需清算（本行即时到账：站内转账 / 内部归集 / 订阅扣款…）
--     'pending' = 在途（跨行已扣款，等待清算）
--     'settled' = 已清算到账
--     'returned'= 已退票退回
-- --------------------------------------------------------------------------- #
SET @sql_settle = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 'transactions'
       AND column_name = 'settle_status') = 0,
    "ALTER TABLE transactions ADD COLUMN settle_status VARCHAR(20) NULL "
    "COMMENT '跨行清算状态：NULL=无需清算 pending=在途 settled=已到账 returned=已退票' "
    "AFTER remark",
    'SELECT 1'
);
PREPARE stmt_settle FROM @sql_settle;
EXECUTE stmt_settle;
DEALLOCATE PREPARE stmt_settle;

-- 清算状态查询索引（账单页按状态筛 / 清算引擎按状态扫）
SET @sql_settle_idx = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema = DATABASE() AND table_name = 'transactions'
       AND index_name = 'idx_txn_settle') = 0,
    "ALTER TABLE transactions ADD INDEX idx_txn_settle (settle_status)",
    'SELECT 1'
);
PREPARE stmt_settle_idx FROM @sql_settle_idx;
EXECUTE stmt_settle_idx;
DEALLOCATE PREPARE stmt_settle_idx;

-- --------------------------------------------------------------------------- #
--  2) pending_settlements —— 在途清算台账
--     txn_id         关联原转账支出流水 transactions.id（★因果链：退票/到账
--                    必须能追回原流水，否则对账时找不到"这笔退的是哪笔"）
--     expected_at    预计清算完成时间 —— 生产为「创建 + 2 小时」，
--                    演示压缩为「创建 + 5 分钟」（见 settlement_engine）
--     serial_no      原转账流水号（到账/退票通知与退款流水备注引用）
--     status         pending=在途  done=已处理（到账或退票，二者由原流水
--                    settle_status 区分：settled / returned）
-- --------------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS pending_settlements (
    id             BIGINT        NOT NULL AUTO_INCREMENT,
    txn_id         BIGINT        NOT NULL COMMENT '原转账支出流水 transactions.id',
    user_id        BIGINT        NOT NULL COMMENT '付款方用户 ID',
    amount         DECIMAL(14,2) NOT NULL COMMENT '在途金额',
    target_card_no VARCHAR(24)   NULL     COMMENT '收款方他行卡号',
    target_bank_id INT           NULL     COMMENT '收款方开户行 bank_directory.id',
    serial_no      VARCHAR(40)   NULL     COMMENT '原转账流水号',
    expected_at    DATETIME      NOT NULL COMMENT '预计清算完成时间（创建+5分钟，演示压缩）',
    status         VARCHAR(16)   NOT NULL DEFAULT 'pending' COMMENT 'pending=在途 done=已处理',
    created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_ps_due (status, expected_at),
    KEY idx_ps_user (user_id, status),
    KEY idx_ps_card (target_card_no),
    KEY idx_ps_txn (txn_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='跨行转账在途清算台账';

-- --------------------------------------------------------------------------- #
--  3) external_accounts —— 他行可清算账户库（演示用）
--     ★ 清算引擎的唯一判据：在库 = 对方账户存在 → 到账；不在库 → 退票。
--     真实生产由收款行核验账户，本表仅为演示模拟。
-- --------------------------------------------------------------------------- #
CREATE TABLE IF NOT EXISTS external_accounts (
    id         BIGINT      NOT NULL AUTO_INCREMENT,
    card_no    VARCHAR(24) NOT NULL COMMENT '他行卡号（Luhn 合法）',
    real_name  VARCHAR(64) NULL     COMMENT '户名',
    bank_id    INT         NULL     COMMENT '开户行 bank_directory.id',
    created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_ext_card (card_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='他行可清算账户库（演示：登记=账存在）';

-- --------------------------------------------------------------------------- #
--  4) 种子：把现有「已核验收款人」（收款人簿内的跨行收款人，linked_user_id 为空）
--     逐条登记进 external_accounts —— 与 payees 现有卡号一致。
--     ★ 站内收款人（linked_user_id 非空 / 本行微众卡）不进本表：它们在
--       accounts 里，走实时到账，不涉及跨行清算。
-- --------------------------------------------------------------------------- #
INSERT INTO external_accounts (card_no, real_name, bank_id)
SELECT p.card_no, p.real_name, p.bank_id
FROM payees p
WHERE p.linked_user_id IS NULL
  AND p.card_no IS NOT NULL AND p.card_no <> ''
  AND NOT EXISTS (SELECT 1 FROM external_accounts e WHERE e.card_no = p.card_no);

-- --------------------------------------------------------------------------- #
--  自检
-- --------------------------------------------------------------------------- #
SELECT '--- transactions.settle_status ---' AS check_item;
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM information_schema.columns
WHERE table_schema = DATABASE() AND table_name = 'transactions'
  AND column_name = 'settle_status';

SELECT '--- pending_settlements ---' AS check_item;
SELECT COUNT(*) AS rows_now FROM pending_settlements;

SELECT '--- external_accounts（他行可清算账户库）---' AS check_item;
SELECT e.id, e.card_no, e.real_name, b.bank_name
FROM external_accounts e
LEFT JOIN bank_directory b ON b.id = e.bank_id
ORDER BY e.id;
