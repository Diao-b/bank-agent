-- =====================================================================
-- upgrade_pii_crypto.sql —— PII 保护 Tier 1：删除冗余明文列 code_plain
--
-- 背景：verification_codes.code_plain 曾用于「复用未过期验证码时回读明文重发」
--   与「后台客服查看明文」。二者均已改造：
--     · 复用 → 改为「重新生成新码 + 旧码置 void」（不再依赖明文）；
--     · 后台 → 改为「重发验证码」（不需要看明文）。
--   ⇒ 明文列已无用途，删除（这是本任务里唯一不可逆的一步）。
--
-- ★ 只删这一列；不动任何其它字段/数据。幂等：仅当列存在时才 DROP。
-- ★ 删除前请先备份（见 docs/pii_protection_design.md 回滚方案）。
-- =====================================================================

USE bank_agent;

SET @cp_exists := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'verification_codes'
      AND column_name = 'code_plain'
);
SET @sql := IF(@cp_exists > 0,
    'ALTER TABLE verification_codes DROP COLUMN code_plain',
    'SELECT ''verification_codes.code_plain already dropped'' AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
