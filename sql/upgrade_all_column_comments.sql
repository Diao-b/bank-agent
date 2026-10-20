-- =====================================================================
-- upgrade_all_column_comments.sql —— 银行库全部表「缺失字段」的中文备注（自动生成）
--
-- 由 scripts/_gen_all_comments.py 依据 information_schema 真实定义生成：
-- 只追加 COMMENT，类型 / NULL / 默认值 / AUTO_INCREMENT / ON UPDATE 均原样保留。
-- 可重复执行（幂等）。覆盖：非模块 A/B 表的 123 个原缺备注字段。
-- =====================================================================

USE bank_agent;

-- ---------------------------------------------------------------- accounts
ALTER TABLE `accounts`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '账户ID（主键）',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '所属用户ID（users.id）',
  MODIFY COLUMN `name` varchar(64) NOT NULL COMMENT '账户名称（如 活期储蓄账户）',
  MODIFY COLUMN `balance` decimal(14,2) NOT NULL DEFAULT 0.00 COMMENT '账户余额（元）',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '开户时间';

-- ---------------------------------------------------------------- admin_logs
ALTER TABLE `admin_logs`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '日志ID（主键）',
  MODIFY COLUMN `admin_id` bigint NOT NULL COMMENT '操作管理员（users.id）',
  MODIFY COLUMN `action` varchar(64) NOT NULL COMMENT '操作类型（user_lock / config_update / cardkey_generate 等）',
  MODIFY COLUMN `target_type` varchar(32) NOT NULL DEFAULT '' COMMENT '操作对象类型（user / config / product / risk_rule / card_batch 等）',
  MODIFY COLUMN `target_id` varchar(64) NULL DEFAULT '' COMMENT '操作对象ID',
  MODIFY COLUMN `detail` text NULL DEFAULT NULL COMMENT '操作详情（JSON，含 old→new）',
  MODIFY COLUMN `pending_sync` tinyint(1) NOT NULL DEFAULT 0 COMMENT '是否标记「知识文档待同步」：1=待同步',
  MODIFY COLUMN `sync_done_at` datetime NULL DEFAULT NULL COMMENT '知识同步完成时间',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录时间';

-- ---------------------------------------------------------------- bank_directory
ALTER TABLE `bank_directory`
  MODIFY COLUMN `id` int NOT NULL AUTO_INCREMENT COMMENT '银行ID（主键）',
  MODIFY COLUMN `bank_name` varchar(64) NOT NULL COMMENT '银行名称';

-- ---------------------------------------------------------------- cards
ALTER TABLE `cards`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '卡片ID（主键）',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '所属用户ID（users.id）',
  MODIFY COLUMN `card_no` varchar(32) NOT NULL COMMENT '银行卡号（掩码 / 尾号展示用）',
  MODIFY COLUMN `bank` varchar(64) NOT NULL COMMENT '所属银行',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '开卡时间';

-- ---------------------------------------------------------------- chat_messages
ALTER TABLE `chat_messages`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '消息ID（主键）',
  MODIFY COLUMN `conversation_id` varchar(64) NOT NULL COMMENT '会话ID（conversations.conversation_id）',
  MODIFY COLUMN `content` text NOT NULL COMMENT '消息内容',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  MODIFY COLUMN `user_id` bigint NULL DEFAULT NULL COMMENT '所属用户ID（users.id）';

-- ---------------------------------------------------------------- conversations
ALTER TABLE `conversations`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '所属用户ID（users.id）',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  MODIFY COLUMN `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间';

-- ---------------------------------------------------------------- external_accounts
ALTER TABLE `external_accounts`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '外部（他行）账户ID（主键）',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';

-- ---------------------------------------------------------------- holdings
ALTER TABLE `holdings`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '持仓ID（主键）',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '持仓用户ID（users.id）',
  MODIFY COLUMN `product_id` bigint NOT NULL COMMENT '理财产品ID（products.id）',
  MODIFY COLUMN `buy_date` date NOT NULL COMMENT '买入日期',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';

-- ---------------------------------------------------------------- idempotency_records
ALTER TABLE `idempotency_records`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '记录ID（主键）',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';

-- ---------------------------------------------------------------- knowledge_chunks
ALTER TABLE `knowledge_chunks`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '切片ID（主键）',
  MODIFY COLUMN `doc_name` varchar(128) NOT NULL COMMENT '来源文档名（docs/knowledge/*.md）',
  MODIFY COLUMN `heading` varchar(255) NULL DEFAULT '' COMMENT '所属标题',
  MODIFY COLUMN `content` text NOT NULL COMMENT '切片正文',
  MODIFY COLUMN `chunk_index` int NOT NULL DEFAULT 0 COMMENT '文档内切片序号（从 0 起）',
  MODIFY COLUMN `embedding` json NULL DEFAULT NULL COMMENT '向量嵌入（JSON 数组）',
  MODIFY COLUMN `embed_model` varchar(64) NULL DEFAULT '' COMMENT '生成 embedding 的模型名',
  MODIFY COLUMN `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间';

-- ---------------------------------------------------------------- login_attempts
ALTER TABLE `login_attempts`
  MODIFY COLUMN `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间';

-- ---------------------------------------------------------------- notifications
ALTER TABLE `notifications`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '通知ID（主键）',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '接收用户ID（users.id）',
  MODIFY COLUMN `title` varchar(100) NOT NULL COMMENT '通知标题',
  MODIFY COLUMN `content` text NULL DEFAULT NULL COMMENT '通知正文',
  MODIFY COLUMN `is_read` tinyint NOT NULL DEFAULT 0 COMMENT '是否已读：1=已读，0=未读',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';

-- ---------------------------------------------------------------- op_tickets
ALTER TABLE `op_tickets`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '票据ID（主键）',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '持有人（users.id）',
  MODIFY COLUMN `scene` varchar(48) NOT NULL COMMENT '使用场景（如 internal_transfer / wealth_buy）',
  MODIFY COLUMN `token_hash` char(64) NOT NULL COMMENT '票据哈希（SHA-256，不存明文）',
  MODIFY COLUMN `status` varchar(16) NOT NULL DEFAULT 'active' COMMENT '状态：active=有效 / used=已用 / expired=已过期',
  MODIFY COLUMN `expires_at` datetime NOT NULL COMMENT '过期时间',
  MODIFY COLUMN `consumed_at` datetime NULL DEFAULT NULL COMMENT '使用时间',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';

-- ---------------------------------------------------------------- operation_logs
ALTER TABLE `operation_logs`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '日志ID（主键）',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '操作用户ID（users.id）',
  MODIFY COLUMN `action` varchar(64) NOT NULL COMMENT '操作类型（transfer / verify_send / ...）',
  MODIFY COLUMN `detail` text NULL DEFAULT NULL COMMENT '操作详情（JSON）',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录时间';

-- ---------------------------------------------------------------- payees
ALTER TABLE `payees`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '收款人ID（主键）',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '归属用户ID（users.id）',
  MODIFY COLUMN `nickname` varchar(64) NOT NULL COMMENT '收款人昵称 / 姓名',
  MODIFY COLUMN `bank` varchar(64) NOT NULL COMMENT '收款银行',
  MODIFY COLUMN `account_tail` varchar(8) NOT NULL COMMENT '收款账号尾号',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';

-- ---------------------------------------------------------------- pending_settlements
ALTER TABLE `pending_settlements`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '在途清算ID（主键）',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';

-- ---------------------------------------------------------------- products
ALTER TABLE `products`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '理财产品ID（主键）',
  MODIFY COLUMN `name` varchar(64) NOT NULL COMMENT '产品名称',
  MODIFY COLUMN `min_amount` decimal(14,2) NOT NULL DEFAULT 0.00 COMMENT '起购金额（元）',
  MODIFY COLUMN `status` varchar(16) NOT NULL DEFAULT 'on' COMMENT '状态：on=上架 / off=下架';

-- ---------------------------------------------------------------- subscriptions
ALTER TABLE `subscriptions`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '订阅ID（主键）',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '订阅用户ID（users.id）',
  MODIFY COLUMN `service_name` varchar(64) NOT NULL COMMENT '服务名称（如 视频会员）',
  MODIFY COLUMN `fee` decimal(10,2) NOT NULL COMMENT '每期费用（元）',
  MODIFY COLUMN `cycle` varchar(16) NOT NULL DEFAULT '月付' COMMENT '扣费周期（月付 / 年付）',
  MODIFY COLUMN `last_used` date NOT NULL COMMENT '最近一次使用 / 扣费日期';

-- ---------------------------------------------------------------- system_config
ALTER TABLE `system_config`
  MODIFY COLUMN `key` varchar(64) NOT NULL COMMENT '参数键（rules.RULE_SPECS 的 key）',
  MODIFY COLUMN `value` varchar(128) NOT NULL COMMENT '参数当前值',
  MODIFY COLUMN `label` varchar(128) NOT NULL COMMENT '参数中文名',
  MODIFY COLUMN `value_type` varchar(16) NOT NULL DEFAULT 'int' COMMENT '值类型：int / decimal',
  MODIFY COLUMN `min_value` decimal(18,2) NULL DEFAULT NULL COMMENT '允许最小值',
  MODIFY COLUMN `max_value` decimal(18,2) NULL DEFAULT NULL COMMENT '允许最大值',
  MODIFY COLUMN `unit` varchar(24) NULL DEFAULT '' COMMENT '单位（元 / 次 / 秒 / 比例 / 开关 等）',
  MODIFY COLUMN `affects_knowledge` tinyint(1) NOT NULL DEFAULT 0 COMMENT '是否影响 RAG 知识文档：1=改后需同步',
  MODIFY COLUMN `docs_ref` varchar(255) NULL DEFAULT '' COMMENT '需同步的知识文档',
  MODIFY COLUMN `updated_by` bigint NULL DEFAULT NULL COMMENT '最后修改人（users.id）',
  MODIFY COLUMN `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后修改时间';

-- ---------------------------------------------------------------- transactions
ALTER TABLE `transactions`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '流水ID（主键）',
  MODIFY COLUMN `account_id` bigint NOT NULL COMMENT '所属账户ID（accounts.id）',
  MODIFY COLUMN `date` date NOT NULL COMMENT '交易日期',
  MODIFY COLUMN `amount` decimal(12,2) NOT NULL COMMENT '交易金额（元）',
  MODIFY COLUMN `merchant` varchar(128) NOT NULL COMMENT '交易对手 / 摘要（转账时为「收款人（银行 尾号）」派生串）',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';

-- ---------------------------------------------------------------- transfer_daily_quota
ALTER TABLE `transfer_daily_quota`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '额度记录ID（主键）',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '用户ID（users.id）',
  MODIFY COLUMN `quota_date` date NOT NULL COMMENT '额度所属日期',
  MODIFY COLUMN `used_amount` decimal(18,2) NOT NULL DEFAULT 0.00 COMMENT '当日已用转账额度（元）';

-- ---------------------------------------------------------------- users
ALTER TABLE `users`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '用户ID（主键）',
  MODIFY COLUMN `name` varchar(64) NOT NULL COMMENT '姓名 / 展示名',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
  MODIFY COLUMN `real_name` varchar(50) NULL DEFAULT NULL COMMENT '实名认证姓名',
  MODIFY COLUMN `gender` varchar(10) NULL DEFAULT NULL COMMENT '性别',
  MODIFY COLUMN `title_prefix` varchar(20) NULL DEFAULT NULL COMMENT '称呼前缀（如 先生 / 女士）',
  MODIFY COLUMN `phone_tail` varchar(20) NULL DEFAULT NULL COMMENT '手机号尾号（脱敏存储）',
  MODIFY COLUMN `occupation` varchar(50) NULL DEFAULT NULL COMMENT '职业',
  MODIFY COLUMN `preferences` json NULL DEFAULT NULL COMMENT '偏好设置（JSON，如风险偏好）',
  MODIFY COLUMN `updated_at` datetime NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '资料更新时间',
  MODIFY COLUMN `locked` tinyint(1) NOT NULL DEFAULT 0 COMMENT '是否锁定：1=锁定（禁止登录），0=正常';

-- ---------------------------------------------------------------- verification_codes
ALTER TABLE `verification_codes`
  MODIFY COLUMN `id` bigint NOT NULL AUTO_INCREMENT COMMENT '验证码记录ID（主键）',
  MODIFY COLUMN `user_id` bigint NOT NULL COMMENT '用户ID（users.id）',
  MODIFY COLUMN `attempts` int NOT NULL DEFAULT 0 COMMENT '已尝试核验次数',
  MODIFY COLUMN `expires_at` datetime NOT NULL COMMENT '过期时间',
  MODIFY COLUMN `verified_at` datetime NULL DEFAULT NULL COMMENT '核验通过时间',
  MODIFY COLUMN `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';
