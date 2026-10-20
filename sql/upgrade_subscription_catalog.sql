-- ============================================================================
--  upgrade_subscription_catalog.sql —— 订阅「服务商 + 套餐目录」· 数据层迁移
--
--  问题（2026-10-14 用户反馈：订阅应像理财 products 一样由后台配置、用户只选，
--        金额不该用户自填、服务商不该手填导致与流水 merchant 对不齐）：
--    旧模型 subscriptions 是唯一表，用户自填 service_name/fee/cycle/merchant，
--    金额用户自定、merchant 手填 → 扣款引擎/discover 按 merchant 文本对齐流水时
--    极易对不上（填"爱奇艺会员"就对不上真实的"爱奇艺"）。
--
--  本脚本做三件事：
--    1) 新增 merchants（服务商目录，后台配置上架）
--    2) 新增 subscription_plans（套餐目录，绑定 merchant、带固定 fee/cycle，
--       用户不可改金额）
--    3) subscriptions 加 merchant_id / plan_id（catalog 模式签约来源；存量 NULL）
--
--  ★ 锁价语义：签约时从套餐拷贝 fee/cycle/merchant 到 subscriptions 原字段作快照，
--    套餐后期改价不影响已签约用户（与理财申购不同）。
--  ★ 种子对齐真实流水商户名（爱奇艺/腾讯视频/网易云音乐/美团外卖会员），
--    保证扣款引擎按 merchant 文本对齐流水不破。
--  ★ 幂等：表用 IF NOT EXISTS；种子用 INSERT IGNORE + 唯一键，重复跑安全。
--
--  用法：python init_db.py --upgrade-only
-- ============================================================================

USE bank_agent;

CREATE TABLE IF NOT EXISTS merchants (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    name        VARCHAR(64)   NOT NULL                COMMENT '服务商名（须与真实流水 merchant 一致）',
    category    VARCHAR(32)   NULL                    COMMENT '类目：视频/音乐/生活/通讯/工具…',
    logo_url    VARCHAR(255)  NULL,
    description VARCHAR(255)  NULL,
    status      VARCHAR(16)   NOT NULL DEFAULT 'active' COMMENT 'active=上架 displayed / offline=下架',
    sort_order  INT           NOT NULL DEFAULT 0,
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_merchant_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订阅服务商目录（后台配置上架）';

CREATE TABLE IF NOT EXISTS subscription_plans (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    merchant_id BIGINT        NOT NULL                COMMENT '所属服务商',
    name        VARCHAR(64)   NOT NULL                COMMENT '套餐名，如「连续包月」',
    fee         DECIMAL(10,2) NOT NULL                COMMENT '套餐固定费用（用户不可改）',
    cycle       VARCHAR(16)   NOT NULL DEFAULT '月付' COMMENT '月付/季付/年付',
    description VARCHAR(255)  NULL,
    status      VARCHAR(16)   NOT NULL DEFAULT 'active' COMMENT 'active=可签约 / offline=下架',
    sort_order  INT           NOT NULL DEFAULT 0,
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_plan (merchant_id, name),
    KEY idx_plan_merchant (merchant_id, status),
    CONSTRAINT fk_plan_merchant FOREIGN KEY (merchant_id) REFERENCES merchants(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订阅套餐目录（后台配置，金额用户不可改）';

ALTER TABLE subscriptions
    ADD COLUMN merchant_id BIGINT NULL COMMENT '签约来源服务商（catalog 模式；存量 NULL）',
    ADD COLUMN plan_id     BIGINT NULL COMMENT '签约来源套餐（catalog 模式；存量 NULL）',
    ADD KEY idx_subs_plan (plan_id);

-- ---- 种子：对齐真实流水商户名（init.sql seed_txn 里的 merchant） ----
INSERT IGNORE INTO merchants (name, category, description, sort_order) VALUES
    ('爱奇艺',       '视频', '爱奇艺视频会员', 1),
    ('腾讯视频',     '视频', '腾讯视频会员',   2),
    ('网易云音乐',   '音乐', '网易云音乐会员', 3),
    ('美团外卖会员', '生活', '美团外卖会员',   4);

INSERT IGNORE INTO subscription_plans (merchant_id, name, fee, cycle, description, sort_order)
VALUES
    ((SELECT id FROM merchants WHERE name='爱奇艺'),       '连续包月', 25.00,  '月付', '爱奇艺黄金VIP连续包月',   1),
    ((SELECT id FROM merchants WHERE name='爱奇艺'),       '连续包年', 248.00, '年付', '爱奇艺黄金VIP连续包年',   2),
    ((SELECT id FROM merchants WHERE name='腾讯视频'),     '连续包月', 30.00,  '月付', '腾讯视频超级影视VIP连续包月', 1),
    ((SELECT id FROM merchants WHERE name='腾讯视频'),     '连续包年', 288.00, '年付', '腾讯视频超级影视VIP连续包年', 2),
    ((SELECT id FROM merchants WHERE name='网易云音乐'),   '连续包月', 15.00,  '月付', '网易云音乐黑胶VIP连续包月', 1),
    ((SELECT id FROM merchants WHERE name='网易云音乐'),   '连续包年', 158.00, '年付', '网易云音乐黑胶VIP连续包年', 2),
    ((SELECT id FROM merchants WHERE name='美团外卖会员'), '连续包月', 15.00,  '月付', '美团外卖会员连续包月',     1);
