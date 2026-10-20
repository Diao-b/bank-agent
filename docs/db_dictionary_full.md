# 数据字典 · bank_agent（全库）

> 由 `scripts/dump_data_dictionary.py` 从真实库导出；字段「说明」即数据库内 COMMENT。
> 共 32 张表。

## accounts — 用户的银行账户（钱存在这里，如活期账户、信用账户）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 账户ID（主键） |
| user_id | bigint | 否 |  | IDX | 所属用户ID（users.id） |
| name | varchar(64) | 否 |  |  | 账户名称（如 活期储蓄账户） |
| card_no | varchar(19) | 是 |  |  | 完整虚拟卡号（16 位，Luhn 校验合法）；本站账户均开在微众银行行别下 |
| account_type | varchar(16) | 否 | debit |  | debit=储蓄账户 credit=信用账户（共享额度，不参与收付） |
| is_primary | tinyint | 否 | 0 |  | 1=主账户（每人唯一，id 最小的账户） |
| balance | decimal(14,2) | 否 | 0.00 |  | 账户余额（元） |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 开户时间 |

## admin_logs — 管理员操作日志（后台每一次改动都留痕，可追溯）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 日志ID（主键） |
| admin_id | bigint | 否 |  |  | 操作管理员（users.id） |
| action | varchar(64) | 否 |  | IDX | 操作类型（user_lock / config_update / cardkey_generate 等） |
| target_type | varchar(32) | 否 |  | IDX | 操作对象类型（user / config / product / risk_rule / card_batch 等） |
| target_id | varchar(64) | 是 |  |  | 操作对象ID |
| detail | text | 是 |  |  | 操作详情（JSON，含 old→new） |
| pending_sync | tinyint(1) | 否 | 0 | IDX | 是否标记「知识文档待同步」：1=待同步 |
| sync_done_at | datetime | 是 |  |  | 知识同步完成时间 |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 记录时间 |

## balance_reconcile — 对账快照（记录上次余额，用来发现钱被人绕过流水手工改过）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| account_id | bigint | 否 |  | PK | 账户ID（主键，accounts.id） |
| user_id | bigint | 否 |  | IDX | 账户所属用户ID |
| snap_balance | decimal(14,2) | 否 | 0.00 |  | 上次对账时的账户余额快照 |
| snap_income | decimal(14,2) | 否 | 0.00 |  | 上次对账时的累计收入流水 |
| snap_expense | decimal(14,2) | 否 | 0.00 |  | 上次对账时的累计支出流水 |
| updated_at | datetime | 否 | CURRENT_TIMESTAMP |  | 快照更新时间 |

## bank_directory — 支持的银行名单（跨行转账选银行时用）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | int | 否 |  | PK | 银行ID（主键） |
| bank_name | varchar(64) | 否 |  |  | 银行名称 |
| bank_code | varchar(16) | 否 |  | UQ | 联行号演示用，也是 BIN→银行的映射键 |
| is_local | tinyint | 否 | 0 |  | 1=本行（微众银行），0=他行 |

## card_key_batches — 卡密批次汇总（一次生成多少张、每张多大面额）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| batch_no | varchar(32) | 否 |  | PK | 批次号（主键） |
| amount | decimal(14,2) | 否 |  |  | 本批次统一面额（元） |
| count | int | 否 |  |  | 本批次卡密数量 |
| created_by | bigint | 是 |  |  | 生成人（users.id，super_admin） |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 生成时间 |
| remark | varchar(255) | 是 |  |  | 批次备注 |

## card_keys — 卡密明细（用户用它充值；只存加密后的码，不存明文）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 卡密ID（主键） |
| code_hash | char(64) | 否 |  | UQ | 卡密码哈希 SHA-256(pepper+code)；★绝不存明文 |
| code_prefix | varchar(8) | 否 |  |  | 卡密明文前4位（仅供人工核对，非明文全量） |
| amount | decimal(14,2) | 否 |  |  | 面额（元）；生成即冻结，不可修改 |
| batch_no | varchar(32) | 否 |  | IDX | 所属批次号（card_key_batches.batch_no） |
| status | enum('unused','used','void','expired') | 否 | unused | IDX | 状态：unused=未使用/used=已使用/void=已作废/expired=已过期 |
| created_by | bigint | 是 |  |  | 生成人（users.id，管理员） |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 生成时间 |
| expires_at | datetime | 是 |  |  | 过期时间（NULL=永不过期） |
| used_by | bigint | 是 |  | IDX | 核销人（users.id，用户本人） |
| used_at | datetime | 是 |  |  | 核销时间 |
| used_account_id | bigint | 是 |  |  | 入账账户（accounts.id） |
| remark | varchar(255) | 是 |  |  | 备注 |

## card_redeem_throttle — 卡密核销限流（限制同一人/IP 每分钟能试几次，防爆破）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| user_id | bigint | 否 |  | PK | 用户ID（限流维度之一） |
| ip | varchar(64) | 否 |  | PK | 客户端IP（限流维度之一） |
| bucket_min | datetime | 否 |  | PK | 分钟时间桶（按分钟聚合计数） |
| cnt | int | 否 | 1 |  | 本分钟（用户+IP）内的核销尝试次数 |

## cards — 用户名下的银行卡

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 卡片ID（主键） |
| user_id | bigint | 否 |  | IDX | 所属用户ID（users.id） |
| account_id | bigint | 是 |  | IDX | 关联账户（申请新卡时挂主账户） |
| card_no | varchar(32) | 否 |  |  | 银行卡号（掩码 / 尾号展示用） |
| card_no_full | varchar(19) | 是 |  |  | 完整卡号（Luhn 合法）；仅申请人可见一次，列表仅返回尾号 |
| card_name | varchar(64) | 否 |  |  | 卡面展示名，如 招商储蓄卡 |
| card_type | varchar(16) | 否 | debit |  | debit=储蓄卡 credit=信用卡 |
| tail | varchar(8) | 否 |  |  | 卡号后四位 |
| bank | varchar(64) | 否 |  |  | 所属银行 |
| credit_limit | decimal(14,2) | 否 | 0.00 |  | 信用卡总额度；储蓄卡为 0 |
| available_limit | decimal(14,2) | 否 | 0.00 |  | 信用卡可用额度；储蓄卡为 0 |
| status | varchar(16) | 否 | normal |  | normal/frozen/lost |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 开卡时间 |
| deleted_at | datetime | 是 |  |  | 软删除时间（status=deleted） |

## chat_messages — 聊天消息（用户与 AI 的每一条对话内容）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 消息ID（主键） |
| conversation_id | varchar(64) | 否 |  | IDX | 会话ID（conversations.conversation_id） |
| role | varchar(16) | 否 |  |  | user/assistant/system |
| content | text | 否 |  |  | 消息内容 |
| node_trace | json | 否 | _utf8mb4\'[]\' |  | 管道/节点轨迹，普通消息为空数组 |
| kind | varchar(20) | 否 | chat |  | chat=普通 / greeting=问候语 |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 创建时间 |
| seq | int | 否 | 0 |  | 会话内消息序号 |
| user_id | bigint | 是 |  | IDX | 所属用户ID（users.id） |

## conversations — 会话列表（用户和 AI 的每个聊天窗口）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 自增主键 |
| conversation_id | varchar(64) | 否 |  | UQ | 前端 thread_id，如 web-9k3k4e |
| user_id | bigint | 否 |  | IDX | 所属用户ID（users.id） |
| title | varchar(128) | 否 | 新会话 |  | 首条用户消息截取，≤20 字 |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 创建时间 |
| updated_at | datetime | 否 | CURRENT_TIMESTAMP |  | 最后更新时间 |

## external_accounts — 他行账户登记表（跨行转账需要清算的对方账户）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 外部（他行）账户ID（主键） |
| card_no | varchar(24) | 否 |  | UQ | 他行卡号（Luhn 合法） |
| real_name | varchar(64) | 是 |  |  | 户名 |
| bank_id | int | 是 |  |  | 开户行 bank_directory.id |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 创建时间 |

## holdings — 用户的理财持仓（买了哪些产品、多少钱）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 持仓ID（主键） |
| user_id | bigint | 否 |  | IDX | 持仓用户ID（users.id） |
| product_id | bigint | 否 |  | IDX | 理财产品ID（products.id） |
| amount | decimal(14,2) | 否 |  |  | 持仓金额（买入本金） |
| buy_date | date | 否 |  |  | 买入日期 |
| status | varchar(16) | 否 | holding |  | holding=持有中 redeemed=已赎回 |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 创建时间 |

## idempotency_records — 防重复提交记录（同一个请求只会真正执行一次）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 记录ID（主键） |
| idem_key | varchar(64) | 否 |  |  | 客户端幂等键（Idempotency-Key 头） |
| user_id | bigint | 否 |  | IDX | 归属用户（★ 按用户隔离，A 的 key 不影响 B） |
| action | varchar(64) | 否 |  |  | 动作名（同响应协议 action 字段） |
| request_hash | char(64) | 否 |  |  | sha256(method+path+body)，防同 key 打不同请求 |
| response_json | json | 是 |  |  | 终态回执（仅成功写入；NULL=处理中） |
| status | varchar(16) | 否 | pending |  | pending / done |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 创建时间 |
| expires_at | datetime | 否 |  | IDX | 过期时间（过期后可重新执行） |

## knowledge_chunks — 知识库切片（把说明文档切小段并转向量，供 AI 检索作答）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 切片ID（主键） |
| doc_name | varchar(128) | 否 |  | IDX | 来源文档名（docs/knowledge/*.md） |
| heading | varchar(255) | 是 |  |  | 所属标题 |
| content | text | 否 |  |  | 切片正文 |
| chunk_index | int | 否 | 0 |  | 文档内切片序号（从 0 起） |
| embedding | json | 是 |  |  | 向量嵌入（JSON 数组） |
| embed_model | varchar(64) | 是 |  |  | 生成 embedding 的模型名 |
| updated_at | datetime | 否 | CURRENT_TIMESTAMP |  | 更新时间 |

## login_attempts — 登录失败次数与锁定，防止别人暴力猜密码

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| username | varchar(64) | 否 |  | PK | 登录名（大小写按业务判定，这里原样存） |
| fail_count | int | 否 | 0 |  | 连续失败次数 |
| first_fail_at | datetime | 是 |  |  | 首次失败时间 |
| locked_until | datetime | 是 |  |  | 临时锁定截止（NULL=未锁） |
| updated_at | datetime | 否 | CURRENT_TIMESTAMP |  | 最后更新时间 |

## notifications — 站内通知 / 收件箱（系统给用户发的消息）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 通知ID（主键） |
| user_id | bigint | 否 |  | IDX | 接收用户ID（users.id） |
| type | varchar(32) | 否 | notice |  | verify_code / transfer_receipt / notice |
| title | varchar(100) | 否 |  |  | 通知标题 |
| content | text | 是 |  |  | 通知正文 |
| meta | json | 是 |  |  | 结构化数据：验证码、流水号、用途等 |
| is_read | tinyint | 否 | 0 |  | 是否已读：1=已读，0=未读 |
| expires_at | datetime | 是 |  |  | 验证码类过期时间；普通通知为 NULL |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 创建时间 |

## op_tickets — 一次性操作凭证（重要操作前先发一张只能用一次的门票）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 票据ID（主键） |
| user_id | bigint | 否 |  | IDX | 持有人（users.id） |
| scene | varchar(48) | 否 |  |  | 使用场景（如 internal_transfer / wealth_buy） |
| token_hash | char(64) | 否 |  | UQ | 票据哈希（SHA-256，不存明文） |
| status | varchar(16) | 否 | active |  | 状态：active=有效 / used=已用 / expired=已过期 |
| expires_at | datetime | 否 |  |  | 过期时间 |
| consumed_at | datetime | 是 |  |  | 使用时间 |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 创建时间 |

## operation_logs — 用户操作日志（谁在什么时候做了什么业务动作）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 日志ID（主键） |
| user_id | bigint | 否 |  | IDX | 操作用户ID（users.id） |
| action | varchar(64) | 否 |  |  | 操作类型（transfer / verify_send / ...） |
| detail | text | 是 |  |  | 操作详情（JSON） |
| result | varchar(32) | 否 |  |  | success/failed/not_implemented |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 记录时间 |

## payees — 用户保存的收款人名单（转账常用对象）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 收款人ID（主键） |
| user_id | bigint | 否 |  | IDX | 归属用户ID（users.id） |
| nickname | varchar(64) | 否 |  |  | 收款人昵称 / 姓名 |
| real_name | varchar(64) | 是 |  |  | 对方真实姓名（站内来自用户表，跨行来自填单） |
| bank | varchar(64) | 否 |  |  | 收款银行 |
| bank_id | int | 是 |  |  | bank_directory 外键（开户行） |
| card_no | varchar(19) | 是 |  |  | 完整卡号（Luhn 合法）；转账标的物，最终以账号为准 |
| account_tail | varchar(8) | 否 |  |  | 收款账号尾号 |
| linked_user_id | bigint | 是 |  | IDX | 该收款人对应的站内用户 ID（NULL=行外收款人） |
| account_id | bigint | 是 |  | IDX | 站内收款对应的目标账户（跨行收款人为 NULL） |
| is_frequent | tinyint | 否 | 0 |  | 1=常用收款人 |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 创建时间 |

## paypass_passed — 支付密码本次已通过的临时标记（对完一次密码短期内不用再对）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| user_id | bigint | 否 |  | PK | 用户 id |
| expires_at | datetime | 否 |  |  | 通过标记过期时间（一次性消费） |

## pending_settlements — 跨行转账在途清算记录（钱还在路上的那部分）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 在途清算ID（主键） |
| txn_id | bigint | 否 |  | IDX | 原转账支出流水 transactions.id |
| user_id | bigint | 否 |  | IDX | 付款方用户 ID |
| amount | decimal(14,2) | 否 |  |  | 在途金额 |
| target_card_no | varchar(24) | 是 |  | IDX | 收款方他行卡号 |
| target_bank_id | int | 是 |  |  | 收款方开户行 bank_directory.id |
| serial_no | varchar(40) | 是 |  |  | 原转账流水号 |
| expected_at | datetime | 否 |  |  | 预计清算完成时间（创建+5分钟，演示压缩） |
| status | varchar(16) | 否 | pending | IDX | pending=在途 done=已处理 |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 创建时间 |
| fee | decimal(12,2) | 否 | 0.00 |  | 本笔已收跨行手续费（退票时随本金一并退回） |

## products — 理财产品目录（有哪些产品可买）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 理财产品ID（主键） |
| name | varchar(64) | 否 |  |  | 产品名称 |
| term | varchar(32) | 否 |  |  | 期限描述，如 90天 |
| risk_level | tinyint | 否 |  |  | 1=低风险 2=中风险 3=中高风险 |
| annual_rate | decimal(5,2) | 否 |  |  | 年化收益率(%) |
| min_amount | decimal(14,2) | 否 | 0.00 |  | 起购金额（元） |
| status | varchar(16) | 否 | on |  | 状态：on=上架 / off=下架 |

## risk_event_views — 风控详情查看记录（谁看过告警的完整内容，留痕）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 查看记录ID（主键） |
| event_id | bigint | 否 |  | IDX | 被查看的告警事件ID（risk_events.id） |
| admin_id | bigint | 否 |  | IDX | 查看人（users.id，管理员） |
| viewed_at | datetime | 否 | CURRENT_TIMESTAMP |  | 查看时间 |
| reason | varchar(255) | 否 |  |  | 展开完整明细的理由（强制填写，留痕） |
| fields_disclosed | varchar(255) | 是 |  |  | 本次披露的字段清单 |

## risk_events — 风控告警记录（如可疑转账，进入后台待处理队列）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 事件ID（主键） |
| user_id | bigint | 否 |  | IDX | 涉及用户ID（users.id） |
| rule_code | varchar(32) | 否 |  | IDX | 命中规则编码（risk_rules.rule_code） |
| severity | varchar(16) | 否 | medium |  | 告警级别：low/medium/high/critical |
| status | varchar(16) | 否 | open | IDX | 处置状态：open=待处理/confirmed=已确认/dismissed=已忽略/closed=已关闭 |
| hit_at | datetime | 否 | CURRENT_TIMESTAMP |  | 命中时间 |
| payload | json | 是 |  |  | 最小字段载荷(JSON)：金额分桶/收款人掩码/窗口/计数等；★不含敏感原文 |
| linked_txn_ids | json | 是 |  |  | 命中的流水ID列表（JSON数组，transactions.id） |
| handled_by | bigint | 是 |  |  | 处置人（users.id，管理员） |
| handled_at | datetime | 是 |  |  | 处置时间 |
| note | varchar(255) | 是 |  |  | 处置备注 |

## risk_rules — 风控规则设置（什么情况算可疑、要多严）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| rule_code | varchar(32) | 否 |  | PK | 规则编码（R-DUP/R-FREQ/R-NEWLARGE/R-AMTOUT/R-SCATTER/R-DRAIN/R-NIGHT/R-CHARGE） |
| name | varchar(64) | 否 |  |  | 规则名称（中文） |
| enabled | tinyint(1) | 否 | 1 |  | 是否启用：1=启用，0=停用（停用后不再产生新事件） |
| threshold | json | 是 |  |  | 规则阈值（JSON），如 {"window_min":30,"amount_same":1} |
| severity | varchar(16) | 否 | medium |  | 告警级别：low/medium/high/critical |
| user_facing | tinyint(1) | 否 | 0 |  | 是否走通道A：1=命中时实时提醒用户本人，0=仅后台告警 |
| description | varchar(255) | 否 |  |  | 规则说明（触发条件的人类可读描述） |

## schema_migrations — 数据库升级台账（记录每个改动脚本有没有执行过）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| filename | varchar(128) | 否 |  | PK | 迁移文件名（sql/ 下 upgrade_*.sql） |
| checksum | char(64) | 否 |  |  | 文件内容 sha256（漂移检测） |
| statements | int | 否 | 0 |  | 脚本语句数 |
| skipped | int | 否 | 0 |  | 幂等跳过（对象已存在）的语句数 |
| applied_at | datetime | 否 | CURRENT_TIMESTAMP |  | 最近一次应用时间 |

## subscriptions — 用户的自动扣费订阅（如会员每月扣款）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 订阅ID（主键） |
| user_id | bigint | 否 |  | IDX | 订阅用户ID（users.id） |
| service_name | varchar(64) | 否 |  |  | 服务名称（如 视频会员） |
| fee | decimal(10,2) | 否 |  |  | 每期费用（元） |
| cycle | varchar(16) | 否 | 月付 |  | 扣费周期（月付 / 年付） |
| last_used | date | 否 |  |  | 最近一次使用 / 扣费日期 |
| status | varchar(16) | 否 | active |  | active=生效中 cancelled=已取消 |
| cancelled_at | datetime | 是 |  |  | 取消时间（软删除，记录保留供前端展示删除线） |
| source | varchar(20) | 否 | identified |  | contracted=签约代扣 identified=流水识别 |
| merchant | varchar(100) | 是 |  |  | 关联的流水商户名（识别来源/扣款引擎用） |
| next_due | date | 是 |  |  | 下次扣款日（签约计算/识别推算） |
| confirmed | tinyint | 否 | 0 |  | identified 类型是否经用户确认（0=待确认 1=已确认） |

## system_config — 系统参数设置（可后台调整的业务数值，如限额、费率、开关）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| key | varchar(64) | 否 |  | PK | 参数键（rules.RULE_SPECS 的 key） |
| value | varchar(128) | 否 |  |  | 参数当前值 |
| label | varchar(128) | 否 |  |  | 参数中文名 |
| value_type | varchar(16) | 否 | int |  | 值类型：int / decimal |
| min_value | decimal(18,2) | 是 |  |  | 允许最小值 |
| max_value | decimal(18,2) | 是 |  |  | 允许最大值 |
| unit | varchar(24) | 是 |  |  | 单位（元 / 次 / 秒 / 比例 / 开关 等） |
| affects_knowledge | tinyint(1) | 否 | 0 |  | 是否影响 RAG 知识文档：1=改后需同步 |
| docs_ref | varchar(255) | 是 |  |  | 需同步的知识文档 |
| updated_by | bigint | 是 |  |  | 最后修改人（users.id） |
| updated_at | datetime | 否 | CURRENT_TIMESTAMP |  | 最后修改时间 |

## transactions — 交易流水（每一笔钱的进出明细，最核心的一张表）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 流水ID（主键） |
| account_id | bigint | 否 |  | IDX | 所属账户ID（accounts.id） |
| date | date | 否 |  | IDX | 交易日期 |
| amount | decimal(12,2) | 否 |  |  | 交易金额（元） |
| category | varchar(32) | 否 |  |  | 餐饮/购物/交通/娱乐/订阅/转账/收入 |
| merchant | varchar(128) | 否 |  |  | 交易对手 / 摘要（转账时为「收款人（银行 尾号）」派生串） |
| type | varchar(16) | 否 | expense |  | expense=支出 income=收入 |
| remark | varchar(128) | 是 |  |  | 备注（订阅自动扣费等） |
| settle_status | varchar(20) | 是 |  | IDX | 跨行清算状态：NULL=无需清算 pending=在途 settled=已到账 returned=已退票 |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 创建时间 |
| payee_id | bigint | 是 |  | IDX | 收款人ID（风控结构化匹配，D1修复；空=非转账/未解析） |

## transfer_daily_quota — 转账每日额度（记录某天已用多少、还剩多少）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 额度记录ID（主键） |
| user_id | bigint | 否 |  | IDX | 用户ID（users.id） |
| quota_date | date | 否 |  | IDX | 额度所属日期 |
| used_amount | decimal(18,2) | 否 | 0.00 |  | 当日已用转账额度（元） |

## users — 用户账号（注册的人都在这里）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 用户ID（主键） |
| name | varchar(64) | 否 |  |  | 姓名 / 展示名 |
| created_at | datetime | 否 | CURRENT_TIMESTAMP |  | 注册时间 |
| real_name | varchar(50) | 是 |  |  | 实名认证姓名 |
| gender | varchar(10) | 是 |  |  | 性别 |
| title_prefix | varchar(20) | 是 |  |  | 称呼前缀（如 先生 / 女士） |
| phone_tail | varchar(20) | 是 |  |  | 手机号尾号（脱敏存储） |
| occupation | varchar(50) | 是 |  |  | 职业 |
| preferences | json | 是 |  |  | 偏好设置（JSON，如风险偏好） |
| updated_at | datetime | 是 | CURRENT_TIMESTAMP |  | 资料更新时间 |
| username | varchar(64) | 是 |  | UQ | 登录账号 |
| password_hash | varchar(255) | 是 |  |  | pbkdf2-sha256 哈希 |
| role | varchar(16) | 否 | user |  | user/admin/risk_ops/super_admin |
| pay_password_hash | varchar(128) | 是 |  |  | 支付密码哈希（sha256+盐） |
| pay_pwd_attempts | int | 否 | 0 |  | 连续错误次数 |
| pay_pwd_locked_until | datetime | 是 |  |  | 锁定截止时间（当日 23:59:59） |
| phone | varchar(20) | 是 |  |  | 完整手机号，仅后端持有，对外只回脱敏号 |
| phone_pending | varchar(20) | 是 |  |  | 绑定中暂存的待绑定手机号 |
| realname_verified | tinyint | 否 | 0 |  | 实名认证是否已完成（仅 ④ 核验通过置 1；与 ③ 基本信息真实姓名无关） |
| locked | tinyint(1) | 否 | 0 |  | 是否锁定：1=锁定（禁止登录），0=正常 |

## verification_codes — 验证码记录（发了什么码、有没有验证通过、错了几次）

| 字段 | 类型 | 可空 | 默认 | 键 | 说明 |
| --- | --- | --- | --- | --- | --- |
| id | bigint | 否 |  | PK | 验证码记录ID（主键） |
| user_id | bigint | 否 |  | IDX | 用户ID（users.id） |
| scene | varchar(32) | 否 | transfer |  | transfer / buy_product / realname 等 |
| code_hash | varchar(64) | 否 |  |  | sha256(plain) 校验用 |
| status | varchar(16) | 否 | unused |  | unused 未使用 / verified 已验证 / expired 已过期 / void 作废 |
| attempts | int | 否 | 0 |  | 已尝试核验次数 |
| notification_id | bigint | 是 |  |  | 收件箱消息 id（双写同事务的证据） |
| expires_at | datetime | 否 |  |  | 过期时间 |
| verified_at | datetime | 是 |  |  | 核验通过时间 |
| created_at | datetime | 否 | CURRENT_TIMESTAMP | IDX | 创建时间 |
