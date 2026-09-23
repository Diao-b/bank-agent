-- ============================================================================
--  upgrade_runtime_state.sql —— 进程内状态治理（P1.4）
--
--  把两处**安全/正确性相关**的进程内状态迁到 DB，使多 worker / 多实例下一致：
--    1) 登录失败计数 + 临时锁定：原先 run_server._LOGIN_FAILS 是进程内 dict
--       → N 个 worker 等于把"5 次锁定"放宽成 5N 次；
--    2) 支付密码"已通过"短标记（verify → resume 桥接）：原先 paypass._PASSED 是进程内 dict
--       → 多 worker 下"确认"请求落到别的进程就找不到标记。
--
--  ★ 纯新增表，不改任何既有表 / 数据，幂等可重复执行。
-- ============================================================================
USE bank_agent;

CREATE TABLE IF NOT EXISTS login_attempts (
    username      VARCHAR(64) NOT NULL COMMENT '登录名（大小写按业务判定，这里原样存）',
    fail_count    INT         NOT NULL DEFAULT 0 COMMENT '连续失败次数',
    first_fail_at DATETIME    NULL     COMMENT '首次失败时间',
    locked_until  DATETIME    NULL     COMMENT '临时锁定截止（NULL=未锁）',
    updated_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='登录失败/锁定（P1.4 跨进程共享）';

CREATE TABLE IF NOT EXISTS paypass_passed (
    user_id    BIGINT   NOT NULL COMMENT '用户 id',
    expires_at DATETIME NOT NULL COMMENT '通过标记过期时间（一次性消费）',
    PRIMARY KEY (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='支付密码"已通过"短标记（P1.4 跨进程共享）';
