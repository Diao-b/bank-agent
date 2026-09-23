-- ============================================================================
--  upgrade_account_system.sql —— 收款人与转账账户体系重构（幂等）
--
--  目标：把转账标的物从「姓名」升级为「账户（卡号）」，实现真实银行的
--        账户校验模型：
--        - 站内转账：收款账号在本系统内 → 后端实时校验账户存在 → 实时到账
--        - 跨行转账：收款账号在其他银行 → 仅做 Luhn + BIN 银行校验，
--          T+0 提交、2 小时内到账（演示口径）
--        - 收款人簿（payees）= 用户保存的账户快捷方式：每条必须绑定一个
--          已校验的账号 + 开户行，不允许仅凭姓名 + 自填卡号转账
--
--  幂等约定：
--        - CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS（MySQL 8.0.12+）
--        - 银行目录用 INSERT ... ON DUPLICATE KEY UPDATE，重复执行不增行
--        - bank_id 回填只对 bank_id IS NULL 的行执行，重复执行为空操作
--        - 卡号生成（Luhn 合法）由 Python 种子脚本 seed_account_system.py 完成，
--          因为 SQL 不擅长算 Luhn 校验位；本文件只负责 DDL + 银行目录 + bank_id 回填。
--
--  执行顺序（见 scripts/seed_account_system.py）：
--        1) 本文件（DDL + 银行目录种子 + bank_id 回填）
--        2) Python 脚本补 card_no / account_tail / 李雷→微众+link 李哥
-- ============================================================================

USE bank_agent;
SET FOREIGN_KEY_CHECKS = 0;

-- ------------------------------------------------------------------ accounts
-- 完整虚拟卡号（16 位，Luhn 合法）。演示用户 / 李哥等所有存量账户补卡号。
-- ★ 幂等由 scripts/seed_account_system.py 统一保证（先查 information_schema 再决定
--   是否 ALTER）；本文件 ALTER 用普通 ADD COLUMN，便于人工直接执行一次。
ALTER TABLE accounts
  ADD COLUMN card_no VARCHAR(19) NULL
  COMMENT '完整虚拟卡号（16 位，Luhn 校验合法）；本站账户均开在微众银行行别下' AFTER name;

-- ------------------------------------------------------------------ 银行目录
-- 全量真实银行清单（演示级约 20 家）。★ 本行 = 微众银行（is_local=1）。
CREATE TABLE IF NOT EXISTS bank_directory (
    id         INT          NOT NULL AUTO_INCREMENT,
    bank_name  VARCHAR(64)  NOT NULL,
    bank_code  VARCHAR(16)  NOT NULL  COMMENT '联行号演示用，也是 BIN→银行的映射键',
    is_local   TINYINT      NOT NULL DEFAULT 0 COMMENT '1=本行（微众银行），0=他行',
    PRIMARY KEY (id),
    UNIQUE KEY uk_bank_code (bank_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 种子导入（idempotent：bank_code 唯一，重复执行只更新名称/本行标记）
INSERT INTO bank_directory (id, bank_name, bank_code, is_local) VALUES
    (1,  '中国工商银行', 'ICBC',   0),
    (2,  '中国农业银行', 'ABC',    0),
    (3,  '中国银行',     'BOC',    0),
    (4,  '中国建设银行', 'CCB',    0),
    (5,  '交通银行',     'BCM',    0),
    (6,  '招商银行',     'CMBC',   0),
    (7,  '中信银行',     'CITIC',  0),
    (8,  '中国光大银行', 'CEB',    0),
    (9,  '华夏银行',     'HXB',    0),
    (10, '中国民生银行', 'MSB',    0),
    (11, '平安银行',     'PAB',    0),
    (12, '兴业银行',     'CIB',    0),
    (13, '浦发银行',     'SPDB',   0),
    (14, '邮储银行',     'PSBC',   0),
    (15, '北京银行',     'BJB',    0),
    (16, '上海银行',     'SHB',    0),
    (17, '江苏银行',     'JSB',    0),
    (18, '宁波银行',     'NBCB',   0),
    (19, '微众银行',     'WEBANK', 1)   -- ★ 本行
ON DUPLICATE KEY UPDATE bank_name = VALUES(bank_name), is_local = VALUES(is_local);

-- ------------------------------------------------------------------ 收款人重构
-- payee_name（显示昵称）= 原 nickname 语义，保留 nickname 列作展示名；
-- real_name：对方真实姓名（站内来自 users 表，跨行来自填单）；
-- bank_id：bank_directory 外键；
-- card_no：完整卡号（Luhn 合法）；card_tail 由 card_no 派生（保留列作展示镜像）；
-- linked_user_id：保留（站内绑定）。
ALTER TABLE payees
  ADD COLUMN real_name VARCHAR(64) NULL
  COMMENT '对方真实姓名（站内来自用户表，跨行来自填单）' AFTER nickname;
ALTER TABLE payees
  ADD COLUMN bank_id INT NULL
  COMMENT 'bank_directory 外键（开户行）' AFTER bank;
ALTER TABLE payees
  ADD COLUMN card_no VARCHAR(19) NULL
  COMMENT '完整卡号（Luhn 合法）；转账标的物，最终以账号为准' AFTER bank_id;

-- 存量收款人按旧 bank 名回填 bank_id（仅对尚未回填的行；李雷会在 Python 端改成本行）
UPDATE payees p
  JOIN bank_directory b ON b.bank_name = p.bank
  SET p.bank_id = b.id
  WHERE p.bank_id IS NULL;

SET FOREIGN_KEY_CHECKS = 1;
