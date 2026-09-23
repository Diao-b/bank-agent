-- ============================================================================
--  upgrade_card_account.sql —— 卡片-账户一体化（申请新卡成为真正可收付的银行卡）
--
--  设计原则（与 utils/card_service.py 注释一致，改动前先读这一段）：
--    ★ 卡片必须挂靠真实账户 —— 卡是账户的入口，不是装饰记录。
--      · 申请储蓄卡 = 开新账户（生成账号 + 卡号）+ 绑卡；
--        或挂靠已有账户（同一账户最多 2 张卡）。
--      · 申请信用卡 = 挂信用账户（每人一个，共享额度；卡有独立卡号）。
--      · 没有账户的卡片不允许存在。
--
--  本文件只负责 DDL（人可直接执行），数据回填 / 幂等 / 遗留卡清理
--  全部由 scripts/seed_card_account.py 完成（原因同账户体系迁移：
--  MySQL 8.0 不支持 ADD COLUMN IF NOT EXISTS，且卡号要算 Luhn 校验位）。
--
--  幂等：重复执行本文件会因列已存在而报 1060，属预期；请以 Python 脚本为准。
-- ============================================================================

USE bank_agent;
SET FOREIGN_KEY_CHECKS = 0;

-- ------------------------------------------------------------------ accounts
-- account_type：debit=储蓄账户（可收付）/ credit=信用账户（共享额度，不参与收付）
--   ★ 信用账户是本次新增的账户形态：余额语义 = 可用额度，不参与转账收付轨道。
-- is_primary：1=主账户（每人最多一个，= 该用户 id 最小的账户）。
--   ★ 主账户是「归集到主账户」的默认目标，也是转账付款的默认扣款账户
--     （与 POST /api/transfer 的 ORDER BY id ASC LIMIT 1 口径一致）。
ALTER TABLE accounts
  ADD COLUMN account_type VARCHAR(16) NOT NULL DEFAULT 'debit'
  COMMENT 'debit=储蓄账户 credit=信用账户（共享额度，不参与收付）' AFTER card_no;
ALTER TABLE accounts
  ADD COLUMN is_primary TINYINT NOT NULL DEFAULT 0
  COMMENT '1=主账户（每人唯一，id 最小的账户）' AFTER account_type;

-- ------------------------------------------------------------------ cards
-- card_no_full：完整卡号（16 位 Luhn 合法）。
--   ★ 与 cards.card_no 的区别：card_no 是展示用掩码（如 6102****0343），
--     card_no_full 是真实卡号，只在「申请成功」时对本人展示一次，列表接口不下发。
--   ★ 卡号反查（转账收款）同时命中 accounts.card_no 与 cards.card_no_full：
--     前者是「账户编号」，后者是「账户下挂靠的卡」，两者都能定位到具体账户。
ALTER TABLE cards
  ADD COLUMN card_no_full VARCHAR(19) NULL
  COMMENT '完整卡号（Luhn 合法）；仅申请人可见一次，列表仅返回尾号' AFTER card_no;

-- ------------------------------------------------------------------ payees
-- account_id：该收款人（账户）在站内对应的具体账户。
--   ★ 一个用户可以有多个账户、多张卡，收款人必须记住「转到哪个账户」，
--     而不是像以前那样一律入账到对方 id 最小的账户。
ALTER TABLE payees
  ADD COLUMN account_id BIGINT NULL
  COMMENT '站内收款对应的目标账户（跨行收款人为 NULL）' AFTER linked_user_id;

SET FOREIGN_KEY_CHECKS = 1;

-- ------------------------------------------------------------------ 回填（人工执行版）
-- 每人 id 最小的账户标记为主账户
UPDATE accounts a
  JOIN (SELECT user_id, MIN(id) AS mid FROM accounts GROUP BY user_id) t
    ON t.mid = a.id
  SET a.is_primary = 1
  WHERE a.is_primary = 0;

-- 存量卡的完整卡号：从挂靠账户的 card_no 回填（卡与账户同号）
UPDATE cards c
  JOIN accounts a ON a.id = c.account_id
  SET c.card_no_full = a.card_no
  WHERE c.card_no_full IS NULL AND a.card_no IS NOT NULL;

-- ------------------------------------------------------------------ 遗留卡清理
-- ★ 旧模型的卡没有 account_id、card_no 是伪造掩码（如 '6225****1234'，不是合法卡号），
--   既不能收款也不能付款 —— 按新原则「没有账户的卡片不允许存在」，迁移时注销。
--   由 scripts/seed_card_account.py 用软删除（status='deleted'）执行，可回溯。
--   示例（脚本里带报表输出，勿手工乱跑）：
--   UPDATE cards SET status='deleted', deleted_at=NOW()
--     WHERE account_id IS NULL AND status <> 'deleted';
