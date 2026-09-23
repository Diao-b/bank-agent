# 过度解耦体检报告 · audit_coupling.md

> 项目：小微 AI 银行助手（bank-agent）
> 阶段：第一阶段（只读体检，未改动任何代码）
> 配套约束（铁律，待第二阶段写入 CONTRIBUTING.md）：
> 1. 三层上限：路由 → 服务 → 数据访问；简单逻辑直接进路由
> 2. 一域一文件：transfer 就是 transfer.py，不允许 xxx_service / xxx_manager / xxx_helper 拆分
> 3. 跨域引用仅限 utils（纯函数）与 db 层
> 4. 行为零变化——纯结构调整，禁止顺手改逻辑/加功能
> 5. 合并为主方向：拆掉的层合回去，不是重新规划
> 6. 重构前先录制基线（tests/baseline/），途中改需求会让 diff 误报

---

## 0. 全局结构速览（先建立共识）

```
bank-agent/
├── run_server.py   (1628 行)  8178：SPA + Agent + 显式路由 + 通配代理 proxy_api
├── mock_bank/
│   ├── main.py     (2541 行)  8003：全部银行 API 路由（内联业务逻辑）
│   └── db.py        (81 行)   数据访问层（get_conn / fetch_* / execute / log_operation）
├── config.py       (130 行)   配置单源（env → 模块常量）
├── profile_db.py   (792 行)   用户画像/令牌数据层（自带 _conn）
├── utils/  (14 个文件，129~396 行)
│   ├── transfer_resolver.py  card_service.py  card_validator.py
│   ├── subscription_detector.py  deduction_engine.py  settlement_engine.py
│   ├── inbox.py  verify_codes.py  paypass.py  user_provision.py
│   ├── mcp_bridge.py  mcp_client.py  event_bus.py  security.py
└── agent/  (LangGraph 状态机：main_graph / write_flow / read_agent / tools / prompts / state / driver)
```

**重构后结构（2026-09-11 · 第二阶段完成）**：

```
bank-agent/
├── run_server.py                 8178：SPA + Agent + proxy_api（未拆 —— 网关必需）
├── config.py                     配置单源（进程/部署）
├── rules.py                      ★业务常量单源（组 W）
├── profile_db.py / agent/        （保持）
├── mock_bank/
│   ├── main.py  (1012 行 ← 2541) 瘦身后：读端点 + 账户/收款人/归集 + include_router 组合
│   └── db.py                     数据访问层（唯一连接 / 留痕入口）
└── utils/
    ├── card.py           卡域单文件（校验 + 服务 + 7 路由）※ 组 K + C+；壳 card_service/card_validator
    ├── subscription.py   订阅域（detect + 10 路由）※ 组 S；惰性壳 subscription_detector
    ├── transfer.py       转账域（resolve_payee + execute_transfer）※ 组 T；壳 transfer_resolver
    ├── wealth.py         理财域（products / holdings / buy / redeem 路由）※ 组 P
    ├── settlement_engine.py  跨行清算 + 订阅补扣双引擎 ※ 组 H；壳 deduction_engine
    ├── deps.py           current_uid（FastAPI 依赖单源）※ 组 C+
    └── inbox / verify_codes / paypass / user_provision / security / event_bus / mcp_*  （保持）
```

**架构事实（影响所有结论）**：`run_server`(8178) 用 `@app.api_route("/api/{path:path}")`(run_server:1518) 把
**除 verify/health 外**的所有 `/api/*` 经 HTTP 转发到 `mock_bank`(8003)。因此每个银行 API 实际是
`前端 → run_server.proxy_api(HTTP) → mock_bank.route → 业务/工具 → db`。
代理跳是**跨进程网关**，下文"调用链深度"只统计**单进程内（mock_bank 一侧）的落库文件数**，代理跳单列说明。

**总体判断（重要，与"过度解耦"前提略有出入）**：
本项目**不是经典意义的过度抽象**（没有 ABC/Protocol/Manager/Helper 抽象泄漏，见 A5），
主要矛盾是**两种相反倾向并存**：
- 路由层是**上帝文件**：`mock_bank/main.py`(2541) 把全部业务逻辑内联在路由里；
- 少数业务域**被不必要地拆成多文件**（card / subscription / transfer / verify）；
- 数据访问入口**被复制成 ~12 处**直连 pymysql，绕过了唯一的 `db.get_conn`；
- 业务常量**散落各文件，无 rules.py 单一来源**（验收 #3 直接受此影响）。

所以合并式重构的主线是：**把拆散的同域文件合回单文件 + 把直连连接收口到 db 层 + 抽 rules.py**，
同时借机把 `main.py` 里最重的几个域（transfer / subscription / card）抽成域文件，**既消god-file又消过拆**。

---

## A. 解耦问题扫描

### A1. 过深调用链（单进程内，路由 → 落库，>3 即标记）

| # | 链路（文件 → 文件 → 文件 → 落库） | 层数 | 标红层（只转发/包装） | 备注 |
|---|---|---|---|---|
| 1 | main.py`/api/transactions` → settlement_engine.process_housekeeping → **deduction_engine.run_due_deductions** → db | **4** | deduction_engine（被 settlement 编排，自身是完整引擎） | 最深链；两引擎链式编排 |
| 2 | main.py`/api/payees/resolve` → transfer_resolver.resolve_payee → **card_validator**(luhn/bank_from_card) → db | 4 | card_validator 是 utils 纯函数（铁律#3 允许，不计入"层"） | 实际层数=3（main→resolver→db） |
| 3 | run_server`/api/verify/send` → verify_codes.issue_code → **inbox.push_notification** → db | 4 | inbox 是独立服务（非转发） | 跨两进程；mock_bank 侧=3 |
| 4 | main.py`/api/cards/apply` → card_service.apply_card → **card_validator**(gen_unique_card_no) → db | 4 | card_validator 纯函数（不计层） | 实际=3 |
| 5 | main.py`/api/transfer`(跨行) → 内联逻辑 → settlement_engine(台账) → inbox → db | 4 | inbox 服务层 | 内联逻辑本身无独立 service 文件 |

**结论**：真正超过 3 层的是 **#1（settlement→deduction 双引擎）**。其余"4 层"多因夹了
`card_validator`（纯函数工具，按铁律不算层）或 `inbox`（独立服务，不是转发）。验收 #1 满足方式：
把 deduction_engine 合进 settlement_engine（或二者并入 housekeeping.py），#1 即降为 3 层；其余本就≤3。

### A2. 一域多文件（应合并候选组）

| 域 | 拆成的文件 | 各行数 | 互相 import | 合并目标 | 严重度 |
|---|---|---|---|---|---|
| Card | `card_service.py` + `card_validator.py` | 334 + 198 = 532 | card_service→card_validator | `card.py`（纯函数+服务合一，<800 行） | P1 |
| Subscription ★已解决(组S) | `subscription_detector.py` + `deduction_engine.py` + main.py 内订阅路由(844–918,1965–2466) | 129+191+~575 | detector→db；engine→db(已并 settlement)；路由内联 | `utils/subscription.py`(detect+状态机，~700)；`subscription_detector.py` 降惰性兼容壳；main 仅留 `include_router` 薄壳 | P1 |
| Transfer ★已解决(组T) | `transfer_resolver.py` + main.py`/api/transfer`(内联 242 行) + settlement_engine(共享清算) | 321+242+257 | resolver→card_validator/db；main→inbox/settlement | `utils/transfer.py`(resolve+execute 合一，~590)；`transfer_resolver.py` 降 re-export 壳；settlement_engine 保留（被 housekeeping 共用） | P1/P2 |
| User/Profile | `user_provision.py` + `profile_db.py` | 396 + 792 = 1188 | provision→card_service/db；profile→db | **不建议合**（合后>800，且两者职责不同：开通 vs 画像/令牌） | P2（边界，留） |

### A3. 纯转发层（只透传/调一个函数就 return）

| 位置 | 表现 | 是否真转发层 | 处置 |
|---|---|---|---|
| `card_service.group_card_no / mask_card_no / tail_of`(67–83) | 直接 `return card_validator.xxx(...)` | **是**，薄包装 | 合并进 `card.py` 后消失（或保留为域函数） |
| `inbox.push_transfer_receipt`(67–84) | 拼文案后 `return push_notification(...)` | 轻微包装 | 可保留（便利助手），非阻塞项 |
| `run_server.proxy_api`(1518) | 原样转发 HTTP 到 mock_bank | **是转发层，但属架构网关** | **保留**（SPA+Agent 单源必需）；不计入"坏解耦" |
| `settlement_engine.process_housekeeping`(222) | 编排 deduction_engine + process_due_settlements | 编排层非转发 | 合并 deduction 后自然消解双引擎链 |

> 注：未发现"service→manager→helper→db"四级纯转发。真正的薄转发集中在 card_service 对 card_validator 的再包装。

### A4. 重复实现（同一逻辑多处各写一份）

| 组 | 重复位置 | 行为是否一致 | 处置 |
|---|---|---|---|
| **DB 连接获取** | `db.get_conn`(db.py:21) 是唯一正规入口；但直连 `pymysql.connect(**config.db_config())` 出现在：profile_db._conn(75)、inbox._conn(27)、verify_codes._conn(30)、paypass._conn(33)、security.ensure_ownership 非 cur 分支(217)、**run_server 内 8 处**(990/1194/1256/1306/1340/1377/1411/1457)、user_provision(76) | 一致（同签名） | **全部收口到 `db.get_conn`**（P0，最高价值：统一事务/连接语义） |
| **验证码 vs 支付密码 重试锁** | verify_codes.MAX_ATTEMPTS=3（错3次作废码，TTL 300）；paypass.MAX_ATTEMPTS=3（错3次锁到当日23:59:59） | **语义不同**（作废码 vs 锁账户） | **不自动合并**（铁律#4：行为不一致先报告）。仅把二者的 `_conn` 收口到 db 层；锁/作废语义各自保留 |
| **留痕 operation_logs** | 正规 `db.log_operation`（带 cur 复用事务）；但 paypass._log(85) 与 run_server 后台/实名路由(1256+/1306+/1340+/1377+/1411+/1457) 用**内联 INSERT** 自开连接写 | 一致（都写 operation_logs） | 内联处改用 `db.log_operation`；paypass 因需"失败不影响主流程"可保留但应走 db 层连接（P1） |
| **转账轨道判定 insite/offsite** | transfer_resolver._validate_payee(184) 算 `track`；main.py`/api/transfer`(1073–1104) 又自己算一遍 | **近似但不完全一致**（resolver 看 linked_user_id；route 还校验 linked 用户存在且正常） | 合并进 transfer.py 后统一为单一判定函数（P1，避免"改一处漏一处"） |
| **余额校验** | transfer(1107) / deduction_engine(83) / settlement_engine(_PRIMARY_ACCOUNT_SQL) 各有 `Decimal(balance) 比较` | 一致（上下文不同） | 低优先；可抽 `utils` 纯函数 `has_sufficient(cur, acct, amt)`，非阻塞 |

### A5. 抽象泄漏（接口/基类只有一个实现）

**未发现。** 全仓 grep `abstractmethod / ABC / Protocol / interface / *Manager / *Helper / *Base / *Service`
**零命中**。本项目没有任何抽象基类或单实现接口——与"过度抽象"相反，是"平铺 + god-file"。
→ 第二阶段**无需去接口**（没有可去的接口），也反向说明合并方向正确（合文件而非抽接口）。

### A6. 配置穿透（配置值传了 3+ 层才用到）

**未发现穿透问题**。`config.py` 是扁平模块，业务代码一律 `config.X` 直接读（如 `settlement_engine.SETTLE_DELAY_MINUTES`
由 main.py 直接 `import` 引用，1 跳）。无"入口→A→B→C 传参传递"式穿透。
⚠️ 但"业务**常量**散落"（非配置穿透）是真实问题，见 B1 / 合并计划 R。

---

## B. 修改影响面统计

### B1. 改"转账限额"现在要动几个文件？

**当前事实：转账限额（单笔/单日 ¥50,000）只在 RAG 文档（docs/knowledge）描述，代码中并未强制校验**——
`/api/transfer`(main.py:1012) 仅校验 `amount>0` 与余额，无上限。真正散落在代码里的是**风控分级阈值**：

| 常量 | 位置 | 含义 |
|---|---|---|
| `> 5000` 需验证码 | write_flow.py:101 / 1070 | 风险分级（你知+你有） |
| `> 20000` 或新收款人大额 | write_flow.py:824 / 1071 | 大额警示行 |
| `CARD_LIMIT_MAX = 50000` | main.py:61 | **信用卡调额上限**（非转账限额，易被误认） |
| `CARD_DEFAULT_CREDIT_LIMIT=5000` | main.py:63 | 信用卡默认额度 |
| ¥50,000 单笔/单日 | docs/knowledge（未落地） | Agent 知识，无代码强制 |

**影响面**：若要把"转账限额"做成真强制规则，目前**无任何单点**——需新建 `rules.py` 并在
`/api/transfer` 增加校验（1 处新增 + 1 处常量）。但"改风控阈值"要动 **write_flow.py 的 3 处**
(101/824/1070-1071)，且阈值与 docs 描述可能漂移。**验收 #3 的前置条件 = 建 `rules.py` 收口。**

### B2. 修复半径复盘（无 git，按结构证据推断 3 个历史特性）

> 仓库无 git 历史，以下基于代码结构与已知特性边界推断"改一处牵动几处"，非精确 diff。

| 历史特性 | 当时牵连文件（结构推断） | 修复半径 | 连锁隐患 |
|---|---|---|---|
| 跨行在途清算（settlement_engine + 退票） | main.py`/api/transfer`(台账登记) + settlement_engine + sql/upgrade_crossbank + inbox(退票通知) | 4 文件 | 轨道判定在 transfer route 与 resolver 各一份（A4），改退票文案易漏一边 |
| 验证码 C3 改造（收件箱必达、MySQL 化） | 新增 verify_codes.py + inbox.py(双写) + run_server`/api/verify/*` + **mock_bank 旧 VERIFY_CODES 未删**（A1 死代码） | 4 文件 + 1 死代码 | 旧实现残留致"看起来有两套"，新人易改错文件 |
| 卡片-账户一体化（开卡=开账户） | card_service + card_validator + sql/upgrade_card_account + transfer_resolver(反查) + main.py`/api/transfer`(入账落点) | 5 文件 | 反查口径在 transfer_resolver.lookup_local_card，转账入账在 main.py 内联，两处需同步 |

**共性教训**：每次"重特性"都因逻辑散在 route 内联 + 独立 utils 而牵动 4–5 文件；且**旧实现常未清理**
（VERIFY_CODES）造成"双实现"错觉。合并式重构正是要把同域逻辑收口到单文件，把修复半径压到 1–2 文件。

---

## C. 合并计划（按业务域分组）

> 规则：保持函数名不变（只挪位置），减少调用方改动；文件 >800 行才拆两文件并说明边界。
> 每组完成后立即跑该域回归（tests/ 下脚本 + 录制基线 diff），再做下一组。

### 组 R（P0，基础设施，最先做）
- **合并前**：`db.get_conn`(db.py) 为唯一入口；`profile_db._conn` / `inbox._conn` / `verify_codes._conn` / `paypass._conn` / `security:217` / `run_server`×8 / `user_provision:76` 各自直连。
- **合并后**：全部改为 `from mock_bank import db; db.get_conn()`（跨域引用 db 层符合铁律#3）。
  run_server 已 import profile_db，加 `from mock_bank import db` 即可。
- **改动量**：约 12 处 import/调用替换，纯机械，无逻辑变化。
- **风险**：`db.get_conn` 是**热点文件**，但本次只"让别人调用它"，不改其签名，影响面为零。

### 组 V（P0，删死代码）★ 2026-09-11 已解决
- **合并前**：`mock_bank/main.py:55,951–999` 旧 `VERIFY_CODES` 内存字典 + `/api/verify/send` + `/api/verify/check`。
- **合并后**：**整段删除**（含 `VERIFY_TTL_SECONDS`/`_VERIFY_LOCK`/`VerifySendReq`/`VerifyCheckReq`，均仅死代码块内部引用）。
  `run_server` 的 `/api/verify/*`(1106/1121) 调 `verify_codes` 才是活路径；
  proxy_api 已 exclude verify(1506)，前端永不到 mock_bank 的 verify 路由。
- **改动量**：删 ~50 行；无调用方（仅 mock_bank 自身，grep 已确认）。`random` 仍为 serial_no 生成所用，import 保留。
- **风险**：低。删除前用 grep 确认 `VERIFY_CODES` 仅 mock_bank 引用（已确认）。
- **零变化证据（本轮）**：
  - 只读基线 `baseline_readonly.json` 对比 post-R `golden_readonly.json` → **IDENTICAL**（忽略 `sse_chat_stream,_meta` 及
    3 个 append-only 日志/通知端点 `/api/operation_logs`、`/api/inbox`、`/api/admin/logs`——它们被"捕获过程自身的 SSE·agent 动作"追加行，
    自增 id 不可复现，与 sse 同列为环境相关面）。
  - 8 套回归 **ALL GREEN**：crossbank 41/41、insite 27/27、account_system ✓、card_apply 66/66、internal 26/26、
    subscription 29/0、realname 7/7、card ✓（rc 全 0）。
  - 8003 重启后 health=200；`py_compile` 通过；main.py 内 `VERIFY_CODES` 等 4 符号零残留。

### 组 K（P1，Card）★ 2026-09-11 已解决
- **合并前**：`card_service.py`(334) + `card_validator.py`(198)。
- **合并后**：`utils/card.py` 为卡域单文件（卡号校验纯函数 + 卡片-账户一体化服务，~530 行 <800）。
  `card_validator.py` / `card_service.py` 降为**兼容 re-export 壳**（`from utils.card import (...)`，
  仅 re-export 各自公开名），故 `transfer_resolver` / `mock_bank.main` / `user_provision` / `seed_*` /
  `tests/test_card.py` 等全部调用方**零改动**。`card_service.group_card_no/mask_card_no/tail_of`
  薄包装在 card.py 内直接调用同模块 `normalize_card_input` / `generate_card_no`（去掉 `card_validator.` 前缀）。
- **改动量**：新增 card.py + 将两个旧文件改写为 shim；无调用方改动。
- **风险**：`card_validator` 是**热点**，用 shim 兼容零改动规避；`card.py` 不 import 任何 utils 子模块，无循环导入。
- **零变化证据（本轮）**：只读基线 vs golden **IDENTICAL**（同组 V 忽略项）；8 套回归 **ALL GREEN**
  （crossbank 41/41、insite 27/27、account_system ✓、card_apply 66/66、internal 26/26、subscription 29/0、
  realname 7/7、card ✓，rc 全 0）。`from utils import card is card_service.apply_card` 断言同对象，无重复实现。

### 组 S（P1，Subscription）★ 2026-09-11 已解决
- **合并前**：`subscription_detector.py`(129) + `deduction_engine.py`(191，组 H 已并入 settlement) + main.py 订阅路由(844–918 + 1965–2466)。
- **合并后**：`utils/subscription.py` 为订阅域单文件（**detect 识别引擎 + 10 个路由/状态机**，~700 行 <800）。
  `deduction_engine` 已在组 H 并入 `settlement_engine`，本域经 `deduction_engine.run_due_deductions`（兼容壳）调用。
  装饰器挂在 `router = APIRouter()` 上，main.py 仅留薄壳 `app.include_router(subscription.router)`（置于**模块末尾**）。
  `subscription_detector.py` 降为**惰性兼容壳**（模块级 `__getattr__` 转发；先 `import mock_bank.main` 打破循环导入）。
- **改动量**：main.py 净删 575 行路由/模型（2477→1902）；新增 `utils/subscription.py`(~700)。
- **风险**：订阅路由是 main.py 最大区段。两处坑已踩并解决：
  (1) **路由顺序**——`/api/subscriptions/{sub_id}`(path param) 必须晚于 `/api/subscriptions/discover`(static) 注册，
      router 内保留原顺序（否则 discover 会被 `{sub_id}` 吞掉）；
  (2) **共享 helper 循环导入**——subscription.py 顶部 `from mock_bank.main import owned/json_safe/to_decimal/fail/current_uid/VALID_CYCLES/add_months`，
      故 main 的 `include_router` 必须放在模块**最末**（各 helper 定义之后）；兼容壳用惰性转发 + 先 `import mock_bank.main` 兜底，任何导入顺序都安全。
- **零变化证据（本轮）**：只读基线 vs golden **IDENTICAL**（同组 V 忽略项）；8 套回归 **ALL GREEN**
  （crossbank 41/41、insite 27/27、account_system ✓、card_apply 66/66、internal 26/26、subscription 29/0、
  realname 7/7、card ✓，rc 全 0）。`from utils.subscription_detector import discover_candidates` 与
  `utils.subscription.discover_candidates` 同对象；shell-first / main-first 两种导入顺序均通过。

### 组 H（P1，清算/补扣引擎合并，顺带压平 A1#1）
- **合并前**：`settlement_engine.py`(257) 编排 `deduction_engine.run_due_deductions`(191)。
- **合并后**：`deduction_engine` 整体并入 **`settlement_engine.py`**（双引擎合一，~450 行）。
  `deduction_engine.py` 降为**兼容 re-export 壳**（`from utils.settlement_engine import ...`），故
  `main.py`(861/2130 调 `deduction_engine.run_due_deductions`) 与 `settlement_engine.process_housekeeping`
  内部调用均零改动。`process_housekeeping` 改调同文件 `run_due_deductions`，A1#1 链降为 3 层。
- **改动量**：搬 subscription 函数进 settlement_engine + 去 `from utils import deduction_engine`（防循环导入）
  + 旧文件改 shim；无调用方改动。
- **风险**：`settlement_engine` 是**热点**。改名规避碰撞：`deduction` 的 `_DUE_SQL/_ACCOUNTS_SQL/_DUP_SQL`
  → `_SUB_DUE_SQL/_SUB_ACCOUNTS_SQL/_SUB_DUP_SQL`；订阅 `describe_result` → `describe_deduction_result`
  （对外 shim 仍 re-export 为 `describe_result`）。两模块原 `describe_result` 均未被外部调用，安全。
- **零变化证据（本轮）**：只读基线 vs golden **IDENTICAL**；8 套回归 **ALL GREEN**
  （crossbank 41/41、insite 27/27、account_system ✓、card_apply 66/66、internal 26/26、subscription 29/0、
  realname 7/7、card ✓，rc 全 0）。`deduction_engine.run_due_deductions is settlement_engine.run_due_deductions` 断言通过。

### 组 T（P1/P2，Transfer）★ 2026-09-11 已解决
- **合并前**：`transfer_resolver.py`(321) + main.py `/api/transfer` 内联(242) + settlement_engine(共享)。
- **合并后**：`utils/transfer.py` 为转账域单文件（**resolve_payee 唯一入口 + execute_transfer 执行**，~590 行 <800）。
  `transfer_resolver.py` 降为**兼容 re-export 壳**（23 行；调用方 `scripts/seed_card_account.py` 零改动）。
  main.py `/api/transfer` 改为薄壳调 `transfer.execute_transfer(...)`；`/api/payees/resolve` 直调 `transfer.resolve_payee`；
  `_sender_name` 随执行逻辑一并迁入。settlement_engine 保留（被 housekeeping 共用，见组 H）。
- **A4 双份判定收口**：抽出单一谓词 `is_insite_track(linked_user_id, has_insite_target)`，resolver 与 execute 共用。
  ★ 两端点 track **标签不同**（resolve 用 `cross_bank`；execute 用 `offsite`）且 resolver **不做实时用户校验**（假设绑定即站内），
  故**只收口谓词、保留各自标签与校验时机**（改标签/加校验都会破坏既有 API 契约，属行为变化，不做）。
- **改动量**：main.py 净减 ~233 行（1905→1672）；新增 `utils/transfer.py`(~590)。
- **风险/坑**：`utils/transfer.py` 顶层**刻意不 import mock_bank.main**（否则与 main 末尾互相依赖形成循环导入）——
  共享 helper `owned/to_decimal/fail` 改为 `execute_transfer` **函数内惰性 import**。因此 main 可安全地在**顶层**
  `from utils.transfer import ...`，兼容壳也可安全地直接 re-export（`seed_card_account.py` 独立导入亦通过）。
- **零变化证据（本轮）**：只读基线 vs golden **IDENTICAL**（同组 V 忽略项）；8 套回归 **ALL GREEN**
  （crossbank 41/41、insite 27/27、account_system ✓、card_apply 66/66、internal 26/26、subscription 29/0、
  realname 7/7、card ✓，rc 全 0）。`transfer_resolver.lookup_local_card is transfer.lookup_local_card` 断言同对象；
  `is_insite_track(None)=False / (5)=True / (5,False)=False`。

### 组 C（P2，留痕统一）★ 2026-09-11 已解决
- **合并前**：内联 `INSERT INTO operation_logs` 散在 6 处：`paypass._log`、`run_server`(phone_bind_confirm / admin_verify_code_plain)、
  `verify_codes`(verify_send / verify_check)、`user_provision`(provision_user)。
- **合并后**：**全部改走 `db.log_operation`**（唯一留痕入口）；传 `cur=` 复用调用方事务的场合保持同事务语义，
  旁路场景（paypass）走 `log_operation` 自开连接。`grep INSERT INTO operation_logs` 现仅命中 `db.py` 自身（单源）。
- **改动量**：6 处内联 SQL → 6 行 `db.log_operation(...)` 调用。
- **★ 行为等价要点**：`db.log_operation` 对非字符串 detail 做 `json.dumps(..., ensure_ascii=False)`，
  与原手写 f-string **逐字节一致**（已断言）：`{"scene": "phone_bind", "vc_id": 42}` / `{"scene": "phone_bind"}` / `{"vc_id": 42}`；
  phone_bind / provision_user / paypass 原本就是字符串 detail，原样透传。
- **零变化证据（本轮）**：只读基线 vs golden **IDENTICAL**；8 套回归 **ALL GREEN**
  （crossbank 41/41、insite 27/27、account_system ✓、card_apply 66/66、internal 26/26、subscription 29/0、
  realname 7/7、card ✓，rc 全 0）——其中 card_apply / insite / crossbank / internal 均**直接断言 operation_logs 内容**，覆盖留痕等价。

### 组 C+（卡片路由抽离）★ 2026-09-11 已解决（第二阶段追加组）
- **合并前**：`/api/card/validate`、`/api/cards`(GET)、`/api/cards/apply`、`/api/cards/adjust_limit`、
  `/api/cards/freeze`、`/api/cards/unfreeze`、`/api/cards/{id}`(DELETE) 共 7 个路由 + 3 个请求模型内联在 main.py。
- **合并后**：路由**与卡域同处** —— 全部迁入 **`utils/card.py`** 的「HTTP 路由」段（`router = APIRouter()`）；
  main.py 仅留 `app.include_router(card_domain.router)`。旧 `card_service.` / `card_validator.` 前缀本地化为同模块直接调用。
- **两处必要的配套改动**：
  1. **`current_uid` 收口到新的 `utils/deps.py`**（单一归属）——原因：域模块路由需在**定义期**求值
     `Depends(current_uid)`，而 `card.py` 会被 user_provision / seed / 兼容壳**先于 main** 导入，
     不能顶层 import main；`security.py` 又**刻意不依赖 FastAPI**（其头注释），故给它一个 FastAPI-aware 的中立归属。
     main 改为 `from utils.deps import current_uid`（同名 re-export，零调用方改动）。
  2. **路由函数 `apply_card` 改名为 `apply_card_endpoint`**——与卡域服务 `card_service.apply_card` 同名，
     同处一文件会互相覆盖（URL/行为不变，仅内部函数名）。
- **复用组 T 惰性模式**：`utils/card.py` 顶层**不** import `mock_bank.main`；`owned`/`fail`/`json_safe`/`to_decimal`
  在各路由函数内惰性 import（4 个 helper 按需）。
- **改动量**：main.py −232 行（1672 区域 → **1260**，本组净减 ~230）；`utils/card.py` 941 行；
  新增 `utils/deps.py`(37)。
- **⚠️ 已知偏离**：`utils/card.py` 941 行 > 800 行指引——因用户明确要求「路由与 card.py 同处」，
  故未按「>800 拆两文件」处理；如需拆分可改为 `utils/card_routes.py`（同域两文件）。
- **零变化证据（本轮）**：只读基线 vs golden **IDENTICAL**；8 套回归 **ALL GREEN**（card_apply 66/66 直接覆盖卡路由）；
  `app.openapi()` 含全部 7 条卡路径；`main.current_uid is utils.deps.current_uid`。

### 组 P（理财路由抽离）★ 2026-09-11 已解决（第二阶段追加组）
- **合并前**：`/api/products`(GET)、`/api/holdings`(GET)、`/api/products/buy`(POST)、`/api/redeem`(POST)
  + `WealthBuyReq` / `RedeemReq` 内联在 main.py。
- **合并后**：抽到 **`utils/wealth.py`**（理财域单文件，284 行，`router = APIRouter()`）；main 仅 `include_router`。
  复用组 T 惰性模式（顶层不 import main；`json_safe`/`fail`/`to_decimal` 函数内惰性 import）。
- **改动量**：main.py 净减 ~248 行（1260 → **1012**）；新增 `utils/wealth.py`(284)。
- **零变化证据（本轮）**：只读基线 vs golden **IDENTICAL**；8 套回归 **ALL GREEN**；
  `app.openapi()` 含全部 4 条理财路径（`/api/products`、`/api/holdings`、`/api/products/buy`、`/api/redeem`）。

### 组 D（死代码清理）★ 2026-09-11 已解决（第二阶段收尾组）

**工具**：`vulture 2.16`（装在隔离环境，未污染项目 venv）+ 全仓人工零引用核验。
vulture 输出 113 行候选中**绝大多数是误报**（FastAPI 路由函数 / `TypedDict` 字段 / 被 re-export 壳**间接**引用的名字
—— vulture 看不穿壳），故一律以「全局搜索符号名 + 前端 / 测试 / SSE / 路由 URL 字符串」为准。

**已删除清单（每项附零引用证据）**

| # | 项目 | 位置 | 行数 | 零引用证据 |
|---|---|---|---|---|
| 1 | 兼容壳 `transfer_resolver.py` | `utils/` | 24 | 唯一调用方 `scripts/seed_card_account.py` 已改直指 `utils.transfer`；全仓无 `from utils import transfer_resolver` |
| 2 | 兼容壳 `subscription_detector.py` | `utils/` | 41 | 全仓零 import（仅注释提及） |
| 3 | 兼容壳 `deduction_engine.py` | `utils/` | 11 | 唯一调用方 `utils/subscription.py` 已改 `from utils.settlement_engine import run_due_deductions` |
| 4 | 死常量 `MARK_VERIFY_SMS` / `MARK_VERIFY_PWD` | `agent/write_flow.py` | 2 | `[VERIFY:...]` 标记实际由 `_verify_question` 内联拼接，二常量零引用 |
| 5 | 死常量 `READ_TEST_DB` | `config.py` | 2 | 零引用（`WRITE_TEST_DB` 仍被 agent 使用 → 保留） |
| 6 | 死反查表 `TITLE_TO_GENDER` | `profile_db.py` | 1 | 零引用（`GENDER_TO_TITLE` 被 :162/:249 使用 → 保留） |
| 7 | 死函数 `save_greeting` / `touch_conversation` | `profile_db.py` | ~20 | 零引用；已被 `get_or_create_greeting`（走 `save_message`，含 touch）取代 |
| 8 | 死私有函数 `_bank_by_id` | `utils/transfer.py` | 6 | 零引用（组 T 合并后仅 `_bank_by_code` 在用） |
| 9 | 死导入 `EmbeddedResource` / `ImageContent` | `utils/mcp_client.py` | 2 | 仅出现在 import 与降级赋值行；仅 `TextContent` 被 `_as_text` 使用 |
| 10 | 不可达分支 `… if True else []` | `run_server.py:1345` | 1 | vulture 100% 判定不可达；等价于直接赋值 |

合计：**删 3 个文件（76 行）+ 约 36 行代码**。删除后全仓复扫，残留仅剩各文件里「原 X 并入本文件」的
**合并史注释**（保留，属历史凭证）。

**疑似死代码（零引用但有逻辑 / 像公开 API；按要求只报告、不删）**

| 符号 | 位置 | 备注 |
|---|---|---|
| `set_current_user_id` / `get_current_auth_token` / `ensure_all_ownership` / `primary_account_id` | `utils/security.py` | 隔离层公开 API，security 头部注释提及 `set_current_user_id` |
| `current_scope` / `listener_count` | `utils/event_bus.py` | SSE 事件总线自省辅助 |
| `describe_result` / `describe_deduction_result` | `utils/settlement_engine.py` | 结果文案助手（组 H 已备注「未被外部调用」） |
| `execute_tool_calls` | `agent/tools.py` | read_agent 已内联等价逻辑；agent 域，保守留 |
| `ensure_isolation_schema` | `utils/user_provision.py` | 隔离 schema 运维助手（可能供迁移/排障用） |
| `verify_record_count` | `utils/paypass.py` | 违规核验计数查询（后台倾向） |
| `cur_title`（未用局部变量） | `profile_db.py:239` | 可能指向未完成功能，建议人工确认而非直接删 |
| `_loop_thread`（未被读取的属性） | `utils/mcp_client.py` | ★ **禁删**：持有后台线程引用防 GC（降级链），删了会导致线程被回收 |

**未发现**：>10 行的「注释掉的代码块」（仅 2 处长注释，均为**设计说明**：`run_server.py:1472` 代理网关说明、
`utils/security.py:122` current_uid 用法示例）；种子脚本未发现无效字段（本轮未改 schema）。

**零变化证据（本轮）**：只读基线 vs golden **IDENTICAL**；8 套回归 **ALL GREEN**
（crossbank 41/41、insite 27/27、account_system ✓、card_apply 66/66、internal 26/26、subscription 29/0、
realname 7/7、card ✓）；`py_compile` 全通过；`/api/{balance,cards,products,subscriptions,transactions}` 实测 200。


### 组 W（P2，业务常量单源 rules.py，配合验收#3）★ 2026-09-11 已解决
- **⚠️ 核实推翻了原前提（重要）**：审计原判「write_flow.py:101 硬编码 5000 = 转账验证阈值」**有误**。
  实际 `ACTION_SPECS[*]["need_verify"]` 这批 lambda **从未被调用**（全仓无 `["need_verify"]` 读取，只读
  `required`/`label`）；**真正生效的是 `_verify_queue()`**（`risk_check` 用 `need = bool(queue)` 驱动
  verify/confirm 路由）。活阈值 = **transfer `>2000` 需双要素 / `>20000` 或（非常用且 `>5000`）大额警示 /
  buy_product `>10000`**，与 `docs/knowledge`（2000 / 20000）一致；死配置里的 **5000 与活逻辑、文档均冲突**。
  按用户「发现不一致立即停下报告」的指令**先停后报**，拍板结果：**按活逻辑迁移**，死 lambda 保留并标注。
- **合并后**：新建 **`rules.py`** 作为业务常量单源：
  `TRANSFER_VERIFY_THRESHOLD=2000` / `TRANSFER_BIG_THRESHOLD=20000` / `TRANSFER_BIG_UNFREQUENT_THRESHOLD=5000` /
  `WEALTH_VERIFY_THRESHOLD=10000` / **`TRANSFER_DAILY_LIMIT=50000`（★仅预留常量 + 注释，不加调用）**；
  `CARD_LIMIT_MIN/MAX`、`CARD_DEFAULT_CREDIT_LIMIT`、`DEBIT_CARD_MAX`/`CREDIT_CARD_MAX`/`CARDS_PER_ACCOUNT_MAX`、
  `SETTLE_DELAY_MINUTES`、`VERIFY_CODE_TTL_SECONDS`/`VERIFY_CODE_MAX_ATTEMPTS`、`PAYPASS_MAX_ATTEMPTS`（两份语义独立保留）、
  `SCAN_MONTHS`/`MIN_TXNS`/`MONTHLY_*`/`YEARLY_*`/`AMOUNT_STD_RATIO`/`EXCLUDED_CATEGORIES`、`VALID_CYCLES`/`VALID_CARD_TYPES`。
  各域模块改为 `from rules import ...`（保留本地名，兼容壳零改动）；`write_flow._verify_queue` 引用 `rules.*`。
  **副作用收益**：解除 `main↔subscription` 的 `VALID_CYCLES` 常量耦合（`subscription` 改从 rules 取）；
  main 删除 3 个实际未用的常量（`VALID_CARD_TYPES`/`CARD_DEFAULT_CREDIT_LIMIT`/`VALID_CYCLES`）。
- **边界（刻意不迁）**：进程/部署配置留 `config.py`；纯实现/识别常量（BIN 表、`LOCAL_BANK_*`、补扣护栏
  `MAX_CATCHUP_CYCLES`/`CYCLE_DAYS`、`_PASS_TTL_SECONDS`）留各自域文件 —— 不属"业务可调项"。
- **改动量**：新增 `rules.py`；改 8 个文件（main / card / settlement_engine / verify_codes / paypass / subscription / write_flow + rules 自身）。
- **零变化证据（本轮）**：只读基线 vs golden **IDENTICAL**；8 套回归 **ALL GREEN**；`_verify_queue` 边界断言逐条与原逻辑一致
  （2000→仅密码 / 2001→双要素 / 20001→大额警示 / 非常用5001→大额警示 / buy 10001→密码 / internal→仅密码）；
  各常量在 `rules` 与各域模块取值**逐一相同**；兼容壳（card_service / deduction_engine / subscription_detector）仍可 re-export。

### 不合并清单（合理分层，必须保留，严禁砍）
| 文件/层 | 为何保留 |
|---|---|
| `mock_bank/db.py` | 数据访问层（铁律#3 的合法跨域引用点）；合并方向是"别人来调它"，不是删它 |
| `utils/security.py` | 认证 guard + 资源归属校验（隔离铁律唯一收口）；砍了反而耦合/越权 |
| `utils/event_bus.py` | SSE 事件总线（进程内线程安全）；SSE 协议层，铁律允许保留 |
| `run_server.proxy_api`(1518) | 单源网关（SPA+Agent 同 origin）；架构必需，非坏解耦 |
| `utils/mcp_bridge.py` + `mcp_client.py` | 转换+事件 vs 传输，职责不同且非转发（mcp_bridge 有真实 transform+慢调用事件）；保留 |
| `agent/` 整体 | LangGraph 状态机，多文件但**内聚**（graph/prompts/tools/state/driver 各司其职）；**write_flow.py(1397) 是 god-file 但强行拆会制造过度解耦，建议留**；仅把其中硬编码阈值迁 rules.py（组 W） |
| `profile_db.py` + `user_provision.py` | 合后>800 且职责不同（画像/令牌 vs 开通），留（P2 边界） |

---

## 验收对照（第二阶段完成）

| 验收项 | 体检结论 | 实际结果 |
|---|---|---|
| #1 最深链 ≤3 层 | 仅 A1#1 超（settlement→deduction） | ✅ 组 H 合并后全 ≤3 |
| #2 纯转发层归零 | card_service 3 处薄包装 | ✅ 组 K 后消失；proxy 标注为必要网关 |
| #3 改转账限额只改 rules.py | 无 rules.py，阈值散落 | ✅ 组 W 建 rules.py 单源（含 `TRANSFER_DAILY_LIMIT` 预留） |
| #4 重复实现清零 | DB 连接 12 处 / 留痕内联 6 处 / 轨道判定双份 | ✅ R 收口连接、C 收口留痕、T 收口轨道判定；verify vs paypass 锁语义**报告后保留两份** |
| #5 行为零变化 | — | ✅ 每组只读基线 IDENTICAL + 8 套回归全绿 |
| #6 一域一文件 | card/subscription/transfer 各拆多文件 | ✅ `card.py` / `subscription.py` / `transfer.py` / `wealth.py` 单文件 + 兼容壳 |
| #7 CONTRIBUTING 六条 | 无 | ✅ 写入 `README §11`（架构约束六条 + 惰性 import 标准做法） |
| #8 依赖无新增 | — | ✅ 纯结构，未新增第三方依赖 |

---

## 第二阶段完成总表（行为零变化）

> 顺序 **R → V → K → H → S → T → C → W → C+ → P → D**（C/W 为全局收口先做，C+/P 为追加路由抽取，D 为收尾清理）。
> 每组独立验证：`db_snapshot restore` → `capture_readonly` → `diff_captures vs golden` → `run_regression`（8 套）。

| 组 | 内容 | main.py 行数 | 只读基线 | 8 套回归 |
|---|---|---|---|---|
| 起始 | — | 2541 | — | — |
| R | DB 连接全收口 `db.get_conn`（7 模块 + run_server×8） | ~2540 | —（golden 建于 post-R） | ALL GREEN |
| V | 删 main 死码 `VERIFY_CODES` | ~2490 | IDENTICAL | ALL GREEN |
| K | `card_validator` + `card_service` → `utils/card.py` | ~2490 | IDENTICAL | ALL GREEN |
| H | `deduction_engine` → `settlement_engine` | 2477 | IDENTICAL | ALL GREEN |
| S | 订阅路由 + `subscription_detector` → `utils/subscription.py` | 1905 | IDENTICAL | ALL GREEN |
| T | `transfer_resolver` + `/api/transfer` → `utils/transfer.py` | 1672 | IDENTICAL | ALL GREEN |
| C | 6 处内联留痕 → `db.log_operation` | 1672 | IDENTICAL | ALL GREEN |
| W | 建 `rules.py`（业务常量单源；含死码阈值核实） | ~1668 | IDENTICAL | ALL GREEN |
| C+ | 卡片路由 → `utils/card.py`；新增 `utils/deps.py` | 1260 | IDENTICAL | ALL GREEN |
| P | 理财路由 → `utils/wealth.py` | **1012** | IDENTICAL | ALL GREEN |
| D | 死代码清理（删 3 壳 + 36 行；详见组 D） | 1011 | IDENTICAL | ALL GREEN |

**只读基线一致性总表**（`diff_captures.py` vs `golden_readonly.json`，忽略 5 个环境相关面
`sse_chat_stream, _meta, /api/operation_logs, /api/inbox, /api/admin/logs`）：

| 组 | V | K | H | S | T | C | W | C+ | P | D |
|---|---|---|---|---|---|---|---|---|---|---|
| 结果 | IDENTICAL | IDENTICAL | IDENTICAL | IDENTICAL | IDENTICAL | IDENTICAL | IDENTICAL | IDENTICAL | IDENTICAL | IDENTICAL |

**8 套回归 golden（每组复跑均一致）**：crossbank 41/41、insite 27/27、account_system ALL GREEN、
card_apply 66/66、internal 26/26（环境偶发）、subscription 29/0、realname 7/7、card ALL GREEN。

**⚠️ 环境相关面（非回归）**
- `/api/operation_logs`、`/api/inbox`、`/api/admin/logs`：append-only 日志/通知表，被**捕获过程自身的
  SSE·agent 动作**追加行 → 自增 id 不可复现，diff 时按子串忽略。
- `internal`（`test_internal_transfer_flow`）：走 LangGraph 对话管线，`bank.db` checkpoint 偶发
  `attempt to write a readonly database` → 环境偶发失败，不计回归（preR golden 恰为环境失败态 20 FAIL）。

**⚠️ 保留的不一致（均已报告，未自行取舍）**
1. `verify_codes`（错 3 次**作废码**）vs `paypass`（错 3 次**锁账户**至 23:59:59）：语义不同 →
   两份常量独立保留（`rules.py` 内 `VERIFY_CODE_MAX_ATTEMPTS` / `PAYPASS_MAX_ATTEMPTS`）。
2. `ACTION_SPECS[*]["need_verify"]`（transfer `>5000`）是**死代码**，与实际生效的 `_verify_queue`
   （transfer `>2000`）冲突 → **已报告**，按**活逻辑**迁 `rules.py`，死 lambda 保留并标注（见组 W）。
3. `TRANSFER_DAILY_LIMIT = 50000` 仅**预留常量 + 注释**，未加调用（拍板：下轮启用）。

---

## 交付物清单

- 代码：`utils/{card,subscription,transfer,wealth,deps}.py` 新建/扩写；`rules.py` 业务常量单源；
  `card_validator/card_service/subscription_detector/deduction_engine/transfer_resolver.py` 降兼容壳。
- 文档：`README §11`（约束六条 + 惰性 import 标准做法 + 验证协议）；本 `audit_coupling.md`（各组证据 + 总表）。
- 基线：`tests/baseline/`（DB 快照 + 只读形态 + 8 套回归 + diff/回归脚本）。

