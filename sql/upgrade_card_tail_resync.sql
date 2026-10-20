-- 卡号一致性回填（2026-09-25）
--
-- 背景：多银行开卡改造（2026-09-24）后，新卡的 cards.card_no / cards.tail 与
--   完整卡号 cards.card_no_full 同源；但改造前的存量卡（种子「微众储蓄卡」等）
--   只维护了旧格式的 cards.card_no（如 '6225****5025'）与 cards.tail（'5025'），
--   卡号在账户侧被重新生成后未同步 → 卡片管理页同一张卡出现两个末四位
--   （页面显示「6102 **** **** 1121」，下面却写「尾号 5025」），注销确认弹层
--   也会报错尾号。
--
-- 口径：以「卡自身完整号」为唯一真值（cards.card_no_full；缺失时回落到
--   关联账户的 accounts.card_no —— 开新账户/信用卡场景卡与账户同号）。
--   本迁移只回填 cards.tail 与 cards.card_no 两列，不动卡、不动账户、不动余额。
--
-- 幂等：重复执行结果不变；无条件出现在「卡号为空」的极端行（COALESCE 兜底自身）。
UPDATE cards c
LEFT JOIN accounts a ON a.id = c.account_id
SET c.tail = RIGHT(COALESCE(NULLIF(c.card_no_full, ''), NULLIF(a.card_no, ''), c.card_no), 4),
    c.card_no = CONCAT(
        LEFT(COALESCE(NULLIF(c.card_no_full, ''), NULLIF(a.card_no, ''), c.card_no), 4),
        ' **** **** ',
        RIGHT(COALESCE(NULLIF(c.card_no_full, ''), NULLIF(a.card_no, ''), c.card_no), 4)
    )
WHERE COALESCE(NULLIF(c.card_no_full, ''), NULLIF(a.card_no, '')) IS NOT NULL;
