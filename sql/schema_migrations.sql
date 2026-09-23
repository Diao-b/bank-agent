-- ============================================================================
--  schema_migrations —— 迁移版本台账（P1.1）
--
--  用途：init_db.py 的迁移执行器在应用每个 sql/upgrade_*.sql 后登记
--  （文件名 + 内容 sha256 + 语句数 / 跳过数 + 时间），从而实现「只应用未应用的迁移」。
--  由 init_db.py 在应用迁移前自动创建（CREATE TABLE IF NOT EXISTS，幂等）。
--
--  ★ checksum 漂移检测：若某迁移已登记但文件内容变化（sha256 不同），
--    执行器会告警并幂等重跑（迁移脚本均写成可重复执行）。
-- ============================================================================
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   VARCHAR(128) NOT NULL COMMENT '迁移文件名（sql/ 下 upgrade_*.sql）',
    checksum   CHAR(64)     NOT NULL COMMENT '文件内容 sha256（漂移检测）',
    statements INT          NOT NULL DEFAULT 0 COMMENT '脚本语句数',
    skipped    INT          NOT NULL DEFAULT 0 COMMENT '幂等跳过（对象已存在）的语句数',
    applied_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '最近一次应用时间',
    PRIMARY KEY (filename)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='迁移版本台账（P1.1）';
