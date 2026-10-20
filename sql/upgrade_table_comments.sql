-- =====================================================================
-- upgrade_table_comments.sql —— 给全部 32 张表写「通俗易懂」的表备注（TABLE COMMENT）
--
-- 作用：看库时，每张表是干什么的一眼就懂。
-- 影响：只改表注释（元数据），不动任何字段、不动数据。可重复执行。
-- =====================================================================

USE bank_agent;

-- 用户 / 账号 / 安全
ALTER TABLE `users`                 COMMENT = '用户账号（注册的人都在这里）';
ALTER TABLE `login_attempts`        COMMENT = '登录失败次数与锁定，防止别人暴力猜密码';
ALTER TABLE `verification_codes`    COMMENT = '验证码记录（发了什么码、有没有验证通过、错了几次）';
ALTER TABLE `op_tickets`            COMMENT = '一次性操作凭证（重要操作前先发一张只能用一次的门票）';
ALTER TABLE `paypass_passed`        COMMENT = '支付密码本次已通过的临时标记（对完一次密码短期内不用再对）';

-- 账户 / 银行卡
ALTER TABLE `accounts`              COMMENT = '用户的银行账户（钱存在这里，如活期账户、信用账户）';
ALTER TABLE `cards`                 COMMENT = '用户名下的银行卡';
ALTER TABLE `bank_directory`        COMMENT = '支持的银行名单（跨行转账选银行时用）';
ALTER TABLE `external_accounts`     COMMENT = '他行账户登记表（跨行转账需要清算的对方账户）';

-- 交易 / 收款人 / 转账
ALTER TABLE `transactions`          COMMENT = '交易流水（每一笔钱的进出明细，最核心的一张表）';
ALTER TABLE `payees`                COMMENT = '用户保存的收款人名单（转账常用对象）';
ALTER TABLE `transfer_daily_quota`  COMMENT = '转账每日额度（记录某天已用多少、还剩多少）';
ALTER TABLE `pending_settlements`   COMMENT = '跨行转账在途清算记录（钱还在路上的那部分）';

-- 理财 / 订阅
ALTER TABLE `products`              COMMENT = '理财产品目录（有哪些产品可买）';
ALTER TABLE `holdings`              COMMENT = '用户的理财持仓（买了哪些产品、多少钱）';
ALTER TABLE `subscriptions`         COMMENT = '用户的自动扣费订阅（如会员每月扣款）';

-- 对话 / 消息 / 收件箱
ALTER TABLE `conversations`         COMMENT = '会话列表（用户和 AI 的每个聊天窗口）';
ALTER TABLE `chat_messages`         COMMENT = '聊天消息（用户与 AI 的每一条对话内容）';
ALTER TABLE `notifications`         COMMENT = '站内通知 / 收件箱（系统给用户发的消息）';

-- 参数 / 知识库
ALTER TABLE `system_config`         COMMENT = '系统参数设置（可后台调整的业务数值，如限额、费率、开关）';
ALTER TABLE `knowledge_chunks`      COMMENT = '知识库切片（把说明文档切小段并转向量，供 AI 检索作答）';

-- 日志 / 审计
ALTER TABLE `operation_logs`        COMMENT = '用户操作日志（谁在什么时候做了什么业务动作）';
ALTER TABLE `admin_logs`            COMMENT = '管理员操作日志（后台每一次改动都留痕，可追溯）';
ALTER TABLE `idempotency_records`   COMMENT = '防重复提交记录（同一个请求只会真正执行一次）';
ALTER TABLE `schema_migrations`     COMMENT = '数据库升级台账（记录每个改动脚本有没有执行过）';

-- 支付风控（模块 A）
ALTER TABLE `risk_rules`            COMMENT = '风控规则设置（什么情况算可疑、要多严）';
ALTER TABLE `risk_events`           COMMENT = '风控告警记录（如可疑转账，进入后台待处理队列）';
ALTER TABLE `risk_event_views`      COMMENT = '风控详情查看记录（谁看过告警的完整内容，留痕）';

-- 卡密充值（模块 B）
ALTER TABLE `card_keys`             COMMENT = '卡密明细（用户用它充值；只存加密后的码，不存明文）';
ALTER TABLE `card_key_batches`      COMMENT = '卡密批次汇总（一次生成多少张、每张多大面额）';
ALTER TABLE `card_redeem_throttle`  COMMENT = '卡密核销限流（限制同一人/IP 每分钟能试几次，防爆破）';

-- 对账
ALTER TABLE `balance_reconcile`     COMMENT = '对账快照（记录上次余额，用来发现钱被人绕过流水手工改过）';
