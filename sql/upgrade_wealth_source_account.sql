-- ============================================================================
--  upgrade_wealth_source_account.sql —— 理财「赎回回款到原扣款账户」· 数据层迁移
--
--  问题（2026-10-11 用户反馈："赎回的金额不应该返回买的这个账号吗，这个不能乱"）：
--    此前 buy_product / redeem 都硬编码「用户名下 id 最小的非信用账户」——
--    用「零钱罐」申购，赎回的钱却回到「微众卡」，款项去向与来源不一致、无法追溯。
--
--  本脚本做一件事（仅加列，不动数据）：
--    holdings 新增 source_account_id —— 记录**申购时的扣款账户 id**，
--    赎回时优先回款到该账户（为空则沿用旧兜底：id 最小的非信用账户）。
--
--  ★ 存量数据无需回填：旧代码只能从"id 最小的非信用账户"扣款，
--    因此历史持仓的 source_account_id 留 NULL 后走同一条兜底，语义与当时一致。
--
--  用法：python init_db.py --upgrade-only
-- ============================================================================

USE bank_agent;

ALTER TABLE holdings
    ADD COLUMN source_account_id BIGINT NULL COMMENT '申购时的扣款账户 id（赎回回款到此账户；NULL=沿用 id 最小的非信用账户）';

CREATE INDEX idx_holdings_source_acc ON holdings (source_account_id);
