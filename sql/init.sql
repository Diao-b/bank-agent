-- ============================================================================
--  AI 银行智能体 —— Mock 银行数据初始化脚本（MySQL 8.0）
--
--  用法：
--    mysql -u root -p < sql/init.sql
--  或（推荐，Windows 也友好，自动按语句切分执行）：
--    python init_db.py
--
--  设计要点：
--  1) 所有流水日期基于 CURDATE() 动态生成，"本月 / 上月"语义长期有效；
--     本月日期会被 LEAST(..., CURDATE()) 截断，不会出现"未来流水"。
--  2) 预埋三个可分析模式：
--     a) 本月「娱乐」1200 元 vs 上月 300 元 —— 环比 +300%
--     b) 本月「爱奇艺」被扣费两次（25.00 × 2）—— 重复扣费
--     c) 「腾讯视频」近 3 个月每月 1 号 30.00 元周期扣款，但【不写入
--        subscriptions 表】—— 供流水识别引擎 discover 出"新订阅"演示
--  3) 收款人含两个昵称同为「妈妈」的记录（招行 6688 / 工行 8899），用于歧义澄清
--  4) 订阅含 2 项 last_used 距今 > 60 天，用于联动演示；
--     全部 source='identified'（merchant 对齐流水商户），next_due=下月初
--  5) 用户 B（李测试）单独埋「美团外卖会员」3 笔周期流水 —— 多用户隔离下
--     discover 各查各的，B 不会发现 A 的腾讯视频
-- ============================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

CREATE DATABASE IF NOT EXISTS bank_agent
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_general_ci;

USE bank_agent;

DROP TABLE IF EXISTS operation_logs;
DROP TABLE IF EXISTS holdings;
DROP TABLE IF EXISTS subscriptions;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS transactions;
DROP TABLE IF EXISTS cards;
DROP TABLE IF EXISTS payees;
DROP TABLE IF EXISTS accounts;
DROP TABLE IF EXISTS users;
-- 会话相关表由增量脚本维护（sql/upgrade_chat.sql）。重建业务库时一并清掉，
-- 否则 chat_messages 里的 conversation_id 会全部变成指向已删用户的孤儿记录
DROP TABLE IF EXISTS chat_messages;
DROP TABLE IF EXISTS conversations;

-- ------------------------------------------------------------------ 用户
CREATE TABLE users (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    name        VARCHAR(64)  NOT NULL,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------------ 收款人
CREATE TABLE payees (
    id           BIGINT      NOT NULL AUTO_INCREMENT,
    user_id      BIGINT      NOT NULL,
    nickname     VARCHAR(64) NOT NULL,
    bank         VARCHAR(64) NOT NULL,
    account_tail VARCHAR(8)  NOT NULL,
    is_frequent  TINYINT     NOT NULL DEFAULT 0 COMMENT '1=常用收款人',
    created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_payees_user (user_id),
    KEY idx_payees_nickname (user_id, nickname)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------------ 账户
CREATE TABLE accounts (
    id         BIGINT        NOT NULL AUTO_INCREMENT,
    user_id    BIGINT        NOT NULL,
    name       VARCHAR(64)   NOT NULL,
    balance    DECIMAL(14,2) NOT NULL DEFAULT 0.00,
    created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_accounts_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------------ 流水
CREATE TABLE transactions (
    id         BIGINT        NOT NULL AUTO_INCREMENT,
    account_id BIGINT        NOT NULL,
    `date`     DATE          NOT NULL,
    amount     DECIMAL(12,2) NOT NULL,
    category   VARCHAR(32)   NOT NULL COMMENT '餐饮/购物/交通/娱乐/订阅/转账/收入',
    merchant   VARCHAR(128)  NOT NULL,
    type       VARCHAR(16)   NOT NULL DEFAULT 'expense' COMMENT 'expense=支出 income=收入',
    remark     VARCHAR(128)  NULL     COMMENT '备注（订阅自动扣费等）',
    created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_txn_account_date (account_id, `date`),
    KEY idx_txn_date_category (`date`, category),
    KEY idx_txn_merchant_date (account_id, merchant, `date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------------ 理财产品
CREATE TABLE products (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    name        VARCHAR(64)   NOT NULL,
    term        VARCHAR(32)   NOT NULL COMMENT '期限描述，如 90天',
    risk_level  TINYINT       NOT NULL COMMENT '1=低风险 2=中风险 3=中高风险',
    annual_rate DECIMAL(5,2)  NOT NULL COMMENT '年化收益率(%)',
    min_amount  DECIMAL(14,2) NOT NULL DEFAULT 0.00,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------------ 理财持仓
-- 用户已买入的理财产品。取消/赎回走软删除（status），保证前端能展示删除线状态
CREATE TABLE holdings (
    id         BIGINT        NOT NULL AUTO_INCREMENT,
    user_id    BIGINT        NOT NULL,
    product_id BIGINT        NOT NULL,
    amount     DECIMAL(14,2) NOT NULL COMMENT '持仓金额（买入本金）',
    buy_date   DATE          NOT NULL,
    status     VARCHAR(16)   NOT NULL DEFAULT 'holding' COMMENT 'holding=持有中 redeemed=已赎回',
    created_at DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_holdings_user (user_id, status),
    KEY idx_holdings_product (product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------------ 订阅/会员
-- v2 模型：订阅 = 签约代扣（contracted）+ 流水识别（identified）
--   source     contracted / identified
--   merchant   关联的流水商户名（识别来源 / 扣款引擎按它对齐流水）
--   next_due   下次扣款日（扣款引擎到期未扣即自动扣款并顺延）
--   confirmed  identified 是否经用户确认（0=待确认 1=已确认）
-- status 取值：active / cancelled / pending（识别待确认）/ ignored（已忽略）
CREATE TABLE subscriptions (
    id           BIGINT        NOT NULL AUTO_INCREMENT,
    user_id      BIGINT        NOT NULL,
    service_name VARCHAR(64)   NOT NULL,
    fee          DECIMAL(10,2) NOT NULL,
    cycle        VARCHAR(16)   NOT NULL DEFAULT '月付',
    source       VARCHAR(20)   NOT NULL DEFAULT 'identified' COMMENT 'contracted=签约代扣 identified=流水识别',
    merchant     VARCHAR(100)  NULL     COMMENT '关联的流水商户名（识别来源/扣款引擎用）',
    next_due     DATE          NULL     COMMENT '下次扣款日（签约计算/识别推算）',
    confirmed    TINYINT       NOT NULL DEFAULT 0 COMMENT 'identified 类型是否经用户确认',
    last_used    DATE          NOT NULL,
    status       VARCHAR(16)   NOT NULL DEFAULT 'active' COMMENT 'active=生效中 cancelled=已取消 pending=待确认 ignored=已忽略',
    cancelled_at DATETIME      NULL     COMMENT '取消时间（软删除，记录保留供前端展示删除线）',
    PRIMARY KEY (id),
    KEY idx_subs_user (user_id, status),
    KEY idx_subs_user_merchant (user_id, merchant)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------------ 银行卡
CREATE TABLE cards (
    id              BIGINT        NOT NULL AUTO_INCREMENT,
    user_id         BIGINT        NOT NULL,
    card_no         VARCHAR(32)   NOT NULL,
    card_name       VARCHAR(64)   NOT NULL DEFAULT ''      COMMENT '卡面展示名，如 招商储蓄卡',
    card_type       VARCHAR(16)   NOT NULL DEFAULT 'debit' COMMENT 'debit=储蓄卡 credit=信用卡',
    tail            VARCHAR(8)    NOT NULL DEFAULT ''      COMMENT '卡号后四位',
    bank            VARCHAR(64)   NOT NULL,
    credit_limit    DECIMAL(14,2) NOT NULL DEFAULT 0.00    COMMENT '信用卡总额度；储蓄卡为 0',
    available_limit DECIMAL(14,2) NOT NULL DEFAULT 0.00    COMMENT '信用卡可用额度；储蓄卡为 0',
    status          VARCHAR(16)   NOT NULL DEFAULT 'normal' COMMENT 'normal/frozen/lost',
    created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_cards_user (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ------------------------------------------------------------------ 操作日志
CREATE TABLE operation_logs (
    id         BIGINT       NOT NULL AUTO_INCREMENT,
    user_id    BIGINT       NOT NULL,
    action     VARCHAR(64)  NOT NULL,
    detail     TEXT,
    result     VARCHAR(32)  NOT NULL COMMENT 'success/failed/not_implemented',
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_oplog_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================================================
--  种子数据
-- ============================================================================

INSERT INTO users (id, name) VALUES
    (1, '张小明'),
    (2, '李测试');

-- 主账户余额 >= 30000
INSERT INTO accounts (id, user_id, name, balance) VALUES
    (1, 1, '招商银行储蓄卡（主账户）', 50000.00),
    (2, 1, '工商银行工资卡',           8600.50),
    (3, 2, '测试用户B·储蓄卡',          1200.00);

-- ★ 两个昵称同为「妈妈」的收款人 —— 歧义澄清演示
INSERT INTO payees (id, user_id, nickname, bank, account_tail, is_frequent) VALUES
    (1, 1, '妈妈',     '招商银行', '6688', 1),
    (2, 1, '妈妈',     '工商银行', '8899', 1),
    (3, 1, '李雷',     '建设银行', '4321', 0),  -- 非常用收款人，大额风控演示
    (4, 1, '房东王姐', '中国银行', '1102', 1),
    (5, 1, '张伟',     '交通银行', '5566', 1),
    (6, 1, '自己（工资卡）', '工商银行', '7788', 1);

-- 理财产品：风险等级 1-3；最高年化 8.80%（不存在 15% 的产品）
INSERT INTO products (id, name, term, risk_level, annual_rate, min_amount) VALUES
    (1, '稳赢天天利',   '活期/每日可赎', 1, 2.10,  1000.00),
    (2, '安鑫90天',     '90天',         2, 3.35, 10000.00),
    (3, '添利180天',    '180天',        2, 3.85, 50000.00),
    (4, '稳健增利一年', '365天',        2, 4.20, 100000.00),
    (5, '智盈混合A',    '365天',        3, 6.50, 100000.00),
    (6, '全球成长精选', '730天',        3, 8.80, 200000.00);

-- ★ 健身房会员 / Kindle Unlimited 的 last_used 距今 > 60 天
--   全部 source='identified'（系统从流水识别）、confirmed=1、next_due=下月初；
--   merchant 与下方 seed_txn 里实际埋的流水商户一一对齐
--   ★「腾讯视频」刻意不写入本表 —— 只在流水里埋 3 笔周期扣款，
--     供 GET /api/subscriptions/discover 发现"新订阅"演示
SET @sub_next_due := DATE_ADD(
    DATE_SUB(CURDATE(), INTERVAL DAYOFMONTH(CURDATE()) - 1 DAY),
    INTERVAL 1 MONTH);
INSERT INTO subscriptions
    (id, user_id, service_name, fee, cycle, source, merchant, next_due, confirmed, last_used) VALUES
    (1, 1, '爱奇艺黄金VIP',      25.00,  '月付', 'identified', '爱奇艺',     @sub_next_due, 1, DATE_SUB(CURDATE(), INTERVAL 2 DAY)),
    (2, 1, '网易云音乐黑胶VIP',  15.00,  '月付', 'identified', '网易云音乐', @sub_next_due, 1, DATE_SUB(CURDATE(), INTERVAL 5 DAY)),
    (3, 1, '京东PLUS',          149.00,  '年付', 'identified', '京东PLUS',   @sub_next_due, 1, DATE_SUB(CURDATE(), INTERVAL 12 DAY)),
    (4, 1, '健身房会员',        199.00,  '月付', 'identified', '健身房',     @sub_next_due, 1, DATE_SUB(CURDATE(), INTERVAL 95 DAY)),
    (5, 1, 'Kindle Unlimited',   12.00,  '月付', 'identified', 'Kindle',     @sub_next_due, 1, DATE_SUB(CURDATE(), INTERVAL 128 DAY)),
    (6, 1, '百度网盘超级会员',    30.00,  '月付', 'identified', '百度网盘',   @sub_next_due, 1, DATE_SUB(CURDATE(), INTERVAL 8 DAY)),
    (7, 1, 'iCloud+ 200G',       21.00,  '月付', 'identified', 'iCloud',     @sub_next_due, 1, DATE_SUB(CURDATE(), INTERVAL 1 DAY)),
    (8, 1, '视频网站联合会员',    40.00,  '月付', 'identified', '视频联合会员', @sub_next_due, 1, DATE_SUB(CURDATE(), INTERVAL 73 DAY));

-- 4 张卡覆盖全部视觉状态：储蓄卡蓝渐变 x2 / 信用卡深蓝金边 x2（其中 1 张已冻结灰化）
INSERT INTO cards (id, user_id, card_no, card_name, card_type, tail, bank, credit_limit, available_limit, status) VALUES
    (1, 1, '6225****1234', '招商储蓄卡', 'debit',  '1234', '招商银行',     0.00,      0.00, 'normal'),
    (2, 1, '6222****5678', '工行工资卡', 'debit',  '5678', '工商银行',     0.00,      0.00, 'normal'),
    (3, 1, '5187****9012', '招行信用卡', 'credit', '9012', '招商银行', 50000.00, 32860.00, 'normal'),
    (4, 1, '6228****3344', '建行信用卡', 'credit', '3344', '建设银行', 30000.00,  1200.00, 'frozen');

-- 持仓：3 笔共 ¥45,000，覆盖 R1/R2/R3 三档风险等级（总额低于账户余额，账目自洽）
INSERT INTO holdings (id, user_id, product_id, amount, buy_date, status) VALUES
    (1, 1, 1, 20000.00, DATE_SUB(CURDATE(), INTERVAL 120 DAY), 'holding'),
    (2, 1, 2, 10000.00, DATE_SUB(CURDATE(), INTERVAL  62 DAY), 'holding'),
    (3, 1, 5, 15000.00, DATE_SUB(CURDATE(), INTERVAL 200 DAY), 'holding');

-- ---------------------------------------------------------------------------
--  流水：用临时表描述种子，再按 CURDATE() 计算真实日期落库
--  m = 距今月数（0=本月，1=上月，2=上上月，3=再往前一个月）
--  d = 月内偏移天数（0 表示当月 1 号）
-- ---------------------------------------------------------------------------
DROP TEMPORARY TABLE IF EXISTS seed_txn;
CREATE TEMPORARY TABLE seed_txn (
    m        TINYINT       NOT NULL,
    d        TINYINT       NOT NULL,
    amount   DECIMAL(12,2) NOT NULL,
    category VARCHAR(32)   NOT NULL,
    merchant VARCHAR(128)  NOT NULL,
    type     VARCHAR(16)   NOT NULL DEFAULT 'expense',
    acct     TINYINT       NOT NULL DEFAULT 1
) ENGINE=InnoDB;

INSERT INTO seed_txn (m, d, amount, category, merchant, type, acct) VALUES
    -- ===== 上上月再往前一个月 (m=3) =====
    (3, 10, 20000.00, '收入', '公司代发工资',                 'income',  1),
    (3,  5,    42.00, '餐饮', '瑞幸咖啡',                     'expense', 1),
    (3, 12,   118.00, '餐饮', '海底捞',                       'expense', 1),
    (3,  8,   268.00, '购物', '优衣库',                       'expense', 1),
    (3, 18,   160.00, '交通', '滴滴出行',                     'expense', 1),
    (3, 22,   240.00, '娱乐', 'CGV影城',                      'expense', 1),
    (3,  3,    25.00, '订阅', '爱奇艺',                       'expense', 1),
    (3,  3,    15.00, '订阅', '网易云音乐',                   'expense', 1),
    (3,  0,    30.00, '订阅', '腾讯视频',                     'expense', 1),  -- ★周期扣款·不入订阅表
    (3, 20,  2000.00, '转账', '妈妈（招商银行 6688）',        'expense', 1),
    -- ===== 上上月 (m=2) =====
    (2, 10, 20000.00, '收入', '公司代发工资',                 'income',  1),
    (2,  2,    45.00, '餐饮', '瑞幸咖啡',                     'expense', 1),
    (2,  9,   128.00, '餐饮', '海底捞',                       'expense', 1),
    (2, 15,    62.50, '餐饮', '麦当劳',                       'expense', 1),
    (2,  5,   299.00, '购物', '优衣库',                       'expense', 1),
    (2, 18,  1299.00, '购物', '京东商城',                     'expense', 1),
    (2,  7,   200.00, '交通', '滴滴出行',                     'expense', 1),
    (2, 21,   500.00, '交通', '中国国航',                     'expense', 1),
    (2, 12,   260.00, '娱乐', 'CGV影城',                      'expense', 1),
    (2,  3,    25.00, '订阅', '爱奇艺',                       'expense', 1),
    (2,  3,    15.00, '订阅', '网易云音乐',                   'expense', 1),
    (2,  0,    30.00, '订阅', '腾讯视频',                     'expense', 1),  -- ★周期扣款·不入订阅表
    (2, 20,  2000.00, '转账', '妈妈（招商银行 6688）',        'expense', 1),
    (2,  6,   120.00, '餐饮', '星巴克',                       'expense', 2),
    -- ===== 上月 (m=1)：娱乐合计 300.00 =====
    (1, 10, 20000.00, '收入', '公司代发工资',                 'income',  1),
    (1,  1,    52.00, '餐饮', '瑞幸咖啡',                     'expense', 1),
    (1,  8,   156.00, '餐饮', '海底捞',                       'expense', 1),
    (1, 14,    73.00, '餐饮', '麦当劳',                       'expense', 1),
    (1, 22,   210.00, '餐饮', '西贝莜面村',                   'expense', 1),
    (1,  6,   399.00, '购物', '优衣库',                       'expense', 1),
    (1, 17,   899.00, '购物', '京东商城',                     'expense', 1),
    (1, 25,   158.00, '购物', '名创优品',                     'expense', 1),
    (1,  4,   180.00, '交通', '滴滴出行',                     'expense', 1),
    (1, 19,   320.00, '交通', '高铁12306',                    'expense', 1),
    (1, 11,   300.00, '娱乐', 'CGV影城',                      'expense', 1),
    (1,  3,    25.00, '订阅', '爱奇艺',                       'expense', 1),
    (1,  3,    15.00, '订阅', '网易云音乐',                   'expense', 1),
    (1,  0,    30.00, '订阅', '腾讯视频',                     'expense', 1),  -- ★周期扣款·不入订阅表
    (1,  5,    33.00, '订阅', '京东PLUS',                     'expense', 1),
    (1, 18,  3000.00, '转账', '妈妈（招商银行 6688）',        'expense', 1),
    (1, 26,   800.00, '转账', '李雷（建设银行 4321）',        'expense', 1),
    (1,  6,   120.00, '餐饮', '星巴克',                       'expense', 2),
    -- ===== 本月 (m=0)：娱乐合计 1200.00，环比 +300% =====
    (0,  0,    48.00, '餐饮', '瑞幸咖啡',                     'expense', 1),
    (0,  2,   132.00, '餐饮', '海底捞',                       'expense', 1),
    (0,  5,    68.00, '餐饮', '麦当劳',                       'expense', 1),
    (0,  4,   259.00, '购物', '优衣库',                       'expense', 1),
    (0,  1,   150.00, '交通', '滴滴出行',                     'expense', 1),
    (0,  2,   400.00, '娱乐', 'CGV影城',                      'expense', 1),
    (0,  3,   350.00, '娱乐', '密室逃脱·迷雾馆',              'expense', 1),
    (0,  5,   250.00, '娱乐', 'Steam 游戏商店',               'expense', 1),
    (0,  6,   200.00, '娱乐', '大麦网·演唱会',                'expense', 1),
    (0,  3,    25.00, '订阅', '爱奇艺',                       'expense', 1),
    (0,  6,    25.00, '订阅', '爱奇艺',                       'expense', 1),  -- ★同月重复扣费
    (0,  0,    30.00, '订阅', '腾讯视频',                     'expense', 1),  -- ★周期扣款·不入订阅表
    (0,  3,    15.00, '订阅', '网易云音乐',                   'expense', 1),
    (0,  4,  2000.00, '转账', '妈妈（招商银行 6688）',        'expense', 1),
    (0,  2,    88.00, '餐饮', '星巴克',                       'expense', 2),
    -- ===== 用户B（acct=3，李测试）：美团外卖会员周期流水 =====
    --     同样"只埋流水、不写订阅表"，验证多用户隔离：discover 各查各的，
    --     B 只会发现美团外卖会员，不会发现 A 的腾讯视频
    (3,  0,    15.00, '订阅', '美团外卖会员',                 'expense', 3),  -- ★B专属·不入订阅表
    (2,  0,    15.00, '订阅', '美团外卖会员',                 'expense', 3),  -- ★B专属·不入订阅表
    (1,  0,    15.00, '订阅', '美团外卖会员',                 'expense', 3);  -- ★B专属·不入订阅表

INSERT INTO transactions (account_id, `date`, amount, category, merchant, type)
SELECT
    st.acct,
    CASE st.m
        WHEN 0 THEN LEAST(
            DATE_ADD(DATE_SUB(CURDATE(), INTERVAL DAYOFMONTH(CURDATE()) - 1 DAY), INTERVAL st.d DAY),
            CURDATE()
        )
        ELSE DATE_ADD(
            DATE_SUB(DATE_SUB(CURDATE(), INTERVAL DAYOFMONTH(CURDATE()) - 1 DAY), INTERVAL st.m MONTH),
            INTERVAL st.d DAY
        )
    END AS txn_date,
    st.amount,
    st.category,
    st.merchant,
    st.type
FROM seed_txn st
ORDER BY st.m DESC, st.d ASC, st.acct ASC;

DROP TEMPORARY TABLE IF EXISTS seed_txn;

SET FOREIGN_KEY_CHECKS = 1;

-- ---------------------------------------------------------------------------
--  自检：确认预埋模式成立
-- ---------------------------------------------------------------------------
SELECT '--- 本月 vs 上月 娱乐支出 ---' AS check_item;
SELECT
    DATE_FORMAT(CURDATE(), '%Y-%m')                                                   AS cur_month,
    COALESCE(SUM(CASE WHEN DATE_FORMAT(`date`, '%Y-%m') = DATE_FORMAT(CURDATE(), '%Y-%m')
                      THEN amount END), 0)                                            AS cur_month_entertainment,
    COALESCE(SUM(CASE WHEN DATE_FORMAT(`date`, '%Y-%m')
                           = DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m')
                      THEN amount END), 0)                                            AS last_month_entertainment
FROM transactions
WHERE category = '娱乐' AND type = 'expense';

SELECT '--- 爱奇艺 本月扣费笔数（重复扣费模式，应为 2） ---' AS check_item;
SELECT COUNT(*) AS cnt, SUM(amount) AS total
FROM transactions
WHERE merchant = '爱奇艺'
  AND type = 'expense'
  AND DATE_FORMAT(`date`, '%Y-%m') = DATE_FORMAT(CURDATE(), '%Y-%m');

SELECT '--- 腾讯视频 周期流水（近 4 个月每月 1 号 30.00，共 4 笔） ---' AS check_item;
SELECT COUNT(*) AS cnt, MIN(`date`) AS first_date, MAX(`date`) AS last_date, SUM(amount) AS total
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE t.merchant = '腾讯视频' AND t.type = 'expense' AND a.user_id = 1;

SELECT '--- 腾讯视频 / 美团外卖会员 均不在订阅表（discover 待发现） ---' AS check_item;
SELECT COUNT(*) AS in_subscriptions
FROM subscriptions
WHERE merchant IN ('腾讯视频', '美团外卖会员');

SELECT '--- 用户B 美团外卖会员 周期流水（共 3 笔） ---' AS check_item;
SELECT COUNT(*) AS cnt, SUM(amount) AS total
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE t.merchant = '美团外卖会员' AND t.type = 'expense' AND a.user_id = 2;

SELECT '--- 闲置订阅（last_used > 60 天） ---' AS check_item;
SELECT service_name, fee, last_used, DATEDIFF(CURDATE(), last_used) AS idle_days
FROM subscriptions
WHERE DATEDIFF(CURDATE(), last_used) > 60;
