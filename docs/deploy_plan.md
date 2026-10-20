# 银行助手（bank-agent）拆分部署方案

> 目标形态：Kubernetes 编排，三业务容器 + 内部有状态 MySQL，入口经反向代理。
> 状态：方案评审稿（2026-10-15），尚未落地任何配置文件。
> 前置结论：**可以拆，且代码改动极小**——跨模块调用已由环境变量参数化、前端全用相对路径 `/api`。

---

## 0. 为什么能拆（可行性依据）

代码层已存在两条"裂缝"，拆分几乎不碰业务逻辑：

1. `run_server → mock_bank` 是 HTTP 调用，地址由 `config.py` 的 `BANK_API_HOST / BANK_API_PORT / BANK_API_BASE` 驱动（当前 `.env` 为 `127.0.0.1:8003`）。容器化后把 host 改成 K8s Service 名即可，**零代码改动**。
2. 前端 SPA（`web/index.html / admin.html / login.html`）全部用相对路径 `/api/...` 调后端（已 grep 验证，无 `127.0.0.1/localhost/:8178` 写死）。入口反代后**前端零改动**。

---

## 1. 目标拓扑

```
                        客户端浏览器
                            │
                     [Ingress / nginx 入口]
                      /api/* → backend
                      /*     → 静态 SPA (web/)
                            │
        ┌───────────────────┼───────────────────────┐
        ▼                   ▼                        ▼
   [backend]          [mock-bank]              [fay 数字人]
   run_server:8178    :8003 银行核心           Fay(5000)+WS(10002)
   Agent+LangGraph    FastAPI                 + Live2D(5173)
        │  HTTP(BANK_API_BASE)                  ▲
        └──────────────► mock-bank              │ FAY_API_URL / FAY_WS_URL
        │                                        │
        └──────────── 共享同一 MySQL ◄───────────┘
                  [mysql · bank_agent]  (StatefulSet + PVC)
```

- **三业务容器**：`backend`、`mock-bank`、`fay`（按本次确认）。
- **前端静态 SPA**：不作为独立"模块容器"，由入口 nginx 直接 serve（最干净，前端不需改）；若坚持塞进某容器，可并入 `backend`（保留现有 `StaticFiles` 挂载）。
- **MySQL**：K8s 内有状态服务（StatefulSet + PVC），三业务容器共同连接同一实例。
- **bank.db（LangGraph checkpoint, SQLite）**：只挂在 `backend`，单副本 + PVC。

---

## 2. 各容器职责与端口

| 容器 | 进程 / 入口 | 端口 | 依赖 | 有状态？ |
|------|------------|------|------|---------|
| backend | `python run_server.py`（uvicorn :8178） | 8178 | MySQL、mock-bank(HTTP)、DeepSeek、Fay(可选)、bank.db | 是（bank.db、知识库在线编辑） |
| mock-bank | `uvicorn mock_bank.main:app --port 8003` | 8003 | MySQL | 否（连共享 MySQL） |
| fay | Fay flask_server(5000) + WS(10002) + Live2D(5173) | 5000/10002/5173 | backend 被动被调 | 否（但工程在仓库外） |
| mysql | MySQL 8.0 | 3306 | 无 | 是（PVC） |

> 注意：`run_server.py` 第 39 行 `from mock_bank import db`——backend 与 mock-bank 是**同一套代码**，共享连接池逻辑且都连同一个 `bank_agent` 库。拆成两容器后各自连同一 MySQL 即可；这印证"它们本是一体"，拆分靠 HTTP 边界而非代码边界。

---

## 3. 镜像构建策略

- **backend 与 mock-bank 共用同一份基础镜像**（同一 repo、同一 `requirements.txt`），仅靠启动 CMD 不同区分：
  - backend: `CMD ["python","run_server.py"]`
  - mock-bank: `CMD ["python","-m","uvicorn","mock_bank.main:app","--port","8003"]`
- 基础镜像：`python:3.13-slim` + `requirements.txt`（`fastapi/uvicorn/pymysql/openai/requests/python-dotenv/langgraph/langgraph-checkpoint-sqlite/mcp`）。checkpointer 替换后按需加 `langgraph-checkpoint-postgres`（+`psycopg[binary]`）或 `langgraph-checkpoint-redis`（+`redis`）；dev 仍保留 sqlite。
- **fay 单独镜像**：构建 context 指向 `agaent/fay`（仓库外），需先 `npm build` Live2D 子工程（`fay/mate-human/...`），产物随镜像或挂空卷。
- **不要进镜像的东西**（写 `.dockerignore`）：`.env`、`.venv`、`bank.db*`、`__pycache__`、`*.bak`、`_reports`、`scripts/_*.js`（测试用）、`.git`。
- **构建上下文**：`bank-agent` 不是 git 仓库（删除不可逆），镜像用本地目录 `COPY`，**不能 `git clone`**。

---

## 4. 环境变量与密钥清单（重点）

### backend 容器
```dotenv
ENV=production
DEEPSEEK_API_KEY=<secret>            # K8s Secret
DB_HOST=mysql                        # K8s Service 名
DB_PORT=3306
DB_USER=root
DB_PASSWORD=<secret>                 # K8s Secret
DB_NAME=bank_agent
BANK_API_HOST=mock-bank              # K8s Service 名
BANK_API_PORT=8003
BANK_API_BASE=http://mock-bank:8003
BANK_ALLOW_ANON=0
AUTH_SECRET=<统一强随机>             # ★ 三容器必须一致，否则 token 跨容器 401
ADMIN_PASSWORD=<强密码>              # K8s Secret
FAY_API_URL=http://fay:5000
FAY_FRONTEND_URL=http://fay:5173
FAY_WS_URL=ws://fay:10002
```
### mock-bank 容器
```dotenv
DB_HOST=mysql
DB_PORT=3306
DB_USER=root
DB_PASSWORD=<secret>                 # 与 backend 同一 MySQL 凭据
DB_NAME=bank_agent
BANK_ALLOW_ANON=0
AUTH_SECRET=<统一强随机>             # ★ 必须 = backend 的 AUTH_SECRET（current_uid 验 token）
```
### fay 容器
- Fay 被动被 backend 调，自身基本无需连 DB；按 Fay 源码配置 WS 端口与数字人资源路径即可。
- 关键：backend 用 `FAY_API_URL=http://fay:5000`、`FAY_WS_URL=ws://fay:10002` 才能连通。

> `config.py` 在 `ENV=production` 时会**拒绝使用默认 AUTH_SECRET / 默认管理员密码**（`validate_auth_config()`）。上线前必须在 Secret 里覆盖 `AUTH_SECRET` 与 `ADMIN_PASSWORD`，否则启动直接失败——这是预期的安全闸门，不是 bug。

---

## 5. 反向代理 / 路由规则（入口 nginx 或 Ingress）

```nginx
# 静态 SPA（web/ 构建产物）由入口直接 serve
location / {
    root /usr/share/nginx/html;
    try_files $uri /index.html;
}
# 所有 /api 走 backend（前端相对路径天然命中）
location /api/ {
    proxy_pass http://backend:8178;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
# Live2D 数字人（若独立暴露）
location /digital-human/ {
    proxy_pass http://fay:5173;
}
# Fay WebSocket（口型/语音帧推送）必须转发 Upgrade 头
location /ws/ {
    proxy_pass http://fay:10002;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```
- mock-bank 的 `/api` **不对外暴露**，仅被 backend 经服务名内部调用。
- 若保留 backend 的 `StaticFiles` 挂载（第 1114 行），入口 nginx 优先 serve 静态即可，backend 的静态挂载可去掉以减小镜像职责。

---

## 6. 雷区清单（必读，决定怎么拆才稳）

1. **共享 MySQL 是命门**：backend 与 mock-bank 都连同一 `bank_agent` 库。两容器 `DB_HOST` 必须指向**同一个 MySQL Service**，绝不能各带一个库，否则数据分裂。`init_db.py` / 迁移台账只在该库跑一次（用 Init Job，不要在多副本里并发建表）。
2. **bank.db（SQLite checkpoint）必须替换，不能靠 PVC 单副本兜底**：只有 agent 链路用，但 SQLite 不支持多进程并发写，且 `run_server` 按 thread_id 每会话 `sqlite3.connect(bank.db)`（driver.py:54）——多副本即多进程写同一文件→锁冲突/损坏。这是 backend 只能单副本的深层根因。**推荐线上替换为共享 checkpointer（Postgres 或 Redis，见 §6.2）**，替换后 backend 即可多副本、去掉 bank.db PVC。本地 dev 仍可保留 SQLite。
3. **AUTH_SECRET 三处一致**：token 跨容器要能互相验。用同一个 K8s Secret 注入 backend 与 mock-bank（及任何验 token 的组件），否则登录态全废。
4. **非 git 仓库**：镜像用本地 context `COPY` 构建，不能 `git clone`；`.env/.venv/bank.db` 严禁进镜像（`.dockerignore` + Secret/PVC）。
5. **知识库在线编辑要持久化**：后台「知识库」页会写 `docs/knowledge/*.md`，`data/appearance.json` 也会被改。若只读 COPY 进镜像，容器重建即丢。需用 PVC / 挂载卷 / ConfigMap 持久化这两个目录（只读副本不够）。
6. **启动顺序与就绪探针**：backend 启动时会探活 mock-bank（`_bank_health`）并连 MySQL。K8s 用 `initContainers` 或 `readinessGates` 等依赖就绪（mysql 先起 → mock-bank 就绪 → backend 起）。
7. **Fay 在仓库外 + WS**：fay 工程位于 `agaent/fay`（非 bank-agent 仓库），构建 context 要单独指向；Live2D 需 `npm build`；WS 10002 必须透传 `Upgrade`/`Connection` 头，否则数字人收不到语音帧。
8. **水平扩容边界**：`mock-bank`（无状态，连共享 MySQL）可多副本；`web 静态` 可多副本；`backend` 因 bank.db 只能 1 副本；`mysql` 用 StatefulSet（主从/单实例按需求）。
9. **CORS/跨域**：前端走入口同源反代则无需 CORS；mock-bank 自带 `CORSMiddleware` 可保留作兜底。
10. **密钥与对外暴露**：`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`、`DB_PASSWORD`、`AUTH_SECRET`、`ADMIN_PASSWORD` 全部走 K8s Secret，绝不写进镜像层或明文 ConfigMap。



## 6.2 bank.db 替换方案（SQLite → Postgres / Redis）

**现状**：checkpointer 仅在两处真正创建实例 —— `agent/driver.py:54` 与 `agent/main_graph.py:559`（兜底）；`read_agent.py` / `write_flow.py` 为裸编译、由主图统一持有。而 `run_server` 按 thread_id 缓存 `MainDriver`（每会话一个），即每会话 new 一个 checkpointer 连接 —— 这是 bank.db 只能单副本的深层根因，不是简单的「挂 PVC」能解决。

**硬约束**：LangGraph 官方 checkpointer 仅 `SqliteSaver` / `PostgresSaver` / `RedisSaver` 三种，**无 MySQL**。现有 `bank_agent`(MySQL) 业务库不能当后端。要线上并发共享、多副本，必须引入 PostgreSQL 或 Redis。

**改造步骤（改动极小，只动 2 处创建 + 1 个新模块）**：
1. 新增 `agent/checkpointer.py`：进程级单例 `get_checkpointer()`，按 env `CHECKPOINTER_TYPE`（postgres/redis/none→sqlite 兜底）与 `CHECKPOINTER_URI` 返回对应 Saver；**所有会话共用同一实例 + 一个连接池**（取代每会话 new，连接数从「会话数×N」压成 Pod 级 1 个池）。
2. `agent/driver.py:54` 与 `agent/main_graph.py:559` 两处 `SqliteSaver(sqlite3.connect(...))` 改为调用 `get_checkpointer()`。
3. `config.py` 加 `CHECKPOINTER_TYPE` / `CHECKPOINTER_URI`；`requirements.txt` 加 `langgraph-checkpoint-postgres`（+`psycopg[binary]`）或 `langgraph-checkpoint-redis`（+`redis`）。
4. 进程启动跑一次 `PostgresSaver.setup()` / `RedisSaver.setup()`（幂等建表/索引），用 Init 或启动时幂等执行。
5. env 注入 `CHECKPOINTER_URI`（共享 PG/Redis 连接串，K8s Secret）。

**选型（2026-10-15 定：PostgreSQL）**：
- ✅ **PostgreSQL（已选）**：官方一等公民、`setup()` 自动建表、事务一致最稳。代价：多一个 PG 组件（云 RDS PG，或与现有 MySQL 同机/同集群并行；建议独立库 `bank_agent_cp` 或独立 schema，不混业务数据）。
- ◻ **Redis（备选）**：仅在「不想加 PG 组件」时考虑。天然并发共享、快；代价：checkpoint 持久化依赖 Redis AOF/RDB 配置，且 `langgraph-checkpoint-redis` 与当前 langgraph 1.2.11 兼容需本地先验。

**迁移**：checkpoint 只是会话中间态（thread state），线上全新部署**无需迁移**，旧 `bank.db` 本地备份即可；若要保留历史会话才需一次性脚本读 sqlite checkpoints 表转写 PG（通常不必要）。

**PostgreSQL 具体化（已选型，落地时照此实施）**：
- 依赖：`langgraph-checkpoint-postgres`（选与 langgraph 1.2.11 兼容版本，先本地 `pip install` 验可装）+ `psycopg[binary]`（或 `psycopg2-binary`）。
- 连接串：`postgresql://cp_user:cp_pass@checkpointer-pg:5432/bank_agent_cp`（`checkpointer-pg` 为 K8s Service 名；独立库，不与业务 `bank_agent` 混）。
- 建表：进程启动幂等跑 `PostgresSaver.setup()`（新版为 `async`；用 `asyncio.run(...)` 或在启动协程里执行一次）；Init Job 或启动时 try/except 幂等执行。
- `agent/checkpointer.py` 单例要点：用 `PostgresSaver.from_conn_string(URI)` 直接拿实例并**进程级长期持有**——注意新版 `from_conn_string` 是 contextmanager，不要 `with` 包裹（否则退出即关连接）；它内部持有连接池，所有 `MainDriver` 共用即把连接数压成 Pod 级 1 个池。
- config 新增：`CHECKPOINTER_TYPE=postgres`、`CHECKPOINTER_URI=<连接串>`（K8s Secret）。

**收益**：backend 可多副本 + HPA；去掉 bank.db PVC；彻底解除「单副本」限制。

---

## 7. K8s 资源建议（概要，非落地文件）

| 资源 | 类型 | 要点 |
|------|------|------|
| mysql | StatefulSet + Service + PVC | 单实例或主从；PVC 持久化 `bank_agent` 数据；Init Job 跑 `init_db.py` |
| backend | Deployment(可多副本) + Service | checkpointer 连共享 PG/Redis（见 §6.2）；PVC 挂 `docs/knowledge`、`data/`；就绪探针探 `/api/health`(经 mock-bank) |
| mock-bank | Deployment(可多副本) + Service | 无状态，连 mysql |
| fay | Deployment + Service | 容器端口 5000/10002/5173；WS 需 Service + Ingress 透传 Upgrade |
| nginx/ingress | Ingress + 可选 nginx Deployment | 路由见 §5 |
| 配置 | ConfigMap | 非密 env（DB_HOST、BANK_API_HOST、端口等） |
| 密钥 | Secret | DEEPSEEK_API_KEY、DB_PASSWORD、AUTH_SECRET、ADMIN_PASSWORD、DASHSCOPE_API_KEY |

> `backend` 的就绪探针只需探 mock-bank 与 mysql 可达 + 进程存活；checkpointer(PG/Redis) 不可达应视为不健康（但别卡在 bank.db 文件写入）。

---

## 8. 分阶段落地步骤（建议先在 compose 验证再上 K8s）

**阶段 0 — 本地 docker-compose 验证连通性（降风险）**
1. 写 `Dockerfile`（backend/mock-bank 共用）、`docker-compose.yml`（mysql + backend + mock-bank + nginx，fay 可后补）、`nginx.conf`、`.dockerignore`。
2. 本地 `docker compose up`：验证 backend 能经 `mock-bank` 服务名调通、前端相对 `/api` 经 nginx 命中、共享 mysql 数据一致。
3. 验证 bank.db 挂 volume 后重建不丢 checkpoint。

**阶段 1 — 转 K8s manifest**
4. 把 compose 的 service/volume 映射为 Deployment/StatefulSet/Service/PVC/Ingress。
5. 非密 env 提 ConfigMap，密 env 提 Secret（从本地 `.env` 抽取，**不提交明文**）。
6. 加 Init Job 跑 `init_db.py`（仅一次，幂等）。

**阶段 2 — 上线**
7. 先上 mysql + mock-bank + backend（核心三件套），nginx 入口暴露。
8. 验证端到端（登录 / 聊天 / 转账 / 后台 / 知识库在线编辑持久化）。
9. 二期补 fay 数字人容器（仓库外构建，WS 透传）。

---

## 9. 备份 / 回滚 / 数据

- **MySQL**：PVC + 定期 `mysqldump` 或 K8s 卷快照；这是唯一真值库，优先级最高。
- **bank.db**：仅在本地 dev / 尚未替换 checkpointer 时存在；线上替换为 PG/Redis 后无此文件，无需挂载/备份。替换前若临时用 SQLite，上线前先 `cp` 一份本地备份（当前 `bank-agent/bank.db` 108MB，含 checkpoint）。
- **知识库 / appearance**：PVC 挂载 `docs/knowledge` 与 `data/`，与镜像解耦。
- **回滚**：镜像用固定 tag（不用 `latest`）；K8s 用 `kubectl rollout undo`，compose 用上一版镜像。
- **不可删**：`bank-agent` 非 git 仓库，删除不可逆。任何 `rm -rf` 类操作前先确认 + 备份。

---

## 10. 待确认 / 待办

- [ ] fay 数字人是否一期就上（工程在仓库外，构建链较长，建议二期）。
- [ ] 前端静态是入口 nginx 直接 serve，还是并入 backend 容器（推荐前者）。
- [ ] MySQL 单实例还是主从（当前数据量单机足够）。
- [ ] 本地是否已有 mysql 数据需导入新 PVC（当前 `DB_PASSWORD=x5` 的本机 3306 数据）。
- [ ] 上线域名 / TLS（Ingress 证书）。
- [ ] **checkpoint 后端选型：PostgreSQL 还是 Redis（见 §6.2）** —— 决定加 PG 组件还是 Redis 组件。

---

*本文件为方案评审稿，落地前需逐条确认 §10。确认后由实施方生成 Dockerfile / docker-compose.yml / nginx.conf / K8s manifest。*
