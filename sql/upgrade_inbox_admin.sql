-- ============================================================================
--  upgrade_inbox_admin.sql —— C 体系数据层升级（站内收件箱 + 后台管理）
--
--  总原则（与 README / 代码注释一致）：
--    送达体系：验证码等通知真实写入站内收件箱（数据自洽、可追溯），
--              外部通道（邮箱/推送）为可选并行增强。
--    管理端：管理员角色全局视图，与用户端数据隔离体系并存（只读）。
--
--  幂等性：所有语句可重复执行（information_schema 探测 + INSERT ... ON DUPLICATE）。
--  ★ 本脚本只增不改：不删除既有列 / 不改既有列类型，重复跑安全。
-- ============================================================================

-- ---------------------------------------------------------------- 1. notifications
-- 站内收件箱消息表。验证码类消息 meta 里带明文 code（收件箱就是"手机"，
-- 用户本人看自己的码天经地义）；expires_at 用于验证码类过期状态展示与置底。
CREATE TABLE IF NOT EXISTS notifications (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    user_id     BIGINT       NOT NULL,
    type        VARCHAR(32)  NOT NULL DEFAULT 'notice'
                COMMENT 'verify_code / transfer_receipt / notice',
    title       VARCHAR(100) NOT NULL,
    content     TEXT         NULL,
    meta        JSON         NULL COMMENT '结构化数据：验证码、流水号、用途等',
    is_read     TINYINT      NOT NULL DEFAULT 0,
    expires_at  DATETIME     NULL COMMENT '验证码类过期时间；普通通知为 NULL',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_notif_user (user_id, is_read, created_at),
    KEY idx_notif_type (user_id, type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- 2. users.role
-- 角色字段：user / admin。admin_guard 依赖它（401=未认证，403=非管理员）。
SET @role_exists := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'role'
);
SET @sql := IF(@role_exists = 0,
    'ALTER TABLE users ADD COLUMN role VARCHAR(10) NOT NULL DEFAULT ''user'' COMMENT ''user/admin''',
    'SELECT ''users.role already exists'' AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------- 3. verification_codes
-- 验证码落库表（原实现是 mock_bank 进程内存 dict，重启即丢、后台无法审计）。
-- ★ code_plain 存明文：管理后台"验证码记录→查看码"需要（真实银行客服后台
--   同样可见明文，属于受控后台视角；用户端任何接口都不回传明文）。
--   用户侧校验走哈希比对，避免表被拖库后明文直接可用。
CREATE TABLE IF NOT EXISTS verification_codes (
    id              BIGINT      NOT NULL AUTO_INCREMENT,
    user_id         BIGINT      NOT NULL,
    scene           VARCHAR(32) NOT NULL DEFAULT 'transfer'
                    COMMENT 'transfer / buy_product / realname 等',
    code_plain      VARCHAR(8)  NOT NULL COMMENT '管理员可见明文（后台客服视角）',
    code_hash       VARCHAR(64) NOT NULL COMMENT 'sha256(plain) 校验用',
    status          VARCHAR(16) NOT NULL DEFAULT 'unused'
                    COMMENT 'unused 未使用 / verified 已验证 / expired 已过期 / void 作废',
    attempts        INT         NOT NULL DEFAULT 0,
    notification_id BIGINT      NULL COMMENT '收件箱消息 id（双写同事务的证据）',
    expires_at      DATETIME    NOT NULL,
    verified_at     DATETIME    NULL,
    created_at      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_vc_user (user_id, status, expires_at),
    KEY idx_vc_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------- 4. pay password（B1）
-- 支付密码（你知）+ 验证码（你有）双要素的密码侧存储。
-- ★ 只存哈希，绝不存明文；错 3 次锁到当日 23:59:59。
SET @pph_exists := (
    SELECT COUNT(*) FROM information_schema.columns
    WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'pay_password_hash'
);
SET @sql := IF(@pph_exists = 0,
    'ALTER TABLE users '
    'ADD COLUMN pay_password_hash    VARCHAR(128) NULL COMMENT ''支付密码哈希（sha256+盐）'', '
    'ADD COLUMN pay_pwd_attempts     INT NOT NULL DEFAULT 0 COMMENT ''连续错误次数'', '
    'ADD COLUMN pay_pwd_locked_until DATETIME NULL COMMENT ''锁定截止时间（当日 23:59:59）''',
    'SELECT ''users.pay_password_hash already exists'' AS msg');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------- 5. 管理员种子账号
-- 种子：admin / admin123（role=admin），由 profile_db.ensure_admin_account()
-- 以代码生成 pbkdf2 哈希并幂等写入（SQL 侧无法生成盐哈希，见 profile_db.py）。
-- 幂等兜底：已存在但 role 被改动的，这里拉回 admin。
UPDATE users SET role = 'admin' WHERE username = 'admin';

-- ============================================================================
--  校验输出
-- ============================================================================
SELECT 'notifications'  AS object, COUNT(*) AS exists_ FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'notifications'
UNION ALL
SELECT 'verification_codes', COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 'verification_codes'
UNION ALL
SELECT 'users.role', COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'role'
UNION ALL
SELECT 'users.pay_password_hash', COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = 'pay_password_hash'
UNION ALL
SELECT 'admin_account', COUNT(*) FROM users WHERE username = 'admin' AND role = 'admin';
