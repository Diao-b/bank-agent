-- P0.2：一次性操作授权票据表（op_tickets）
-- 与 verification_codes / paypass 解耦：本表只负责"write_flow 核实后的单次执行凭证"。
-- 幂等：CREATE TABLE IF NOT EXISTS；可重复执行（init_db 按 UPGRADE_ORDER 顺序跑）。

CREATE TABLE IF NOT EXISTS op_tickets (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  user_id     BIGINT       NOT NULL,
  scene       VARCHAR(48)  NOT NULL,
  token_hash  CHAR(64)     NOT NULL,
  status      VARCHAR(16)  NOT NULL DEFAULT 'active',
  expires_at  DATETIME     NOT NULL,
  consumed_at DATETIME     NULL     DEFAULT NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_op_ticket_hash (token_hash),
  KEY idx_op_ticket_user_scene (user_id, scene)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='一次性操作授权票据（P0.2）';
