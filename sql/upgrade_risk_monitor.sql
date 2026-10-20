-- =====================================================================
-- upgrade_risk_monitor.sql —— 支付风控监控（模块 A）
--
-- 设计要点（详见 docs/risk_monitor_design.md）：
--   1) 双通道：通道 A 用户侧实时提醒（提交前确认卡，保护用户本人）；
--      通道 B 后台告警队列（分级 + 最小披露）。
--   2) 规则引擎可配置：risk_rules 表（threshold 用 JSON，severity / enabled
--      由后台「风控参数」Tab 调整，沿用 pending_sync 机制）。
--   3) 隐私铁律：风控只读取元数据（收款人 / 金额 / 时间 / 频率），
--      ★ 严禁读取 chat_messages、transactions.remark 用于判定。
--   4) 最小披露：告警列表默认脱敏（用户ID脱敏 / 收款人张* / 金额分桶）；
--      展开完整明细需填理由并写 risk_event_views 审计。
--   5) 关联修复（D1）：transactions 增可空 payee_id，使 R-DUP / R-NEWLARGE /
--      R-SCATTER 能用结构化收款人身份稳定匹配，而非易碎的 merchant 串。
--   6) 排除在途/退票（D2）：risk 查询统一过滤 settle_status IS NULL。
--
-- 本脚本可重复执行（CREATE TABLE IF NOT EXISTS + information_schema 条件 ALTER
-- + INSERT IGNORE），由 init_db.py 顺序应用，不改余额、不碰 init.sql。
-- =====================================================================

USE bank_agent;

-- -------------------------------------------------------- 规则配置表
CREATE TABLE IF NOT EXISTS risk_rules (
    rule_code   VARCHAR(32)  NOT NULL COMMENT 'R-DUP / R-FREQ / R-NEWLARGE / R-AMTOUT / R-SCATTER / R-DRAIN / R-NIGHT / R-CHARGE',
    name        VARCHAR(64)  NOT NULL,
    enabled     TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '0=关闭',
    threshold   JSON         NULL     COMMENT '各规则阈值（JSON，如 {"window_min":30,"amount_same":1}）',
    severity    VARCHAR(16)  NOT NULL DEFAULT 'medium' COMMENT 'low / medium / high / critical',
    user_facing TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '1=触发用户侧确认卡（通道 A）',
    description  VARCHAR(255) NOT NULL DEFAULT '',
    PRIMARY KEY (rule_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='支付风控规则（可配置，不硬编码）';

-- 默认规则种子（INSERT IGNORE：重复执行安全；threshold 为演示选值，可后台调整）
INSERT IGNORE INTO risk_rules (rule_code, name, enabled, threshold, severity, user_facing, description) VALUES
('R-DUP',      '同收款人重复同额转账', 1, '{"window_min":30,"amount_same":1}',                          'high',    1, '同一付款账户→同一收款人，金额完全相同，窗口内≥2笔且未退票（诈骗"再转一次"典型）'),
('R-FREQ',     '高频转账',             1, '{"window_min":5,"count":3}',                                 'high',    1, 'X 分钟内≥N 笔转出'),
('R-NEWLARGE', '新收款人大额',         1, '{"amount":3000}',                                           'medium',  0, '首次向该收款人转账且金额>阈值（用 payees.is_frequent 判定）'),
('R-AMTOUT',   '金额突变',             1, '{"k":3,"window_days":30}',                                   'medium',  0, '单笔>近30天单笔均值×K（排除本笔）'),
('R-SCATTER',  '分散转出',             1, '{"window_min":10,"distinct_payees":3}',                       'medium',  0, '短时间内向≥N个不同收款人转出'),
('R-DRAIN',    '余额清空',             1, '{"min_balance":100}',                                        'medium',  0, '转出后余额接近0'),
('R-NIGHT',    '深夜大额',             1, '{"start_hour":0,"end_hour":6,"amount":2000}',                'low',     0, '00:00–06:00 且金额>阈值'),
('R-CHARGE',   '异常充值',             1, '{"window_min":60,"count":3,"daily_amount":20000}',            'high',    1, '同一用户短时间核销多张卡密或单日充值额超阈值（与模块B联动）');

-- -------------------------------------------------------- 风险事件表
CREATE TABLE IF NOT EXISTS risk_events (
    id              BIGINT      NOT NULL AUTO_INCREMENT,
    user_id         BIGINT      NOT NULL,
    rule_code       VARCHAR(32) NOT NULL,
    severity        VARCHAR(16) NOT NULL DEFAULT 'medium',
    status          VARCHAR(16) NOT NULL DEFAULT 'open' COMMENT 'open / confirmed / dismissed / closed',
    hit_at          DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload         JSON        NULL     COMMENT '最小字段：amount_bucket / payee_masked / window / count 等',
    linked_txn_ids  JSON        NULL     COMMENT '命中的 transactions.id 列表',
    handled_by      BIGINT      NULL,
    handled_at      DATETIME    NULL,
    note            VARCHAR(255) NULL,
    PRIMARY KEY (id),
    KEY idx_risk_status_sev (status, severity, hit_at),
    KEY idx_risk_user (user_id, hit_at),
    KEY idx_risk_rule (rule_code, hit_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='支付风控事件（通道B）';

-- -------------------------------------------------------- 明细查看审计表
CREATE TABLE IF NOT EXISTS risk_event_views (
    id               BIGINT      NOT NULL AUTO_INCREMENT,
    event_id         BIGINT      NOT NULL,
    admin_id         BIGINT      NOT NULL,
    viewed_at        DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reason           VARCHAR(255) NOT NULL COMMENT '展开完整明细的理由（强制）',
    fields_disclosed VARCHAR(255) NULL     COMMENT '本次披露的字段清单',
    PRIMARY KEY (id),
    KEY idx_risk_view_event (event_id),
    KEY idx_risk_view_admin (admin_id, viewed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='风控明细查看审计（who/what/when/why）';

-- -------------------------------------------------------- D1：transactions 增 payee_id
-- MySQL 8 无 ADD COLUMN IF NOT EXISTS，用 information_schema 条件判断（与 upgrade_admin_ops.sql 同款写法）
SET @pe_exists := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'transactions' AND column_name = 'payee_id'
);
SET @sql := IF(@pe_exists = 0,
    'ALTER TABLE transactions ADD COLUMN payee_id BIGINT NULL COMMENT ''收款人ID（风控结构化匹配，D1修复；空=非转账/未解析）''',
    'SELECT ''transactions.payee_id already exists'' AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- payee_id 索引（R-DUP/R-NEWLARGE/R-SCATTER 查询加速）：同样条件判断避免重复建
SET @pei_exists := (
    SELECT COUNT(*) FROM information_schema.statistics
    WHERE table_schema = DATABASE() AND table_name = 'transactions' AND index_name = 'idx_txn_payee'
);
SET @sql := IF(@pei_exists = 0,
    'ALTER TABLE transactions ADD INDEX idx_txn_payee (payee_id, account_id, created_at)',
    'SELECT ''idx_txn_payee already exists'' AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- payee_id 外键（SET NULL，与 payees 其他外键一致；列初始全 NULL，不会破坏现有数据）
SET @pefk_exists := (
    SELECT COUNT(*) FROM information_schema.table_constraints
    WHERE table_schema = DATABASE() AND table_name = 'transactions'
      AND constraint_name = 'fk_txn_payee'
);
SET @sql := IF(@pefk_exists = 0,
    'ALTER TABLE transactions ADD CONSTRAINT fk_txn_payee '
    'FOREIGN KEY (payee_id) REFERENCES payees(id) ON DELETE SET NULL',
    'SELECT ''fk_txn_payee already exists'' AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
