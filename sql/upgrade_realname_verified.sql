-- ============================================================================
--  upgrade_realname_verified.sql —— 实名认证「已完成」标志（幂等）
--
--  背景：当前 users.real_name 同时被两条路径写入：
--    - ③ 基本信息「真实姓名」保存（PUT /api/profile，无验证）
--    - ④ 实名认证核验通过（POST /api/realname/confirm，有验证码）
--  两条路径都写同一个字段，前端无法判断「是否已正式通过 ④ 核验」，
--  导致 ④ 卡片在完成后仍暴露修改表单（违反「敏感身份变更必须柜台办理」）。
--
--  修法：新增 realname_verified 标志位，仅 ④ 核验通过置 1；
--    - 前端 ④ 卡片按该标志切换「已实名认证」只读态 / 填写态；
--    - 后端 /api/realname/confirm 拦截「已完成」的二次核验（防绕过前端直接打 API）。
--
--  迁移策略：已有 real_name 的存量用户视为已认证（与现状兼容，不阻塞已认证用户）。
-- ============================================================================

USE bank_agent;
SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- 1) 幂等加列
SET @sql_col = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'realname_verified') = 0,
    "ALTER TABLE users ADD COLUMN realname_verified TINYINT NOT NULL DEFAULT 0 "
    "COMMENT '实名认证是否已完成（仅 ④ 核验通过置 1；与 ③ 基本信息真实姓名无关）'",
    'SELECT 1'
);
PREPARE stmt_col FROM @sql_col;
EXECUTE stmt_col;
DEALLOCATE PREPARE stmt_col;

-- 2) 回填：已有 real_name 的存量用户视为已认证（与现状兼容）
UPDATE users SET realname_verified = 1
WHERE real_name IS NOT NULL AND real_name <> '' AND realname_verified = 0;

SET FOREIGN_KEY_CHECKS = 1;

-- --------------------------------------------------------------------------- #
--  自检
-- --------------------------------------------------------------------------- #
SELECT '--- realname_verified 列 ---' AS check_item;
SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_DEFAULT
FROM information_schema.columns
WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'realname_verified';

SELECT '--- 已认证用户数（应 = 有 real_name 的用户数）---' AS check_item;
SELECT
    (SELECT COUNT(*) FROM users WHERE realname_verified = 1) AS verified,
    (SELECT COUNT(*) FROM users WHERE real_name IS NOT NULL AND real_name <> '') AS have_real_name;
