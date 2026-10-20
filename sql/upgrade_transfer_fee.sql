-- ============================================================================
--  upgrade_transfer_fee.sql —— 跨行转账手续费 · 数据层支持（幂等）
--
--  配套代码：utils/transfer.calc_transfer_fee（唯一计算入口）
--            utils/transfer.execute_transfer（扣款 + 手续费独立流水）
--            utils/settlement_engine._settle_one（退票时手续费随本金退回）
--            mock_bank.main.transfer_fee_quote（页面/对话报价，只读）
--            agent/write_flow._transfer_fee（确认卡与余额预检）
--
--  口径（与 docs/knowledge/transfer-rules.md 一致，该文档此前已写明规则、代码未实现）：
--   - 只对**跨行**转账收取；本行站内转账免费（rate 置 0 即全局免收）。
--   - 手续费 = 金额 × 费率，夹紧到 [最低, 最高]，四舍五入到分。
--     默认 0.1% / 最低 ¥1 / 最高 ¥50（system_config，管理端可调）。
--   - 手续费与本金分开记流水（category='手续费'），不计入转账限额累计。
--   - **跨行退票时手续费随本金一并原路退回**（文档「退票全额原路退回，不收费」），
--     因此 pending_settlements 必须记住本笔收取的手续费。
--
--  本脚本可重复执行（幂等加列）。
-- ============================================================================

USE bank_agent;
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- 1) 在途清算台账记住手续费：退票时要连手续费一起退回
SET @sql_col = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 'pending_settlements'
       AND column_name = 'fee') = 0,
    "ALTER TABLE pending_settlements ADD COLUMN fee DECIMAL(12,2) NOT NULL DEFAULT 0.00 "
    "COMMENT '本笔已收跨行手续费（退票时随本金一并退回）'",
    'SELECT 1'
);
PREPARE stmt_col FROM @sql_col;
EXECUTE stmt_col;
DEALLOCATE PREPARE stmt_col;

SET FOREIGN_KEY_CHECKS = 1;

-- --------------------------------------------------------------------------- --
--  校验：列已存在 + 存量行默认 0（存量在途单未收过手续费，退回 0 即等价旧行为）
-- --------------------------------------------------------------------------- --
SELECT '--- pending_settlements.fee ---' AS check_item;
SELECT COUNT(*) AS rows_with_fee_column FROM information_schema.columns
 WHERE table_schema = DATABASE() AND table_name = 'pending_settlements' AND column_name = 'fee';
SELECT COUNT(*) AS rows_pending FROM pending_settlements WHERE status = 'pending';
