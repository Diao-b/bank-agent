-- =====================================================================
-- upgrade_role_expand.sql —— 角色分级扩展（模块 A 硬前提）
--
-- 原有 users.role 为 varchar(10)，仅容纳 'user' / 'admin'。
-- 'super_admin' 有 11 个字符，varchar(10) 放不下；扩到 varchar(16)
-- 以容纳新增的 risk_ops / super_admin 两档。
--
-- 角色层级（最小权限）：
--   user      普通用户（卡密核销 / 查看自身数据）
--   risk_ops  风控专员：仅支付风控（查看队列 / 配置规则 / 展开明细-审计）
--   admin     通用后台：风控只读 + 卡密列表/导出（不含生成/作废）+ 既有后台能力
--   super_admin 超级管理员：admin 全部 + 卡密生成/作废（"印钞"原语，最高权限）+ 角色管理
--
-- 能力判定全部在代码（routes/_common.py::ROLE_CAPS）中按 DB 复核后的 role 进行，
-- 本脚本只负责把列宽放宽、不破坏现有 'user'/'admin' 数据。
-- 演示账号（riskop / superadmin）由 scripts/seed_roles.py 生成，不在此建用户行。
-- =====================================================================

USE bank_agent;

-- 仅当 role 列宽 < 16 时才 ALTER（可重复执行）
SET @re_exists := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'users'
      AND column_name = 'role' AND CHARACTER_MAXIMUM_LENGTH < 16
);
SET @sql := IF(@re_exists > 0,
    "ALTER TABLE users MODIFY COLUMN role VARCHAR(16) NOT NULL DEFAULT 'user' "
    "COMMENT 'user/admin/risk_ops/super_admin'",
    "SELECT 'users.role already wide enough' AS msg");
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
