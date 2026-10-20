-- =====================================================================
-- upgrade_column_comments.sql —— 为模块 A/B 新增表补齐字段中文备注（COMMENT）
--
-- 目的：看数据库设计时，字段含义一目了然（Navicat / Workbench / DBeaver 直接显示）。
-- 说明：MySQL 加字段备注要用 MODIFY COLUMN 重述完整定义（类型/是否为空/默认值/自增），
--      本脚本严格按现有定义重述，只追加 COMMENT，不改类型、不改默认值、不动索引。
-- 可重复执行（幂等：只是反复设置同一 COMMENT）。
-- =====================================================================

USE bank_agent;

-- -------------------------------------------------- 风控规则（模块 A）
ALTER TABLE risk_rules
  MODIFY COLUMN `rule_code`   VARCHAR(32)  NOT NULL                COMMENT '规则编码（R-DUP/R-FREQ/R-NEWLARGE/R-AMTOUT/R-SCATTER/R-DRAIN/R-NIGHT/R-CHARGE）',
  MODIFY COLUMN `name`        VARCHAR(64)  NOT NULL                COMMENT '规则名称（中文）',
  MODIFY COLUMN `enabled`     TINYINT(1)   NOT NULL DEFAULT 1      COMMENT '是否启用：1=启用，0=停用（停用后不再产生新事件）',
  MODIFY COLUMN `threshold`   JSON             NULL                COMMENT '规则阈值（JSON），如 {"window_min":30,"amount_same":1}',
  MODIFY COLUMN `severity`    VARCHAR(16)  NOT NULL DEFAULT 'medium' COMMENT '告警级别：low/medium/high/critical',
  MODIFY COLUMN `user_facing` TINYINT(1)   NOT NULL DEFAULT 0      COMMENT '是否走通道A：1=命中时实时提醒用户本人，0=仅后台告警',
  MODIFY COLUMN `description` VARCHAR(255) NOT NULL DEFAULT ''     COMMENT '规则说明（触发条件的人类可读描述）';

-- -------------------------------------------------- 风控告警事件（模块 A）
ALTER TABLE risk_events
  MODIFY COLUMN `id`             BIGINT       NOT NULL AUTO_INCREMENT COMMENT '事件ID（主键）',
  MODIFY COLUMN `user_id`        BIGINT       NOT NULL                COMMENT '涉及用户ID（users.id）',
  MODIFY COLUMN `rule_code`      VARCHAR(32)  NOT NULL                COMMENT '命中规则编码（risk_rules.rule_code）',
  MODIFY COLUMN `severity`       VARCHAR(16)  NOT NULL DEFAULT 'medium' COMMENT '告警级别：low/medium/high/critical',
  MODIFY COLUMN `status`         VARCHAR(16)  NOT NULL DEFAULT 'open' COMMENT '处置状态：open=待处理/confirmed=已确认/dismissed=已忽略/closed=已关闭',
  MODIFY COLUMN `hit_at`         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '命中时间',
  MODIFY COLUMN `payload`        JSON             NULL                COMMENT '最小字段载荷(JSON)：金额分桶/收款人掩码/窗口/计数等；★不含敏感原文',
  MODIFY COLUMN `linked_txn_ids` JSON             NULL                COMMENT '命中的流水ID列表（JSON数组，transactions.id）',
  MODIFY COLUMN `handled_by`     BIGINT           NULL                COMMENT '处置人（users.id，管理员）',
  MODIFY COLUMN `handled_at`     DATETIME         NULL                COMMENT '处置时间',
  MODIFY COLUMN `note`           VARCHAR(255)     NULL                COMMENT '处置备注';

-- -------------------------------------------------- 告警明细查看审计（模块 A）
ALTER TABLE risk_event_views
  MODIFY COLUMN `id`               BIGINT       NOT NULL AUTO_INCREMENT COMMENT '查看记录ID（主键）',
  MODIFY COLUMN `event_id`         BIGINT       NOT NULL                COMMENT '被查看的告警事件ID（risk_events.id）',
  MODIFY COLUMN `admin_id`         BIGINT       NOT NULL                COMMENT '查看人（users.id，管理员）',
  MODIFY COLUMN `viewed_at`        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '查看时间',
  MODIFY COLUMN `reason`           VARCHAR(255) NOT NULL                COMMENT '展开完整明细的理由（强制填写，留痕）',
  MODIFY COLUMN `fields_disclosed` VARCHAR(255)     NULL                COMMENT '本次披露的字段清单';

-- -------------------------------------------------- 卡密（模块 B）
ALTER TABLE card_keys
  MODIFY COLUMN `id`              BIGINT        NOT NULL AUTO_INCREMENT COMMENT '卡密ID（主键）',
  MODIFY COLUMN `code_hash`       CHAR(64)      NOT NULL                COMMENT '卡密码哈希 SHA-256(pepper+code)；★绝不存明文',
  MODIFY COLUMN `code_prefix`     VARCHAR(8)    NOT NULL DEFAULT ''     COMMENT '卡密明文前4位（仅供人工核对，非明文全量）',
  MODIFY COLUMN `amount`          DECIMAL(14,2) NOT NULL                COMMENT '面额（元）；生成即冻结，不可修改',
  MODIFY COLUMN `batch_no`        VARCHAR(32)   NOT NULL                COMMENT '所属批次号（card_key_batches.batch_no）',
  MODIFY COLUMN `status`          ENUM('unused','used','void','expired') NOT NULL DEFAULT 'unused' COMMENT '状态：unused=未使用/used=已使用/void=已作废/expired=已过期',
  MODIFY COLUMN `created_by`      BIGINT            NULL                COMMENT '生成人（users.id，管理员）',
  MODIFY COLUMN `created_at`      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '生成时间',
  MODIFY COLUMN `expires_at`      DATETIME          NULL                COMMENT '过期时间（NULL=永不过期）',
  MODIFY COLUMN `used_by`         BIGINT            NULL                COMMENT '核销人（users.id，用户本人）',
  MODIFY COLUMN `used_at`         DATETIME          NULL                COMMENT '核销时间',
  MODIFY COLUMN `used_account_id` BIGINT            NULL                COMMENT '入账账户（accounts.id）',
  MODIFY COLUMN `remark`          VARCHAR(255)      NULL                COMMENT '备注';

-- -------------------------------------------------- 卡密批次（模块 B）
ALTER TABLE card_key_batches
  MODIFY COLUMN `batch_no`   VARCHAR(32)   NOT NULL                COMMENT '批次号（主键）',
  MODIFY COLUMN `amount`     DECIMAL(14,2) NOT NULL                COMMENT '本批次统一面额（元）',
  MODIFY COLUMN `count`      INT           NOT NULL                COMMENT '本批次卡密数量',
  MODIFY COLUMN `created_by` BIGINT            NULL                COMMENT '生成人（users.id，super_admin）',
  MODIFY COLUMN `created_at` DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '生成时间',
  MODIFY COLUMN `remark`     VARCHAR(255)      NULL                COMMENT '批次备注';

-- -------------------------------------------------- 核销限流计数（模块 B）
ALTER TABLE card_redeem_throttle
  MODIFY COLUMN `user_id`    BIGINT      NOT NULL           COMMENT '用户ID（限流维度之一）',
  MODIFY COLUMN `ip`         VARCHAR(64) NOT NULL           COMMENT '客户端IP（限流维度之一）',
  MODIFY COLUMN `bucket_min` DATETIME    NOT NULL           COMMENT '分钟时间桶（按分钟聚合计数）',
  MODIFY COLUMN `cnt`        INT         NOT NULL DEFAULT 1 COMMENT '本分钟（用户+IP）内的核销尝试次数';

-- -------------------------------------------------- 账实对账基线（对账脚本）
ALTER TABLE balance_reconcile
  MODIFY COLUMN `account_id`   BIGINT        NOT NULL           COMMENT '账户ID（主键，accounts.id）',
  MODIFY COLUMN `user_id`      BIGINT        NOT NULL           COMMENT '账户所属用户ID',
  MODIFY COLUMN `snap_balance` DECIMAL(14,2) NOT NULL DEFAULT 0.00 COMMENT '上次对账时的账户余额快照',
  MODIFY COLUMN `snap_income`  DECIMAL(14,2) NOT NULL DEFAULT 0.00 COMMENT '上次对账时的累计收入流水',
  MODIFY COLUMN `snap_expense` DECIMAL(14,2) NOT NULL DEFAULT 0.00 COMMENT '上次对账时的累计支出流水',
  MODIFY COLUMN `updated_at`   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '快照更新时间';
