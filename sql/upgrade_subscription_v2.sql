-- ============================================================================
--  AI 银行智能体 —— 订阅模块数据模型升级 v2（MySQL 8.0）
--
--  配套需求：订阅 = 签约代扣（contracted）+ 流水识别（identified），
--            所有订阅与流水联动（有订阅→周期性扣款；取消→不再扣款）。
--
--  执行方式（幂等，可重复跑）：
--    python init_db.py --sql sql/upgrade_subscription_v2.sql
--
--  变更内容：
--    1) subscriptions.source     扩到 VARCHAR(20)，取值
--       contracted=签约代扣 / identified=流水识别（旧值 system、manual 归一到 identified）
--    2) subscriptions.merchant   关联的流水商户名（识别来源 / 扣款引擎用）
--    3) subscriptions.next_due   下次扣款日（签约计算 / 识别推算）
--    4) subscriptions.confirmed  identified 类型是否经用户确认（0/1）
--    5) transactions.remark      流水备注（扣款引擎写"服务名 自动扣费"）
--    6) 存量迁移：source='identified'、confirmed=1（active）、
--       merchant 按服务名映射、next_due=下月初
--    7) 存量库补埋"腾讯视频"3 笔周期流水（不在订阅表里），
--       保证 discover 有"新订阅"可发现，供演示
--
--  ★ 订阅 status 取值扩展：active / cancelled / pending（识别待确认）/ ignored（已忽略）
-- ============================================================================

USE bank_agent;

DROP PROCEDURE IF EXISTS upgrade_subscription_v2;
DELIMITER //
CREATE PROCEDURE upgrade_subscription_v2()
BEGIN
    -- ------------------------------------------------------------------ --
    --  1) subscriptions.source：加列 / 扩宽（16→20），默认值改为 identified
    -- ------------------------------------------------------------------ --
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'subscriptions' AND column_name = 'source'
    ) THEN
        ALTER TABLE subscriptions
            ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'identified'
            COMMENT 'contracted=签约代扣 identified=流水识别' AFTER cycle;
    ELSEIF (
        SELECT character_maximum_length FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'subscriptions' AND column_name = 'source'
    ) < 20 THEN
        ALTER TABLE subscriptions
            MODIFY COLUMN source VARCHAR(20) NOT NULL DEFAULT 'identified'
            COMMENT 'contracted=签约代扣 identified=流水识别';
    END IF;

    -- ------------------------------------------------------------------ --
    --  2) subscriptions.merchant
    -- ------------------------------------------------------------------ --
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'subscriptions' AND column_name = 'merchant'
    ) THEN
        ALTER TABLE subscriptions
            ADD COLUMN merchant VARCHAR(100) NULL
            COMMENT '关联的流水商户名（识别来源/扣款引擎用）' AFTER source;
    END IF;

    -- ------------------------------------------------------------------ --
    --  3) subscriptions.next_due
    -- ------------------------------------------------------------------ --
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'subscriptions' AND column_name = 'next_due'
    ) THEN
        ALTER TABLE subscriptions
            ADD COLUMN next_due DATE NULL
            COMMENT '下次扣款日（签约计算/识别推算）' AFTER merchant;
    END IF;

    -- ------------------------------------------------------------------ --
    --  4) subscriptions.confirmed
    -- ------------------------------------------------------------------ --
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'subscriptions' AND column_name = 'confirmed'
    ) THEN
        ALTER TABLE subscriptions
            ADD COLUMN confirmed TINYINT NOT NULL DEFAULT 0
            COMMENT 'identified 类型是否经用户确认（0=待确认 1=已确认）' AFTER next_due;
    END IF;

    -- ------------------------------------------------------------------ --
    --  5) transactions.remark：扣款引擎的流水备注
    -- ------------------------------------------------------------------ --
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'bank_agent' AND table_name = 'transactions' AND column_name = 'remark'
    ) THEN
        ALTER TABLE transactions
            ADD COLUMN remark VARCHAR(128) NULL COMMENT '备注（订阅自动扣费等）' AFTER type;
    END IF;

    -- ------------------------------------------------------------------ --
    --  6) 扣款引擎热路径索引：user_id + merchant
    -- ------------------------------------------------------------------ --
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.statistics
        WHERE table_schema = 'bank_agent' AND table_name = 'subscriptions'
          AND index_name = 'idx_subs_user_merchant'
    ) THEN
        ALTER TABLE subscriptions ADD KEY idx_subs_user_merchant (user_id, merchant);
    END IF;

    -- ------------------------------------------------------------------ --
    --  7) 存量数据迁移（幂等：条件 UPDATE，重复执行结果一致）
    -- ------------------------------------------------------------------ --

    -- 7a) 旧来源（system=系统初始化 / manual=用户手动录入）统一归一为 identified。
    --     真实银行里订阅只有"签约代扣"和"流水识别"两个来源；
    --     历史上手工录入的记录没有对应扣款协议，按"系统从流水里发现的"处理最自洽。
    UPDATE subscriptions
       SET source = 'identified'
     WHERE source NOT IN ('contracted', 'identified');

    -- 7b) 已在生效中的订阅视为"已确认"（不进 discover 待确认列表）
    UPDATE subscriptions
       SET confirmed = 1
     WHERE status = 'active' AND source = 'identified' AND confirmed = 0;

    -- 7c) merchant 回填：映射到流水中实际埋的商户名（识别引擎/扣款引擎都按
    --     merchant 字段对齐流水），没有映射的服务名原样充当商户名
    UPDATE subscriptions
       SET merchant = CASE service_name
            WHEN '爱奇艺黄金VIP'     THEN '爱奇艺'
            WHEN '腾讯视频VIP'       THEN '腾讯视频VIP'
            WHEN '网易云音乐黑胶VIP' THEN '网易云音乐'
            WHEN 'Kindle Unlimited'  THEN 'Kindle'
            WHEN '百度网盘超级会员'   THEN '百度网盘'
            WHEN 'iCloud+ 200G'      THEN 'iCloud'
            WHEN '视频网站联合会员'   THEN '视频联合会员'
            WHEN '网盘超级会员'       THEN '百度网盘'
            ELSE service_name
        END
     WHERE merchant IS NULL OR merchant = '';

    -- 7d) next_due 回填为下月初（存量订阅本月扣款已由 init.sql 的流水体现）
    UPDATE subscriptions
       SET next_due = DATE_ADD(
               DATE_SUB(CURDATE(), INTERVAL DAYOFMONTH(CURDATE()) - 1 DAY),
               INTERVAL 1 MONTH)
     WHERE next_due IS NULL;
END //
DELIMITER ;

CALL upgrade_subscription_v2();
DROP PROCEDURE IF EXISTS upgrade_subscription_v2;

-- ---------------------------------------------------------------------------
--  8) 存量库补埋"腾讯视频"周期扣款流水（★刻意不写入 subscriptions 表）
--     —— 近 3 个月每月 1 号一笔 30.00 元订阅扣款，供 discover 引擎发现
--     "新订阅"做演示。幂等：只要该商户已有一笔流水就不再插入。
-- ---------------------------------------------------------------------------
INSERT INTO transactions (account_id, `date`, amount, category, merchant, type, remark)
SELECT seed.acct, seed.d, 30.00, '订阅', '腾讯视频', 'expense', '腾讯视频VIP 自动扣费'
FROM (
    SELECT a.id AS acct,
           DATE_SUB(
               DATE_SUB(CURDATE(), INTERVAL DAYOFMONTH(CURDATE()) - 1 DAY),
               INTERVAL n MONTH) AS d
    FROM accounts a
    JOIN (SELECT 0 AS n UNION SELECT 1 UNION SELECT 2) m
    WHERE a.user_id = 1 AND a.id = (SELECT MIN(id) FROM accounts WHERE user_id = 1)
) seed
WHERE EXISTS (SELECT 1 FROM accounts WHERE id = seed.acct AND user_id = 1)
  AND NOT EXISTS (SELECT 1 FROM transactions WHERE merchant = '腾讯视频');

-- ---------------------------------------------------------------------------
--  自检（重复执行结果一致）
-- ---------------------------------------------------------------------------
SELECT '--- subscriptions v2 新增列 ---' AS check_item;
SELECT column_name, column_type, column_default
FROM information_schema.columns
WHERE table_schema = 'bank_agent' AND table_name = 'subscriptions'
  AND column_name IN ('source', 'merchant', 'next_due', 'confirmed')
ORDER BY column_name;

SELECT '--- 订阅来源分布（应只剩 contracted / identified） ---' AS check_item;
SELECT source, status, COUNT(*) AS cnt
FROM subscriptions
GROUP BY source, status
ORDER BY source, status;

SELECT '--- 腾讯视频周期流水（应 3 笔、30.00 元/笔，且不在订阅表） ---' AS check_item;
SELECT t.`date`, t.amount, t.merchant
FROM transactions t
WHERE t.merchant = '腾讯视频'
ORDER BY t.`date`;

SELECT COUNT(*) AS sub_cnt_should_be_zero
FROM subscriptions
WHERE merchant = '腾讯视频';
