# 开发规范 · CONTRIBUTING.md

> 小微 AI 银行助手（bank-agent）。本规范约束**后续所有**修改与新增（人 / AI 同等适用）。
> 事实来源：`audit_coupling.md`（体检 + 重构）与当前代码。**动代码前先查本文件。**

## 一、架构铁律（6 条，逐条必须遵守）

1. **三层上限**：`路由 → 域文件 → 数据访问`（`mock_bank/db.py`）。简单逻辑直进路由；禁止"纯转发层"。
   跨域只准引用 `utils` 纯函数与 `db` 层，域模块之间不得互相 import 业务实现。*理由：层一多、网一密，改一处要动多处（`card_service` 薄包装即此，组 K 已清）。*
2. **一域一文件**：见 §三 域清单。禁止新拆 `xxx_service / xxx_manager / xxx_helper`。*理由：一域一文件 = 改一处只动一处。*
3. **入口唯一**：同类事只走一个入口（§三 末表）。例如任何收款人解析**必须走** `utils/transfer.resolve_payee`。*理由：两个入口 = 两份规则，迟早不一致。*
4. **修改半径 ≤2 文件**：一次改动触及 >2 个源文件，**先报告**再动。*理由：超半径通常说明改错了层。*
5. **新建文件须说明"为什么不是现有域的延伸"**：答不出就并入现有域文件。*理由：防域清单膨胀成新的散乱。*
6. **新抽域用"惰性 import"**（组 T 先例）：域文件顶层**不** import `mock_bank.main`，共享 helper 在**函数内**惰性 import；路由用 `router = APIRouter()`，main 仅 `include_router`。*理由：顶层 import main 会成环，逼出 `__getattr__` 兼容壳 + "include 必须放末尾"的历史包袱（组 S）。*

## 二、变更规则对照表（按改动类型）

| 改动类型 | 允许（这样做） | 禁止（别这样） |
|---|---|---|
| **新加接口** | 加在**所属域文件**的 `router`（`utils/card.py` / `subscription.py` / `wealth.py`）；读端点加在 `mock_bank/main.py` 对应分组 | 为单个接口新建文件；在 `main.py` 里堆业务逻辑 |
| **新业务规则** | 数值进 `rules.py`；执行逻辑进对应域文件（转账 → `utils/transfer.py`） | 阈值/额度硬编码进路由或 agent |
| **新业务拒绝分支** | 失败统一走 `fail(message, code=ERR.XXX, action="<动作名>")`；错误码先到 `utils/response_codes.py` 的 `ERR` 加常量（值 == 常量名），`action` 由响应包裹层按路由兜底注入 | 只返回中文文案不带 `code`；在调用点写裸字符串 code（散落即不一致） |
| **新写端点** | 只写业务；统一响应协议（action/code/message）与**幂等去重**由 `ResponseEnvelopeMiddleware` 在包裹层自动生效，无需在端点里重复实现 | 在端点内部自己拼装协议字段 / 自己做去重（会与包裹层打架） |
| **新数据表** | 写 `sql/upgrade_<feature>.sql`（幂等），**并登记进 `init_db.UPGRADE_ORDER`**；`init_db.py` 按版本台账 `schema_migrations` 只应用未应用的迁移；**外键优先内联在 `CREATE TABLE` 里** | 改 `sql/init.sql` 既有表结构；手改运行库；在多个迁移文件之间传递 MySQL 用户变量 |
| **新 Agent 意图** | 写操作加 `agent/write_flow.ACTION_SPECS` 条目 + 节点/路由；查询扩 `agent/tools.py` 本地工具 | 绕过 `write_flow` 直接调写端点 |
| **新外部服务** | 传输层放 `utils/<svc>_client.py`、转换层放 `utils/<svc>_bridge.py`（照 `mcp_client` + `mcp_bridge`） | 把外部调用内联进路由或域文件 |
| **UI 改动** | 复用 `web/index.html` 既有组件（`Amount` / `PasswordCard` / `VerifyPanel` / `ConfirmCard` / `PrivacyProvider`…）；状态用文字字符；**图标一律内联 SVG 自绘**（如语音按钮 `MicGlyph`） | 引入 icon 库 / emoji / 外部图标字体；改完不跑 `node scripts/_check_babel.js` |

> ★★ **迁移脚本执行模型（`sql/upgrade_*.sql`）**：`init_db` 用 `split_statements` 拆语句后，
> **一个迁移文件复用同一个 DB 连接**，因此 `SET @x=...; PREPARE ... FROM @x; EXECUTE ...`
> 现在可以正常工作；但单条语句失败只会被记录并继续，不能把修复依赖埋在“看起来没报错”里。
> 新迁移还必须登记进 `init_db.UPGRADE_ORDER`：升级文件之间存在依赖，不能依赖字母序；
> `upgrade_delete_cascade.sql` 永远放在最后。
> 新增表仍优先把 `CONSTRAINT ... FOREIGN KEY` 内联进 `CREATE TABLE ... IF NOT EXISTS`，
> 这样不强依赖多语句会话状态，迁移更直观。
>
> ★ **P1.1 迁移版本化**：`init_db.py` 维护台账 `schema_migrations`（文件名 + sha256 + 语句/跳过数 + 时间）。
> - `python init_db.py`：**含 `DROP TABLE` 重建**（跑 `init.sql`），**仅限全新库**；随后按序应用全部迁移并登记。
> - `python init_db.py --baseline`：把现有迁移登记为“已应用”（**只登记、不执行 SQL**）——**存量库纳入版本管理的唯一正确入口**。
> - `python init_db.py --upgrade-only`：只应用**未应用**的迁移；若台账为空且库内已有数据会**拒绝执行**
>   （因部分迁移并非数据幂等，如 `upgrade_products_fix.sql` 直接 `DELETE FROM products` 后重插）。
> - `python init_db.py --status` / `python scripts/check_migrations.py`：查看台账 / 只读体检（漂移·缺失·未登记·未应用）。
> - ★ 迁移一经登记即视为“已应用”；**改了内容不会自动重跑**（告警「迁移变更·跳过」），需先删台账该行或重跑 `--baseline`。

> **新功能自查（示例：水电费代缴）**：① 放哪文件 —— 新域 `utils/billpay.py`（若由转账扣款则**必须**走 `resolve_payee`）；② 进哪清单 —— §三 域清单 + `README` §5 端点 + `sql/upgrade_billpay.sql`（有新表时）；③ 走哪入口 —— 写操作加 `write_flow.ACTION_SPECS`「billpay」+ 节点；④ 同步什么 —— §五（阈值进 `rules.py`、`docs/knowledge` 数字、验收清单）。

## 三、域清单表（户口本 · 新功能先对号入座）

| 文件（`utils/`） | 职责边界 | 关键入口函数 |
|---|---|---|
| `transfer.py` | 转账域：收款人解析 + 执行 | `resolve_payee` · `execute_transfer` |
| `card.py` | 卡域：卡号校验/生成 + 卡片-账户服务 + 卡路由 | `apply_card` · `validate_card` · `list_cards` |
| `subscription.py` | 订阅域：周期识别 + 状态机 + 订阅路由 | `discover_candidates` · `contract_subscription` |
| `wealth.py` | 理财域：产品/持仓/买入/赎回 + 路由 | `buy_product` · `redeem` |
| `settlement_engine.py` | 跨行清算 + 订阅扣款引擎 | `process_due_settlements` · `run_due_deductions` · `process_housekeeping` |
| `scheduler.py` | 后台调度（P1.2）：独立周期任务驱动上述引擎，替代纯「读接口惰性触发」 | `start` · `stop` · `run_once` · `status` |
| `op_auth.py` | 写操作一次性票据（P0.2）：签发 / 消费 / FastAPI 依赖 | `issue_op_token` · `consume_op_token` · `RequireOpToken` |
| `verify_codes.py` | 短信验证码（**作废码**语义） | `issue_code` · `check_code` |
| `paypass.py` | 支付密码（hash / 锁定 / 通过标记） | `verify` · `has_passed` · `set_password` |
| `inbox.py` | 站内通知 / 收件箱 | `push_notification` · `mark_read` |
| `security.py` | 认证 guard + 资源归属校验（**FastAPI-free**） | `resolve_user_id` · `ensure_ownership` |
| `deps.py` | FastAPI 依赖：当前用户 | `current_uid` |
| `event_bus.py` | SSE 事件总线（进程内、线程安全） | `emit` · `subscribe` |
| `mcp_client.py` | 百炼 MCP 单例传输 | `ensure_connected_sync` |
| `mcp_bridge.py` | MCP ↔ OpenAI function-calling 转换 + 事件 | `get_openai_tools` · `call_mcp_tool_sync` |
| `user_provision.py` | 用户开户 / 种子数据 | `provision_new_user` · `generate_transactions` |
| `card_validator.py` · `card_service.py` | 兼容壳（组 K 遗留，仍被 main / provision / seed / test 引用） | re-export |

**唯一入口（跨域唯一，禁止另起）**

| 事项 | 唯一入口 | 位置 |
|---|---|---|
| 收款人解析 / 转账执行 | `resolve_payee` / `execute_transfer` | `utils/transfer.py` |
| 业务常量（额度/阈值/上限） | `rules.py`（`TRANSFER_VERIFY_THRESHOLD` 等） | `rules.py` |
| DB 连接 | `db.get_conn()`（**P1.3 进程内连接池**；域代码禁止自建裸 `pymysql.connect`） | `mock_bank/db.py` |
| 写操作留痕 | `db.log_operation(...)` | `mock_bank/db.py` |
| 验证码 | `issue_code` / `check_code` | `utils/verify_codes.py` |
| 支付密码核验 | `verify` / `has_passed` | `utils/paypass.py` |
| 站内通知 | `push_notification` | `utils/inbox.py` |
| 知识库（Agent 事实源） | `docs/knowledge/*.md`（★**代码未接线**） | `agaent/docs/knowledge/` |

> 文件存储：**本项目无上传/文件存储模块**；若新增，按 §二「新外部服务」建 `utils/<svc>_client.py`。

## 四、安全不变式（任何变更不得破坏）

1. **资金操作必经 `agent/write_flow` 完整流程**：`phone_guard → fill_params → clarify → risk_check → verify → confirm → execute`；表单不得直连 `/api/transfer`。
2. **密码零落盘**：支付密码只存 hash（`paypass`，盐取自 `user_id`）；明文不落库、不落盘、不进日志。
3. **用户隔离取 token**：user_id 唯一来源 `deps.current_uid` → `security.resolve_user_id`；禁止从请求入参取 user_id。
4. **手机未绑定拦写操作**：`write_flow.phone_guard` 不通过则不放行。
5. **金额一律 `Decimal`**；前端展示走 `Amount` 组件（默认脱敏，`reveal` 才显示）。

## 五、同步义务（改一处，连带更新）

| 改了 | 必须同步 |
|---|---|
| `rules.py` 任何数值 | `docs/knowledge/*.md` 同名数字（如转账限额 ¥50,000 必须一字不差） |
| API（增/改端点、字段） | `README.md` §5「HTTP 端点」+ 验收清单；涉及写操作回执/失败字段 → 同步 `README.md` §5.1「统一结构化响应协议」 |
| 新增错误码 | `utils/response_codes.py` 的 `ERR` + `README.md` §5.1 错误码表 |
| 新增/删除 `utils/` 域文件 | 本文件 §三 域清单表 |
| 新增数据库表 / 字段 | `sql/upgrade_*.sql` + `init_db.py` / 对应 seed 脚本 |

## 六、AI 协作协议（复制到提示词头部即可）

```
本项目遵守 bank-agent/CONTRIBUTING.md：
1) 三层上限：路由→域文件→db，不设纯转发层，跨域只引 utils/db；
2) 一域一文件：转账=utils/transfer.py、卡=utils/card.py、订阅=utils/subscription.py、理财=utils/wealth.py；
3) 入口唯一：收款人解析必走 utils/transfer.resolve_payee；常量进 rules.py；连接走 db.get_conn；留痕走 db.log_operation；
4) 一次改动≤2 文件，超出先报告；新建文件须说明"为何不是现有域的延伸"；
5) 新抽域用惰性 import（顶层不 import mock_bank.main）+ router=APIRouter()，main 仅 include_router；
6) 安全不变式：资金走 write_flow 全流程、密码零落盘、user_id 取自 token、手机未绑定拦写、金额用 Decimal；
7) 交付必含：改动清单 + 影响说明 + 验收自测；
8) 禁止顺手重构 / 加功能 / 改文案。
```

## 七、规范变更流程

新规则须走 **动议 → 影响评估（列出受影响的域/文件）→ 版本号 + 追加**（见末尾版本记录）。
禁止未经评估直接加规则；删除或放宽任何铁律须说明理由，并同步更新 `audit_coupling.md`。

## 八、技术债白名单（已知差异，**禁删禁合并**）

| 项 | 现状 | 为何保留 |
|---|---|---|
| `verify_codes` vs `paypass` 重试锁 | 错 3 次**作废码** vs **锁账户至 23:59:59** | 语义不同，两常量独立（`VERIFY_CODE_MAX_ATTEMPTS` / `PAYPASS_MAX_ATTEMPTS`） |
| `ACTION_SPECS[*]["need_verify"]` | 死 lambda（transfer `>5000`）；实际生效是 `write_flow._verify_queue`（`>2000`） | 历史凭证，保留并标注，不删 |
| `TRANSFER_DAILY_LIMIT=50000` | 已落实单笔 / 单日累计双限额（默认均 50000），见 `rules.get_rule('transfer_daily_limit')` + `utils/transfer.check_transfer_limit`（事务内行锁 + 计数器 + 串行防并发）；阈值经 `set_rule_value` 运行期可调 | 知识库口径见 `docs/knowledge/transfer-rules.md` |
| 轨道判定双标签 | resolver `insite/cross_bank` vs route `insite/offsite`，校验时机不同 | 已收口谓词 `is_insite_track`；改标签破坏 API 契约 |
| `agent/write_flow.py` | 单文件承载写流程（≈1402 行） | 强行拆会制造过度解耦，留 |
| `profile_db.py` + `user_provision.py` | 各 >400 行 | 职责不同（画像/令牌 vs 开户），留 |

---
**版本记录**：v1.0 · 2026-09-11 · 首版（固化合并式重构成果，约束后续变更）。
