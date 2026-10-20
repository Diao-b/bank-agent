# bank-agent · K3s 一期部署（核心三件套）

> 目标集群：一主两从 v1.36.5+k3s1，master 2C4G（余 ~1.5G），worker 2C2G×2。
> 一期只部署 MySQL + backend + mock-bank，全部**钉在 k3s-master**；fay 数字人与
> PostgreSQL checkpoint 放二期。
> 相关设计背景见 `../docs/deploy_plan.md`。

---

## 1. 文件职责

| 文件 | 内容 |
|---|---|
| `../Dockerfile` | backend / mock-bank 共用的运行镜像（CMD 不同） |
| `../.dockerignore` | 排除 `.env`、`bank.db*`、`.venv`、测试产物（**不能漏**） |
| `00-namespace-config-secret.yaml` | Namespace、ResourceQuota、ConfigMap、Secret |
| `10-mysql.yaml` | MySQL 8.0 StatefulSet + 两个 Service + 5Gi PVC（**master**） |
| `20-mock-bank.yaml` | 银行核心 API Deployment + 内部 Service（**自动调度**，软偏好非 master） |
| `30-backend.yaml` | Agent + 静态 SPA Deployment + Service + 1Gi PVC（**master**） |
| `40-init-job.yaml` | 一次性建库 Job（**会跑 init.sql，含 DROP，仅限空库**；跑在 master） |
| `50-ingress.yaml` | Traefik Ingress：`/api` 与 `/` 都指向 backend:8178 |
| `build-and-import.sh` | 本地 `docker build` → `docker save` 出 tar，并打印手动上传/导入命令 |

> **节点上不装 Docker**。构建只在本机做，节点侧只执行 `sudo k3s ctr ... import`。

---

## 2. 部署前必须做的三件事

### ① 改 Secret（4 个值）

```bash
openssl rand -hex 32      # AUTH_SECRET
openssl rand -base64 24   # DB_PASSWORD（同时给 MySQL root 与两个容器用）
```

编辑 `00-namespace-config-secret.yaml` 的 `stringData`，替换 `CHANGE_ME_*`。

> `ENV=production` 会强制校验：`AUTH_SECRET` 为空或为默认值、`ADMIN_PASSWORD=admin123`
> 都会**直接拒绝启动**。这是设计好的安全闸门，不是 bug。

### ② 确认 local-path 的绑定模式

```bash
k3s kubectl get storageclass local-path -o jsonpath='{.volumeBindingMode}'
```

必须是 `WaitForFirstConsumer`（K3s 默认）。若是 `Immediate`，PVC 可能先绑到 worker，
而 Pod 被 nodeSelector 钉在 master → **永久 Pending**。此时先改成 WaitForFirstConsumer：

```bash
k3s kubectl patch storageclass local-path \
  -p '{"volumeBindingMode":"WaitForFirstConsumer"}'
```

### ③ 构建并导入镜像

**构建只在本机（Windows + Docker Desktop）做，节点上绝不装 Docker。**

> ⚠️ K3s 的运行时是 containerd。在 master/worker 上再装 dockerd，会写入
> iptables NAT 与 docker0 网桥规则，和 flannel(VXLAN) + Traefik 争抢转发链路，
> 轻则 Service 不通、Pod 拿不到 IP，重则整个集群失联。**master 上只用
> `sudo k3s ctr`（containerd 客户端）导入镜像**。脚本会主动拦截误在节点上执行。

```bash
# 0) 本地先装 Docker Desktop（本机目前没有），装完重开终端
docker version

# 1) 构建 + 导出（脚本会同步知识库、校验架构与 /docs/knowledge）
cd bank-agent
bash k8s/build-and-import.sh
#   产物（分开两个包，避免把 600MB 的 mysql 也传到 2G 的 Worker）：
#     dist/bank-agent-1.0.tar   三台机器都要（master + 2 台 worker）
#     dist/mysql-8.0.tar        只有 master 要

# 2) 传到 master 并导入
scp dist/bank-agent-1.0.tar dist/mysql-8.0.tar root@118.31.61.128:/root/
ssh root@118.31.61.128
sudo k3s ctr -n k8s.io images import /root/bank-agent-1.0.tar
sudo k3s ctr -n k8s.io images import /root/mysql-8.0.tar
sudo k3s ctr -n k8s.io images ls -q | grep -E 'bank-agent|mysql'

# 3) ★ 两台 Worker 也都要导入主镜像（mock-bank 是自动调度，可能落任意一台）
for H in <worker1公网IP> <worker2公网IP>; do
  scp dist/bank-agent-1.0.tar root@$H:/root/
  ssh root@$H 'sudo k3s ctr -n k8s.io images import /root/bank-agent-1.0.tar'
done
```

> **三台机器都要有镜像是"自动调度"的必付代价**。mock-bank 没有 nodeSelector，
> 调度器可能把它放到任意一台 worker；那台机器的 containerd 里没有 `bank-agent:1.0`
> 就会 `ImagePullBackOff`，而 `imagePullPolicy: Never` 不会退回去拉。
>
> 如果以后让它只在某台机器跑（加回 nodeSelector），那就只需在那台上导入 —— 但也就
> 等于放弃了自动调度。**预置镜像到所有候选节点，是无状态组件可漂移的前提。**

脚本会做 5 件容易漏的事：
0. 把 `../docs/knowledge`（知识库真身）同步进构建上下文 —— 代码读的是
   **`/docs/knowledge`**，不是 `/app/docs/knowledge`。路径错了不报错，只会表现为
   "知识库空白 + RAG 检索不到任何东西"，极难排查；
1. 校验 `/docs/knowledge` 真的进了镜像；
2. 校验 `.env` / `bank.db` **没有**进镜像；
3. 校验镜像架构是 `amd64`（`--platform linux/amd64`，否则节点报 exec format error）；
4. 把 `mysql:8.0` 与主镜像**拆成两个 tar**（只让 master 传 mysql，省 Worker 的带宽）。

> 公网带宽小的话先压一下再传：`gzip -1 dist/bank-agent-1.0.tar`，
> `ctr images import` 同样认 `.tar.gz`，体积能少 60%。

---

## 3. 部署顺序（顺序不能乱）

```bash
# 0) 确认三台节点都是 Ready（mock-bank 交给调度器选，不指定具体节点名）
k3s kubectl get nodes -o wide

# 1) 命名空间 + 配置 + 密钥
k3s kubectl apply -f k8s/00-namespace-config-secret.yaml

# 2) MySQL（等它 Ready，首次初始化要 1~2 分钟）
k3s kubectl apply -f k8s/10-mysql.yaml
k3s kubectl -n bank-agent rollout status statefulset/bank-mysql --timeout=420s

# 3) 建库 Job（Job spec 不可变，重复执行前先删）
k3s kubectl -n bank-agent delete job bank-db-init --ignore-not-found
k3s kubectl apply -f k8s/40-init-job.yaml
k3s kubectl -n bank-agent wait --for=condition=complete job/bank-db-init --timeout=600s
k3s kubectl -n bank-agent logs job/bank-db-init --tail=40

# 4) 业务两件套 + 入口
k3s kubectl apply -f k8s/20-mock-bank.yaml -f k8s/30-backend.yaml -f k8s/50-ingress.yaml
k3s kubectl -n bank-agent rollout status deploy/bank-mock    --timeout=300s
k3s kubectl -n bank-agent rollout status deploy/bank-backend --timeout=300s

# 5) 验收
k3s kubectl -n bank-agent get pods -o wide
curl -s http://127.0.0.1/api/health          # {"ok":true,"db":"up",...}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/   # 200
```

或者把上面的命令整段保存成 `deploy.sh` 一次执行。**故意不做一键部署**：
Job 的 `init.sql` 带 DROP，每一步都值得亲眼看一眼 Pod 状态再往下走。

---

## 4. 内存账（为什么只能单副本）

> **实测基线（2026-10-18 Kuboard）**：集群 Allocatable **6.94 GiB**，已用 **2.22 GiB**（控制面 +
> Kuboard + 两台 k3s agent + 系统），可用 **4.72 GiB**；CPU 仅 1.5%（0.09/6）。
> 之前按"master 只剩 1.5G"定的参数偏保守，本版已放宽。

**调度分布**：MySQL + backend 钉 master（有状态 / 依赖 PVC）；mock-bank 自动调度，
软偏好非 master，所以有两种可能落点。

常态（mock-bank 落到 Worker）：

| 节点 | Pod | requests | limits | 预估 RSS |
|---|---|---|---|---|
| **k3s-master** | MySQL | 512Mi | 768Mi | 380–480Mi（buffer pool 192M） |
| | backend | 512Mi | 768Mi | 320–500Mi |
| | **小计** | **1.0Gi** | **1.5Gi** | **0.7–1.0Gi** |
| **某台 worker** | mock-bank | 192Mi | 384Mi | 160–240Mi |

**兜底**（两台 Worker 都不可用，mock-bank 回落 master）：master limits 变成 **1.884Gi**。
master 4G 扣除控制面约 1.6G 后仍余 ~0.5G —— 用 `preferred` 而不是 `required` 的意义就在这：
偏好落空不会 Pending，只是稍微挤一点。

底线（这几条不是内存能解决的）：

- backend **仍然只能 1 副本**：1Gi PVC 是 RWO + 会话状态在进程内，跟内存多少无关。
  加内存 ≥ 加副本 这个推论在这里不成立。
- MySQL 首次 `mysqld --initialize` 峰值约 450–500Mi，768Mi 留了余量，不会再 OOMKilled；
  若真出现，说明有别的进程在抢，先看 `kubectl top node`。
- backend 768Mi 是按"聊天高峰 + LLM 重试堆积"给的。如果 SSE 并发很高导致 `OOMKilled`，
  先查 `LLM_MAX_RETRIES` 是否被调大，而不是继续加 limit。
- mock-bank 落哪台，那台 2G 机器就少 384Mi。**worker 上别排第四个 Pod。**

### 4.1 跨节点通信（只在 mock-bank 落到 Worker 时发生）

mock-bank 是自动调度的：**它落在 master 上就是同机回环，几乎零额外开销；落在 worker 上**
才走 flannel VXLAN over 公网。后者要知道两点：

1. **延迟**：每次 backend→mock 调用多 20–50ms。一次转账涉及余额校验 + 扣款 +
   流水，多跳累积到百毫秒级，可接受但不要再叠加跨节点调用。
2. **MTU**：VXLAN 封装要占 50 字节。公网 MTU 1500 时容器内应 ≤1450；
   若公网链路还有额外封装（部分阿里云线路），大包会被静默丢弃，表现非常迷惑 ——
   **小请求（登录、查余额）正常，大响应（账单列表、聊天历史）偶发超时/卡住**。

先在 master 侧确认现状（只读）：

```bash
k3s kubectl -n bank-agent get pods -o wide        # 看 mock-bank 到底落在哪台
k3s kubectl -n bank-agent exec deploy/bank-backend -- sh -c 'ip link | grep -E "eth0|flannel"'
k3s kubectl -n bank-agent exec deploy/bank-backend -- ping -c 3 -s 1400 <mock-bank-pod-ip>
```

最后一条如果 `-s 1400` 丢包、`-s 300` 正常，就是 MTU 问题。处置（需要重启 k3s，
选在低峰）：server 端与 agent 端统一加 `--flannel-mtu=1400`，改动
`/etc/systemd/system/k3s.service` 后 `systemctl daemon-reload && systemctl restart k3s`。

> **不想承担跨节点风险的退路**（二选一）：
> 1. 把 `20-mock-bank.yaml` 的 `affinity` 整段换成
>    `nodeSelector: {kubernetes.io/hostname: k3s-master}` —— 主节点 limits 回到 1.884Gi，
>    但 mock-bank 也就失去了自动调度；
> 2. 保留自动调度，只把 MTU 修对（`--flannel-mtu=1400`），一次性解决。

---

## 5. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `ImagePullBackOff` / `ErrImageNeverPull` | 镜像没导入，或 tag 不一致 | `sudo k3s ctr -n k8s.io images ls -q \| grep bank-agent`；确认与 YAML 里的 tag 完全一致 |
| `exec format error` | 镜像架构不是 amd64（在 ARM 机器上 build 的） | 重新 `docker build --platform linux/amd64` |
| Pod `Pending` | local-path 绑到了别的节点 / 资源不足 | `kubectl describe pod` 看 Events；核对 SC 是否 WaitForFirstConsumer |
| **mock-bank 一直 `Pending`** | 节点全都 `NotReady`，或每台都资源不足 | `kubectl describe pod` 看 Events。注意：`preferred` 偏好不会导致 Pending，出现 `node affinity` 报错说明有人改回了 `required`/`nodeSelector` |
| 小请求正常、大响应（账单/历史）偶发超时 | 跨节点 VXLAN MTU 不匹配 | 见 §4.1 的 `ping -s 1400` 排查 |
| `CrashLoopBackOff`，日志有 `cryptography package is required` | 镜像缺 cryptography | 已写进 `requirements.txt`，重新 build |
| `CrashLoopBackOff`，日志有 `RuntimeError: 生产环境禁止使用默认` | Secret 没改 | 改 `AUTH_SECRET` / `ADMIN_PASSWORD` 后 `rollout restart` |
| Pod Running 但访问 502 | backend 绑了 127.0.0.1 | 确认 `SERVER_HOST=0.0.0.0` 已注入（ConfigMap） |
| 后台「知识库」页空白 | 挂载路径错了，或种子没灌进 PVC | `kubectl exec deploy/bank-backend -- ls /docs/knowledge`（**不是** /app/docs/knowledge） |
| 聊天一直"正在输入" | SSE 被中间件缓冲/压缩 | 检查 Traefik 是否挂了 compress 中间件 |
| MySQL `OOMKilled` | 768Mi 仍不够，通常是有别的进程抢内存 | 先 `kubectl top node` 查占用，再决定提到 1Gi |
| 整个集群网络异常 / Service 不通 | 有人在节点上装了 Docker | `systemctl stop docker && systemctl disable docker && yum remove docker-ce`，然后重启 k3s |

查看日志：`k3s kubectl -n bank-agent logs deploy/bank-backend -f --tail=100`

---

## 6. 日常运维

**升级代码**（新 tag，仍然只在本地构建）：

```bash
TAG=1.1 bash k8s/build-and-import.sh

# master 与 Worker 都要导（backend 在 master，mock-bank 在 Worker）
for H in 118.31.61.128 <worker1公网IP> <worker2公网IP>; do
  scp dist/bank-agent-1.1.tar root@$H:/root/
  ssh root@$H 'sudo k3s ctr -n k8s.io images import /root/bank-agent-1.1.tar'
done

k3s kubectl -n bank-agent set image deploy/bank-backend backend=bank-agent:1.1
k3s kubectl -n bank-agent set image deploy/bank-mock    mock-bank=bank-agent:1.1
k3s kubectl -n bank-agent rollout status deploy/bank-backend
k3s kubectl -n bank-agent rollout status deploy/bank-mock
```

**备份 MySQL**（唯一真值库，优先级最高）：

```bash
k3s kubectl -n bank-agent exec bank-mysql-0 -- \
  sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers bank_agent' \
  > backup-$(date +%F).sql
```

**回滚**：`k3s kubectl -n bank-agent rollout undo deploy/bank-backend`

**卷位置**：`/var/lib/rancher/k3s/storage/` 下（`pvc-*_bank-agent_*`）。
删 PVC 会连带删数据（reclaimPolicy=Delete），**不要手滑**。

---

## 7. 二期改动清单（数字人 + PG）

### 7.1 checkpointer 换 PostgreSQL（backend 才能多副本）

1. `requirements.txt` 加 `langgraph-checkpoint-postgres` + `psycopg[binary]`；
2. 新增 `agent/checkpointer.py`，进程级单例 `get_checkpointer()`；
3. `agent/driver.py` / `agent/main_graph.py` 两处 `SqliteSaver(...)` 改调 `get_checkpointer()`；
4. ConfigMap 加 `CHECKPOINTER_TYPE=postgres`、`CHECKPOINTER_URI=postgresql://...`；
5. 新增 `60-postgres.yaml`（StatefulSet + 1Gi PVC，`shared_buffers=32MB`，limit 192Mi）；
6. 去掉 `/data` 挂载与 `CHECKPOINT_PATH`，backend 才能 `replicas: 2`。

> PG 与 MySQL 都放 master 时，limits 会到 1.5Gi 边缘 —— 届时把 backend limit
> 从 576 降到 512，或把 PG limit 压到 160Mi。

### 7.2 fay 数字人

- **必须放 worker**（2G worker 上限约 900Mi，master 已无余量）；
- headless 启动（`start_mode != common`，跳过 PyQt5/QWebEngine）；
- 镜像需 `libportaudio2`（`flask_server.py` 顶层 `import pyaudio`）；
- 端口 5000（HTTP）/ 10002（WS）/ 5173（Live2D 静态）；
- Ingress 加 `/fay`、`/fay-api`、`/fay-ws`，WS 会走公网 + flannel，注意 MTU
  （VXLAN over 公网建议 `--flannel-mtu=1400`）。

---

## 8. 代码侧已做的 5 处改动（部署前提）

| 文件 | 改动 | 为什么 |
|---|---|---|
| `config.py` | 新增 `SERVER_HOST` / `CHECKPOINT_PATH` | 容器里要 `0.0.0.0`、checkpoint 要落 PVC |
| `run_server.py:1221` | `host=config.SERVER_HOST` | 原来写死 `127.0.0.1`，Service 打不进来 |
| `agent/driver.py:49` | 用 `config.CHECKPOINT_PATH` | 原来固定 `BASE_DIR/bank.db`，无法挂卷 |
| `agent/main_graph.py:559` | 用 `config.CHECKPOINT_PATH` | 兜底路径原来是相对路径 `"bank.db"` |
| `requirements.txt` | 补 `cryptography>=42` | MySQL 8 `caching_sha2_password` 必需，缺了连不上库 |
