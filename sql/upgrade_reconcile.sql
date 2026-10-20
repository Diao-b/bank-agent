-- =====================================================================
-- upgrade_reconcile.sql —— 账实一致性对账基线（scripts/reconcile_balance.py 用）
--
-- 不变量：对每个账户，balance 的变动必须等于其流水的净额变动
--         （income 计正、expense 计负；pending 的跨行支出已扣款且已有流水，故也计入）。
-- 本表保存「上一次对账时的基线快照」：余额 + 净流水。增量对账：
--   Δbalance 应 == Δnet_txn；不等即说明发生了「绕过流水的余额改动」（如手工 UPDATE）。
--
-- 纯新增表，可重复执行；不改任何余额、不碰 init.sql。
-- =====================================================================

USE bank_agent;

CREATE TABLE IF NOT EXISTS balance_reconcile (
    account_id   BIGINT       NOT NULL,
    user_id      BIGINT       NOT NULL,
    snap_balance DECIMAL(14,2) NOT NULL DEFAULT 0.00 COMMENT '上次对账时余额',
    snap_income  DECIMAL(14,2) NOT NULL DEFAULT 0.00 COMMENT '上次对账时累计收入',
    snap_expense DECIMAL(14,2) NOT NULL DEFAULT 0.00 COMMENT '上次对账时累计支出',
    updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id),
    KEY idx_recon_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='账实对账基线快照（增量对账）';
