-- =====================================================================
-- upgrade_card_key.sql —— 卡密充值（模块 B，资金入口）
--
-- 设计要点（详见 docs/card_key_design.md）：
--   1) 归属分离：管理员只生成「卡密（预付凭证）」，零余额操作；
--      入账由用户主动提交卡密触发，走独立资金入口，携带 card_keys.id 作为凭证来源。
--      ★ 任何接口都不允许管理员直接改用户余额 —— 铁律完好（见 admin.py 白名单）。
--   2) 安全底线（逐条落地）：
--      a) 绝不存明文：库里只存 code_hash = SHA-256(pepper + code)；明文仅生成时一次性返回。
--      b) 不可枚举：secrets 生成 16 位 base32（≈80bit 熵），禁自增/时间戳/短码。
--      c) 原子核销：UPDATE ... WHERE code_hash=? AND status='unused' → rowcount==1 判定。
--      d) 幂等：复用 idempotency_records（客户端带 Idempotency-Key）。
--      e) 面额服务端权威：只取 card_keys.amount，忽略一切客户端金额。
--      f) 面额生成即冻结：amount 一经生成不可改；改只能作废+重发，两次都留痕。
--      g) 一次性+可作废：支持后台作废未使用卡密；已使用不可作废。
--      h) 账实一致：入账同事务写 transactions(type='income',category='充值',merchant='卡密充值')
--         与 notifications 收件箱通知。
--      i) 审计：生成/作废/导出/核销全写 admin_logs（含 old→new）；用户侧可查自己核销记录。
--      j) 限流防爆破：核销按 user+IP 限频；连续失败达阈值触发锁定+联动模块A。
--      k) 上限：单卡面额上限/单笔/单日充值上限写入 system_config 可配。
--
-- 本脚本可重复执行（CREATE TABLE IF NOT EXISTS），由 init_db.py 顺序应用。
-- 卡密面额上限等开关走 rules.RULE_SPECS（system_config），不在此建表。
-- =====================================================================

USE bank_agent;

-- -------------------------------------------------------- 卡密表
CREATE TABLE IF NOT EXISTS card_keys (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    code_hash       CHAR(64)     NOT NULL COMMENT 'SHA-256(pepper + code)，绝不存明文',
    code_prefix     VARCHAR(8)   NOT NULL DEFAULT '' COMMENT '前4位明文，仅供人工核对',
    amount          DECIMAL(14,2) NOT NULL COMMENT '面额（生成即冻结，不可修改）',
    batch_no        VARCHAR(32)  NOT NULL,
    status          ENUM('unused','used','void','expired') NOT NULL DEFAULT 'unused',
    created_by      BIGINT       NULL,
    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at      DATETIME     NULL,
    used_by         BIGINT       NULL,
    used_at         DATETIME     NULL,
    used_account_id BIGINT       NULL,
    remark          VARCHAR(255) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_code_hash (code_hash),
    KEY idx_ck_batch (batch_no),
    KEY idx_ck_status (status, created_at),
    KEY idx_ck_used (used_by, used_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='卡密（预付凭证池；管理员生成，用户核销）';

-- -------------------------------------------------------- 批次表
CREATE TABLE IF NOT EXISTS card_key_batches (
    batch_no    VARCHAR(32)  NOT NULL,
    amount      DECIMAL(14,2) NOT NULL,
    count       INT          NOT NULL,
    created_by  BIGINT       NULL,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    remark      VARCHAR(255) NULL,
    PRIMARY KEY (batch_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='卡密批次（一次生成 N 张同额卡）';

-- -------------------------------------------------------- 核销限流计数（防爆破）
CREATE TABLE IF NOT EXISTS card_redeem_throttle (
    user_id    BIGINT       NOT NULL,
    ip         VARCHAR(64)  NOT NULL,
    bucket_min DATETIME     NOT NULL COMMENT '按分钟分桶',
    cnt        INT          NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, ip, bucket_min)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='卡密核销限流计数（user+IP+分钟）';
