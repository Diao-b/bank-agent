-- ============================================================================
--  upgrade_subscription_account.sql —— 订阅「指定扣款账户」· 数据层迁移
--
--  问题（2026-10-14 用户反馈：签约新订阅不能像理财那样选付款账户/卡）：
--    此前签约时选的 account_id 只用于补录历史流水，subscriptions 表**没存**；
--    扣款引擎到期扣款按「账户 id 顺序找第一个余额够的」——用户签的时候
--    选没选账户都不影响实际从哪扣，与理财「绝不静默换户」口径不一致。
--
--  本脚本做一件事（仅加列，不动数据）：
--    subscriptions 新增 account_id —— 记录**签约时的扣款账户 id**，
--    扣款引擎优先从该账户扣款；余额不足 → 跳过等待重试，**绝不静默换户**
--    （与 buy_product 的扣款账户口径一致）。
--
--  ★ 存量数据无需回填：历史订阅本就是"任意第一个余额够的账户"扣的，
--    account_id 留 NULL 后走同一条旧兜底，语义与当时一致。
--
--  用法：python init_db.py --upgrade-only
-- ============================================================================

USE bank_agent;

ALTER TABLE subscriptions
    ADD COLUMN account_id BIGINT NULL COMMENT '签约扣款账户 id（扣款引擎优先从该账户扣款；NULL=沿用 id 顺序第一个余额够的非信用账户）';

CREATE INDEX idx_subs_account ON subscriptions (account_id);
