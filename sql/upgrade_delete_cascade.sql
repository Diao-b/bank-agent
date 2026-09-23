-- ============================================================================
--  upgrade_delete_cascade.sql —— 用户数据归属治理：补 ON DELETE CASCADE 外键
--
--  为什么需要
--  --------
--  本库历史上**没有任何外键**，`DELETE FROM users WHERE ...` 只删账号行，
--  accounts / cards / payees / subscriptions / transactions / conversations /
--  chat_messages 全部留在库里变成孤儿数据。危害不只是垃圾数据：
--  `utils/user_provision.provision_new_user()` 的幂等闸门是
--  「该 user_id 已有 accounts 记录就跳过初始化」——所以一旦 MySQL 自增值被复用，
--  新注册用户会**静默继承死账号的全部数据**（余额、卡、收款人、流水、订阅）。
--
--  本脚本的约定
--  ------------
--  1) 「业务数据」随用户物理删除：账户 / 卡 / 收款人 / 持仓 / 订阅 / 在途结算 /
--     通知 / 操作日志 / 验证码 / 会话 / 消息 —— 全部 CASCADE。
--  2) 「审计留痕」不动：admin_logs 没有 user_id 列，天然不受影响；
--     正式环境若要保留资金流水，应改用 users.deleted_at 软删，而不是删用户行。
--  3) transactions 没有 user_id 列，靠 `account_id → accounts.id` 两级级联。
--  4) payees.account_id / payees.linked_user_id 是**指向别人的行**（站内收款人），
--     用 SET NULL 而不是 CASCADE —— 别人销户不该把「我的收款人」一起删掉。
--
--  ★ 执行前请确认库里已无孤儿数据（否则 ALTER 会报 1452 而失败）：
--      ./.venv/Scripts/python.exe scripts/purge_orphan_data.py --dry-run
--    本脚本刻意**不关闭 foreign_key_checks**，让残留孤儿当场暴露而不是被静默放过。
--
--  幂等：所有 ALTER 都先查 information_schema，可重复执行。
--  执行：mysql -h127.0.0.1 -uroot -p bank_agent < sql/upgrade_delete_cascade.sql
-- ============================================================================


-- ---------------------------------------------------------------------------
-- 0) 类型对齐：payees.linked_user_id 早期建成了 INT，users.id 是 BIGINT，
--    类型不一致会被 MySQL 拒绝建外键（errno 3780）。
-- ---------------------------------------------------------------------------
SET @ddl = IF(
  (SELECT column_type FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'payees'
      AND column_name = 'linked_user_id') = 'int',
  'ALTER TABLE payees MODIFY COLUMN linked_user_id BIGINT NULL COMMENT ''该收款人对应的站内用户 ID（NULL=行外收款人）''',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;


-- ---------------------------------------------------------------------------
-- 1) 账户 / 卡 / 流水
-- ---------------------------------------------------------------------------
SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'accounts'
      AND CONSTRAINT_NAME = 'fk_accounts_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE accounts ADD CONSTRAINT fk_accounts_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'cards'
      AND CONSTRAINT_NAME = 'fk_cards_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE cards ADD CONSTRAINT fk_cards_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

-- 卡必须挂在账户上：账户没了卡就没意义
SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'cards'
      AND CONSTRAINT_NAME = 'fk_cards_account' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE cards ADD CONSTRAINT fk_cards_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

-- ★ transactions 没有 user_id，钱包句柄是 account_id —— 靠这一条实现两级级联
SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'transactions'
      AND CONSTRAINT_NAME = 'fk_transactions_account' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE transactions ADD CONSTRAINT fk_transactions_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;


-- ---------------------------------------------------------------------------
-- 2) 收款人（user_id 级联；指向别人的两列 SET NULL）
-- ---------------------------------------------------------------------------
SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'payees'
      AND CONSTRAINT_NAME = 'fk_payees_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE payees ADD CONSTRAINT fk_payees_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'payees'
      AND CONSTRAINT_NAME = 'fk_payees_account' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE payees ADD CONSTRAINT fk_payees_account FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'payees'
      AND CONSTRAINT_NAME = 'fk_payees_linked_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE payees ADD CONSTRAINT fk_payees_linked_user FOREIGN KEY (linked_user_id) REFERENCES users(id) ON DELETE SET NULL',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;


-- ---------------------------------------------------------------------------
-- 3) 资产 / 订阅 / 在途
-- ---------------------------------------------------------------------------
SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'holdings'
      AND CONSTRAINT_NAME = 'fk_holdings_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE holdings ADD CONSTRAINT fk_holdings_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'subscriptions'
      AND CONSTRAINT_NAME = 'fk_subscriptions_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE subscriptions ADD CONSTRAINT fk_subscriptions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'pending_settlements'
      AND CONSTRAINT_NAME = 'fk_pending_settlements_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE pending_settlements ADD CONSTRAINT fk_pending_settlements_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;


-- ---------------------------------------------------------------------------
-- 4) 通知 / 操作日志 / 验证码
--    ★ 验证码是敏感数据，用户没了必须立刻跟着消失，不能留给"下一个占用这个 uid 的人"。
-- ---------------------------------------------------------------------------
SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'notifications'
      AND CONSTRAINT_NAME = 'fk_notifications_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE notifications ADD CONSTRAINT fk_notifications_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'operation_logs'
      AND CONSTRAINT_NAME = 'fk_operation_logs_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE operation_logs ADD CONSTRAINT fk_operation_logs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'verification_codes'
      AND CONSTRAINT_NAME = 'fk_verification_codes_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE verification_codes ADD CONSTRAINT fk_verification_codes_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;


-- ---------------------------------------------------------------------------
-- 5) 会话 / 消息
-- ---------------------------------------------------------------------------
SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'conversations'
      AND CONSTRAINT_NAME = 'fk_conversations_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE conversations ADD CONSTRAINT fk_conversations_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_messages'
      AND CONSTRAINT_NAME = 'fk_chat_messages_user' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE chat_messages ADD CONSTRAINT fk_chat_messages_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;

SET @ddl = IF(
  (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE CONSTRAINT_SCHEMA = DATABASE() AND TABLE_NAME = 'chat_messages'
      AND CONSTRAINT_NAME = 'fk_chat_messages_conversation' AND CONSTRAINT_TYPE = 'FOREIGN KEY') = 0,
  'ALTER TABLE chat_messages ADD CONSTRAINT fk_chat_messages_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE',
  'SELECT 1');
PREPARE ddl_stmt FROM @ddl; EXECUTE ddl_stmt; DEALLOCATE PREPARE ddl_stmt;


-- ---------------------------------------------------------------------------
-- 6) 验收：应列出 16 条外键
-- ---------------------------------------------------------------------------
SELECT rc.TABLE_NAME AS 子表, kcu.COLUMN_NAME AS 子列,
       rc.REFERENCED_TABLE_NAME AS 父表, rc.DELETE_RULE AS 删除动作
FROM information_schema.REFERENTIAL_CONSTRAINTS rc
JOIN information_schema.KEY_COLUMN_USAGE kcu
  ON kcu.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA
 AND kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
WHERE rc.CONSTRAINT_SCHEMA = DATABASE()
ORDER BY rc.TABLE_NAME, kcu.COLUMN_NAME;
