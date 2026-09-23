-- ============================================================================
--  upgrade_transfer_limit.sql —— 转账单笔 / 单日累计限额 · 数据层支持（幂等）
--
--  配套代码：utils/transfer.check_transfer_limit / apply_transfer_limit
--            mock_bank.main.transfer_limit（只读查询）
--            agent/write_flow._precheck_transfer_limit（对话流前置提示）
--
--  设计要点（口径与 docs/knowledge/transfer-rules.md 一致）：
--   - 单笔限额：单笔转账金额上限；
--   - 当日累计：本自然日「已成功转出」的金额上限；
--       已落账的站内转账（实时到账）与跨行转账（扣款即在途）均计入；
--       退票（settle_status='returned'，资金原路退回）当日返还额度。
--   - 本人账户间归集（remark='内部划转'）不计入对外转出限额。
--   - 并发安全：以「(user_id, 当日)」这一行做 SELECT ... FOR UPDATE 当前读 +
--     计数器累加，后到者被行锁阻塞到前一笔提交后再读，不会双双读到 0 而同时放行。
--
--  本表仅作限额串行锁锚点 + 计数器；限额数值来自 rules.transfer_daily_limit
--  （system_config，默认 50000）。本脚本可重复执行（CREATE TABLE IF NOT EXISTS）。
-- ============================================================================

USE bank_agent;
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- --------------------------------------------------------------------------- --
--  转账限额锚点表：按 (用户, 自然日) 一行
--    used_amount：当日已成功转出金额（退票由清算引擎回补递减）
--    ★ 该表不挂外键到 users：限额只是"当日计数器"，删用户时由清理脚本顺手清。
-- --------------------------------------------------------------------------- //
CREATE TABLE IF NOT EXISTS transfer_daily_quota (
  id          BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id     BIGINT       NOT NULL,
  quota_date  DATE         NOT NULL,
  used_amount DECIMAL(18,2) NOT NULL DEFAULT 0.00,
  UNIQUE KEY uq_user_day (user_id, quota_date),
  KEY idx_date (quota_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='转账限额·按日串行锁锚点 + 当日已用计数器';
