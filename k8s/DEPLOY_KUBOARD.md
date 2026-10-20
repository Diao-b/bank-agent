# bank-agent 部署手册（Kuboard 图形界面版）

> 面向：一主两从 K3s（v1.36.5），master 2C4G / worker 2C2G×2，跨账号公网互联。
> 全程 **只在 Windows 本地构建镜像**，节点上只执行 `k3s ctr` 导入，**绝不安装 Docker**。
>
> 预计耗时：准备镜像 25 分钟（含上传）＋ Kuboard 操作 20 分钟 ＋ 首次初始化 5 分钟。

---

## 部署总览（照着这个顺序走）

| 步 | 做什么 | 在哪 | 文档 |
|---|---|---|---|
| 1 | 装 Docker Desktop | Windows | §1.1 |
| 2 | 生成 `AUTH_SECRET` / `DB_PASSWORD` | 本地终端 | §1.2 |
| 3 | 构建并导出两个 tar | 本地终端 | §1.3 |
| 4 | 三台节点导入镜像 | master + 2 worker | §1.4 |
| 5 | 建命名空间 `bank-agent` | Kuboard | §2 |
| 6 | 改 Secret 的 4 个占位符 | 本地文件 | §3.1 |
| 7 | 导入 `00`（ConfigMap + Secret） | Kuboard | §3.2 |
| 8 | 导入 `10`（MySQL） | Kuboard | §4 |
| 9 | 导入 `40`（建表 Job） | Kuboard | §5 |
| 10 | 导入 `20`（mock-bank） | Kuboard | §6.1 |
| 11 | 导入 `30`（backend） | Kuboard | §6.2 |
| 12 | 导入 `50`（Ingress）→ 浏览器验收 | Kuboard | §7 |

**顺序不能调换的只有三处**：镜像（4）必须早于所有 Pod；MySQL（8）必须早于 Job（9）；
Job（9）必须早于 mock（10）和 backend（11）。其余可以并。

---

## 0. 开工前必须知道的 5 件事

| # | 事实 | 影响 |
|---|---|---|
| 1 | MySQL / backend 钉 `k3s-master`；mock-bank 自动调度 | 主节点承载 1.5Gi limits（集群可用 4.72GiB，宽裕） |
| 2 | 镜像要预置在 **master + 两台 worker** | 少一台，mock-bank 调度过去就 ImagePullBackOff |
| 3 | `imagePullPolicy: Never` | 节点上没有镜像不会去拉，只会报错 |
| 4 | `ENV=production` 会校验 Secret | `AUTH_SECRET` / `ADMIN_PASSWORD` 是占位符就拒绝启动 |
| 5 | `40-init-job.yaml` 会执行 `init.sql`（含 11 条 DROP） | **只能对全新空库跑**，已有数据会被清空 |

---

## 1. 准备镜像（Windows 本地）

### 1.1 安装 Docker Desktop

1. 下载安装 Docker Desktop for Windows，安装时勾选 **Use WSL 2**。
2. 安装完**重启电脑**，打开 Docker Desktop，等左下角显示 Engine running。
3. 打开 Git Bash 验证：

```bash
docker version
```

看到 `Server: Docker Engine` 段落才算成功（只有 Client 不行）。

### 1.2 生成两个密钥（先记下来）

```bash
cd /d/Users/Dwm/Desktop/agaent/bank-agent
./.venv/Scripts/python.exe -c "import secrets;print('AUTH_SECRET  =',secrets.token_hex(32))"
./.venv/Scripts/python.exe -c "import secrets;print('DB_PASSWORD  =',secrets.token_urlsafe(18))"
```

把这两个值存到记事本，第 3 步要用。

### 1.3 构建并导出

```bash
cd /d/Users/Dwm/Desktop/agaent/bank-agent
bash k8s/build-and-import.sh
```

脚本会自动：同步 `../docs/knowledge` → 构建 → 校验架构是 amd64 → 校验 `/docs/knowledge` 非空 → 校验 `.env`/`bank.db` 没进镜像 → 导出两个 tar。

**期望输出**（末尾）：

```
dist/bank-agent-1.0.tar   约 450MB
dist/mysql-8.0.tar        约 600MB
```

> 中途报 `docs/knowledge 是空的` → 说明同步失败，检查 `agaent/docs/knowledge` 下是否有约 10 个 `.md`。
> 报 `架构是 arm64` → 加 `--platform linux/amd64` 重跑（脚本已带，通常不会）。

### 1.4 上传并导入镜像

**① master（两个包都要）**

```bash
scp dist/bank-agent-1.0.tar dist/mysql-8.0.tar root@118.31.61.128:/root/

ssh root@118.31.61.128
sudo k3s ctr -n k8s.io images import /root/bank-agent-1.0.tar
sudo k3s ctr -n k8s.io images import /root/mysql-8.0.tar
sudo k3s ctr -n k8s.io images ls -q | grep -E 'bank-agent|mysql'
```

**② 两台 worker（只要主镜像）**

```bash
# 在本地分别对两台 worker 执行
scp dist/bank-agent-1.0.tar root@<worker1公网IP>:/root/
ssh root@<worker1公网IP> 'sudo k3s ctr -n k8s.io images import /root/bank-agent-1.0.tar'

scp dist/bank-agent-1.0.tar root@<worker2公网IP>:/root/
ssh root@<worker2公网IP> 'sudo k3s ctr -n k8s.io images import /root/bank-agent-1.0.tar'
```

**③ 校验三台都到位**

```bash
# 每台都执行，输出里必须能看到 bank-agent:1.0
sudo k3s ctr -n k8s.io images ls -q | grep bank-agent
```

> 如果 `k3s ctr` 报 `permission denied`，命令前加 `sudo`；
> 若 `-n k8s.io` 里看不到镜像，试 `sudo k3s ctr -n k3s.io images ls | grep bank-agent`。

### 1.5 删除临时文件（可选，省磁盘）

```bash
# 在每台机器上
rm -f /root/bank-agent-1.0.tar /root/mysql-8.0.tar
```

---

## 2. 在 Kuboard 创建命名空间

1. 浏览器打开 Kuboard（master 上那个地址）。
2. 左侧进入你的集群。
3. 左侧菜单找到 **命名空间**（或「Namespaces」）。
4. 右上角 **创建**，名称填：`bank-agent`。
5. 提交。

> 找不到入口？在 master 上执行：
> `k3s kubectl create namespace bank-agent`

---

## 3. 导入配置（Secret + ConfigMap）

### 3.1 先改文件里的 4 个占位符（重要）

打开 `k8s/00-namespace-config-secret.yaml`，找到 `stringData:`，替换：

```yaml
stringData:
  DB_PASSWORD: "<1.2 生成的 DB_PASSWORD>"
  AUTH_SECRET: "<1.2 生成的 AUTH_SECRET>"
  ADMIN_PASSWORD: "<自定义管理员密码，不能是 admin123>"
  DEEPSEEK_API_KEY: "sk-xxxxxxxx"        # 你的真实 key
  DASHSCOPE_API_KEY: ""                  # 没有就留空
```

> `DB_PASSWORD` 只有这一个来源：MySQL 的 `MYSQL_ROOT_PASSWORD` 也用 `secretKeyRef`
> 从同一个 key 取，所以**不要**在这里加第二个密码字段。

### 3.2 通过 Kuboard 导入

1. 顶部切换到命名空间 `bank-agent`。
2. 左侧菜单 **配置中心 / 配置** → 进入 ConfigMap 或 Secret 列表均可。
3. 右上角 **创建** → 选择 **使用 YAML 创建**（有的版本叫「导入 YAML」）。
4. 把 `00-namespace-config-secret.yaml` **全文**粘进去 → 提交。

**如果报错 `namespace "bank-agent" already exists` 或不允许创建命名空间**：
把文件开头那段 `kind: Namespace`（第 1 个 `---` 之前的内容）删掉，再粘一次。
第 2 节已经手工建过命名空间了。

> **如果 Kuboard 提示"一次只能创建一个资源"**（部分版本的多文档 YAML 支持有限）：
> 按 `---` 把文件拆开逐个粘。00 文件里有 4 段：Namespace、ResourceQuota、ConfigMap、Secret。
> 或者干脆走命令行（在 master 上，效果完全一样）：
> `k3s kubectl apply -f k8s/00-namespace-config-secret.yaml`

### 3.3 核对

- 配置中心里应出现 `bank-config`（ConfigMap）
- 配置中心里应出现 `bank-secret`（Secret）
- 打开 `bank-secret`，确认 4 个值不是 `CHANGE_ME_*`

> 命令行等价：
> `k3s kubectl apply -f k8s/00-namespace-config-secret.yaml`

---

## 4. 部署 MySQL

### 4.1 导入

1. 命名空间 `bank-agent` → 左侧 **工作负载** → **有状态副本集（StatefulSet）**
2. 右上角 **创建** → **使用 YAML 创建**
3. 粘贴 `10-mysql.yaml` 全文 → 提交

这一步会一并创建：2 个 Service（`bank-mysql`、`bank-mysql-headless`）、1 个 StatefulSet、1 个 5Gi PVC。

### 4.2 观察启动

左侧 **工作负载 → 容器组（Pod）**，应看到 `bank-mysql-0`：

- `ContainerCreating`：正在拉卷，正常
- `Running` 但 `Ready 0/1`：MySQL 正在 `mysqld --initialize`，首次要 1–2 分钟
- `Running` 且 `Ready 1/1`：完成

点进 Pod → **日志**，看到 `ready for connections` 即可。

### 4.3 验证数据库

Pod 详情 → **终端（Terminal）**，执行：

```bash
mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SHOW DATABASES;"
```

应看到 `bank_agent` 库（虽然还是空的）。

> 此时 `bank_agent` 里没有业务表 —— 下一步才是建表。

**如果 Pod 一直 Pending**：点 Pod → **事件**，看是不是
`volume node affinity conflict`。那说明 local-path 的 PVC 绑到了别的节点。
处置：删掉 PVC 和 StatefulSet 重来，并确认 StorageClass 是 `WaitForFirstConsumer`：

```bash
k3s kubectl get storageclass local-path -o jsonpath='{.volumeBindingMode}'
```

---

## 5. 初始化数据库（只跑一次）

### 5.1 导入 Job

1. 左侧 **工作负载** → **任务（Job）**
2. 右上角 **创建** → **使用 YAML 创建**
3. 粘贴 `40-init-job.yaml` 全文 → 提交

> ★ Job 的 spec 不可变。**如果之前跑过**，要先在 Kuboard 里删除旧 Job，再重新导入。

### 5.2 观察结果

Job 状态从 `Running` → `Completed`（约 10–30 秒）。

点 Job 的 Pod → **日志**，正确输出应该长这样：

```
==> 前置检查：确认是空库
schema_migrations 不存在 → 空库，继续初始化
==> 执行 init_db.py（init.sql + 未应用的 upgrade_*.sql）
...
迁移台账 schema_migrations：已应用 N 条
==> 初始化完成
```

（`N` 是实际应用的迁移数，只要能走到「初始化完成」就对了。）

若看到 `REFUSE: schema_migrations 已有记录` → 说明这个库不是空的，Job 主动拒绝了（保护数据）。这时**不要**强行重跑，先确认你是不是连错了库。

### 5.3 验证表已建好

MySQL Pod 终端：

```bash
mysql -uroot -p"$MYSQL_ROOT_PASSWORD" bank_agent -e "SHOW TABLES;" | head -30
```

应看到 `users`、`accounts`、`cards`、`transactions`、`products`、`holdings`、`merchants`、`subscription_plans`、`chat_messages`、`schema_migrations` 等。

---

## 6. 部署业务服务

### 6.1 mock-bank（先起它）

1. **工作负载 → 部署** → 创建 → 使用 YAML 创建
2. 粘贴 `20-mock-bank.yaml` 全文 → 提交

它没有 `nodeSelector`，调度器自己选节点。**记下它落在哪台**：容器组列表里有「节点」列。

> 为什么先起它：backend 的 initContainer `wait-mock` 会轮询 mock-bank 的 `/api/health`，
> mock 不起来，backend 会一直卡在 `Init:1/2`。

### 6.2 backend（托管前端）

1. **工作负载 → 部署** → 创建 → 使用 YAML 创建
2. 粘贴 `30-backend.yaml` 全文 → 提交

这一步会一并创建 PVC `bank-backend-data`（1Gi）、Service `bank-backend`、Deployment `bank-backend`。

启动会先跑两个 initContainer：
- `seed-data`：把镜像里的 `/docs/knowledge` 和 `/app/data` 灌进 PVC（只补不覆盖）
- `wait-mock`：等 mock-bank 就绪（现在它已经 Ready，几秒就过）

### 6.3 等待二者 Ready

| Pod | 期望状态 |
|---|---|
| `bank-mock-xxxx` | `Running 1/1` |
| `bank-backend-xxxx` | `Running 1/1` |

backend 首次启动要预热知识库（约 11 秒）＋ `ensure_auth_schema` + 建管理员账号，给 1–2 分钟。

**如果 backend 卡在 `Init:1/2` 超过 3 分钟**：点 Pod → 日志 → 切到 `wait-mock` 容器，
看是不是 `bank-mock` 的 DNS 解析失败（`Name or service not known`）。

> 命令行等价：
> `k3s kubectl apply -f k8s/20-mock-bank.yaml -f k8s/30-backend.yaml`

---

## 7. 配置 Ingress 入口

### 7.1 导入

1. 左侧 **网络** → **路由（Ingress）**
2. 创建 → 使用 YAML 创建
3. 粘贴 `50-ingress.yaml` 全文 → 提交

规则：`/api` 与 `/` 都指向 `bank-backend:8178`。

### 7.2 验证路由

Kuboard 里 Ingress 状态应显示已生效。命令行确认：

```bash
k3s kubectl -n bank-agent get ingress
```

`ADDRESS` 列可能是空的（K3s 的 Traefik 不写回地址），不影响使用。

### 7.3 浏览器验收

打开 `http://118.31.61.128/` —— 应看到登录页。

> **后台管理页是独立文件**：`http://118.31.61.128/admin.html`
> （**不是** `/admin` —— 后端是 `StaticFiles` 托管，不会把 `/admin` 映射到 `admin.html`）。

> 打不开的顺序排查：
> 1. 阿里云安全组是否放行 **80**
> 2. `curl -I http://118.31.61.128/` 看是 404 还是超时
> 3. 404 → Ingress 没生效；超时 → 安全组或 Traefik
> 4. `k3s kubectl -n kube-system get pods | grep traefik`

---

## 8. 全链路验收清单

按顺序点一遍，任何一步失败就按第 9 节排查：

- [ ] 打开 `http://118.31.61.128/` 出现登录页（不是 502/404）
- [ ] 注册一个新用户成功
- [ ] 登录成功，进入 `#/chat`
- [ ] 在聊天里问「我的余额是多少」，能返回数字（说明 backend → mock-bank → MySQL 全通）
- [ ] 让 AI 查「最近三个月支出」，能返回账单（说明 MySQL 有数据、RAG 正常）
- [ ] 打开 `#/cards`，申请一张卡（会要求实名，按流程走）
- [ ] 打开 **`http://118.31.61.128/admin.html`**（注意是 `.html`，不是 `/admin`），
      用 `admin` + 你设的 `ADMIN_PASSWORD` 登录
- [ ] 后台「知识库」页能看到文档列表（约 10 篇，**这条验证 `/docs/knowledge` 挂载正确**）
- [ ] 在后台改一个知识库文档并保存，然后**重启 backend Pod**，再看文档还在（验证 PVC 持久化）
- [ ] 后台「订阅套餐」页能看到 4 个商户（爱奇艺/腾讯视频/网易云音乐/美团外卖会员）
- [ ] 发一条转账（可用两个账号互转），成功后查流水
- [ ] 数字人功能先不开（一期未部署 fay）

---

## 9. Kuboard 里怎么排错

| 想看什么 | Kuboard 位置 |
|---|---|
| Pod 为什么起不来 | 容器组 → 点 Pod → **事件**（Events） |
| 应用报错 | 容器组 → 点 Pod → **日志**，可切容器 |
| 进容器敲命令 | 点 Pod → **终端** |
| 资源用量 | 集群 → 概览；或 Pod → 监控 |
| 配置对不对 | 配置中心 → ConfigMap / Secret → 点开看 |
| 卷绑到哪 | 存储 → 持久卷声明（PVC）→ 看「节点亲和」 |

**三个最常踩的坑**：

1. **`ImagePullBackOff`** → 那台节点没导入镜像。用第 1.4 节的命令补导。
2. **`CreateContainerConfigError`** → Secret 少了 key，或 key 名字拼错。点 Pod 事件会写明。
3. **`CrashLoopBackOff` + 日志 `RuntimeError: 生产环境禁止使用默认`** → Secret 还是占位符。改完 Secret 后要在 Deployment 里点「重新部署」。

---

## 10. 日常操作（都在 Kuboard 里）

**看实时日志**：容器组 → 点 Pod → 日志 → 勾选「自动滚动」。

**重启 backend**：部署 → `bank-backend` → 右上角 **重新部署（Restart）**。

**改配置**：配置中心 → `bank-config` → 编辑 → 保存后需重启相关 Deployment 才生效
（ConfigMap 是启动时注入的环境变量，不是热更新）。

**备份数据库**：MySQL Pod → 终端先跑一次 mysqldump 写到 `/tmp`：

```bash
mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines bank_agent > /tmp/backup.sql
```

Kuboard 终端不支持下载文件，在 master 命令行取回（**用 `exec` 重定向，不要用 `kubectl cp`** ——
MySQL 镜像里通常没有 `tar`，cp 会失败）：

```bash
k3s kubectl -n bank-agent exec bank-mysql-0 -- \
  sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines bank_agent' \
  > backup-$(date +%F).sql
```

**升级代码**：本地 `TAG=1.1 bash k8s/build-and-import.sh` → 三台节点导入 → 
Kuboard 里点 Deployment → 编辑镜像 tag → 重新部署。

**扩容提醒**：backend 现在是 1 副本，**不要改成 2**（PVC 是 RWO，且会话状态在进程内）。

---

## 11. 故障速查

| 现象 | 最可能原因 | 处理 |
|---|---|---|
| Pod `Pending` | 资源不足 / 卷亲和冲突 | 事件里看 `Insufficient memory` 或 `volume node affinity conflict` |
| `ImagePullBackOff` | 节点没镜像 | 补 `k3s ctr images import` |
| `Init:1/2` 卡住 | mock-bank 还没起 | 先部署 mock-bank |
| backend 起来但页面 502 | 绑了 127.0.0.1 | 确认 ConfigMap 里 `SERVER_HOST=0.0.0.0` |
| 知识库页空白 | `/docs/knowledge` 没挂对 | 进 backend 终端 `ls /docs/knowledge` |
| 聊天一直「正在输入」 | SSE 被缓冲 | 检查是否加了 gzip 中间件 |
| 登录 500 | 表没建 | 重看第 5 节 Job 日志 |
| 转账偶发超时 | mock-bank 在 worker，跨节点 MTU | 见 README §4.1 的 `ping -s 1400` 体检 |
| MySQL `OOMKilled` | 768Mi 仍不够（通常是有别的进程抢内存） | 先 `kubectl top node` 看是谁占的；确认后再提到 1Gi |

---

## 12. 回滚

- **代码回滚**：Kuboard → 部署 → `bank-backend` → **版本历史 / 变更记录** → 选上一版回滚。
  界面里找不到就走命令行：`k3s kubectl -n bank-agent rollout undo deploy/bank-backend`
- **全量重来**：删除命名空间 `bank-agent`（会连带删 PVC 与数据），从第 2 节重新开始
- **只重来数据库**：删 StatefulSet + PVC `data-bank-mysql-0`，再走第 4、5 节
