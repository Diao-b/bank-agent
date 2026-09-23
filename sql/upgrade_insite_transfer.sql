-- ============================================================================
--  upgrade_insite_transfer.sql —— 站内转账闭环 · 数据层迁移（幂等）
--
--  设计原则（写入注释，转账按收款人身份分两轨）：
--  - 站内转账（收款人是本系统注册用户）：
--        发送方扣款 + 收款方账户入账 + 双方收件箱通知 + 双方流水
--        —— 资金闭环、账实一致
--  - 行外转账（收款人仅为 payees 记录，linked_user_id 为 NULL）：
--        发送方扣款 + 发送方收件箱到账通知 —— 资金离开本行
--  ★ 金额在系统内守恒：站内转账双方余额变动之和为零
--
--  本脚本做三件事（全部幂等，可重复执行）：
--    1) payees 表新增 linked_user_id 列（NULL=行外收款人）
--    2) 造测试数据：创建第二个完整用户「李哥」
--       （用户名 lige / 密码 lige123，role=user，资产由 seeder 初始化）
--    3) 把现有收款人「李雷」绑定 linked_user_id = 李哥用户 ID
--       （★ 此后「李雷」从行外人变站内用户；风控规则里 is_frequent
--         仍以 payees 记录为准，不受绑定影响）
--
--  说明：用户的「账户 / 卡片 / 流水 / 订阅」等资产初始化是 Python 逻辑
--        （utils/user_provision.provision_new_user），无法用纯 SQL 表达，
--        请务必再跑一次 scripts/seed_insite_transfer.py 完成资产初始化。
-- ============================================================================

USE bank_agent;
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- --------------------------------------------------------------------------- #
--  1) payees 新增 linked_user_id（幂等：列已存在则跳过）
--     ★ 类型必须是 BIGINT（= users.id 的类型），否则 sql/upgrade_delete_cascade.sql
--       里那条 `payees.linked_user_id → users.id` 外键会因类型不一致建不上（errno 3780）。
-- --------------------------------------------------------------------------- #
SET @sql_linked = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 'payees' AND column_name = 'linked_user_id') = 0,
    "ALTER TABLE payees ADD COLUMN linked_user_id BIGINT NULL "
    "COMMENT '该收款人对应的站内用户 ID（NULL=行外收款人）' AFTER account_tail",
    'SELECT 1'
);
PREPARE stmt_linked FROM @sql_linked;
EXECUTE stmt_linked;
DEALLOCATE PREPARE stmt_linked;

-- --------------------------------------------------------------------------- #
--  2) 创建测试用户「李哥」（幂等：username=lige 已存在则跳过）
--     password_hash 用与 profile_db.hash_password 同格式的 pbkdf2-sha256，
--     固定盐保证 SQL 可复现（密码 = lige123）。
-- --------------------------------------------------------------------------- #
INSERT INTO users (name, username, password_hash, role)
SELECT '李哥', 'lige',
       'pbkdf2$sha256$100000$bElnRTIwMjZzZWVkMDE=$xTX66P4Aro6H7JrBS34YzS7aNSLyB3P/jkKprAA5jts=',
       'user'
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM users WHERE username = 'lige');

-- 若用户已存在（如由 seeder 先建），确保 name / role 正确
UPDATE users SET name = '李哥', role = 'user' WHERE username = 'lige';

-- --------------------------------------------------------------------------- #
--  3) 绑定「李雷」→ 李哥（幂等：仅当尚未绑定时更新）
-- --------------------------------------------------------------------------- #
UPDATE payees p
JOIN users u ON u.username = 'lige'
SET p.linked_user_id = u.id
WHERE p.nickname = '李雷'
  AND p.user_id = 1
  AND p.linked_user_id IS NULL;

SET FOREIGN_KEY_CHECKS = 1;

-- --------------------------------------------------------------------------- #
--  自检
-- --------------------------------------------------------------------------- #
SELECT '--- payees.linked_user_id 列 ---' AS check_item;
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM information_schema.columns
WHERE table_schema = DATABASE() AND table_name = 'payees' AND column_name = 'linked_user_id';

SELECT '--- 李哥用户 ---' AS check_item;
SELECT id, name, username, role FROM users WHERE username = 'lige';

SELECT '--- 李雷绑定结果 ---' AS check_item;
SELECT id, nickname, bank, account_tail, linked_user_id,
       (SELECT username FROM users WHERE id = linked_user_id) AS linked_username
FROM payees WHERE nickname = '李雷' AND user_id = 1;
