# ============================================================================
# bank-agent 运行镜像（backend 与 mock-bank 共用，仅启动命令不同）
#
# ★ 不要加 `# syntax=docker/dockerfile:1`：那会让 BuildKit 去 Docker Hub 拉
#   frontend 镜像（registry-1.docker.io/docker/dockerfile），国内网络常直接 EOF。
#   本文件没用任何 1.x 专属语法，内置 frontend 足够。
#
#   backend   : python run_server.py                → 8178（Agent + 静态 SPA）
#   mock-bank : python -m uvicorn mock_bank.main:app --host 0.0.0.0 --port 8003
#
# 构建（在 **Windows 本地** 的 bank-agent/ 目录下，需 Docker Desktop）：
#   docker build --platform linux/amd64 -t bank-agent:1.0 .
#   docker save bank-agent:1.0 -o bank-agent.tar
#   然后把 tar 上传到 master，sudo k3s ctr -n k8s.io images import bank-agent.tar
#
# ★★ 绝对不要在主节点上安装 Docker / 执行 docker build ★★
#   K3s 底层是 containerd，宿主再装 dockerd 会引入 iptables/网桥规则，
#   与 flannel + Traefik 抢网络，轻则 Pod 不通、重则集群失联。
#   master 上只用 `k3s ctr`（containerd 客户端）导入镜像，不装 docker。
#
# ★ 节点访问不了 DockerHub，镜像必须离线导入，不能在节点上 pull。
# ============================================================================
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8

# tzdata：容器默认 UTC，订阅扣款 / 账单日期 / 日志会整体差 8 小时。
# curl：仅用于容器内手工排障（K8s 探针不走它）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo "$TZ" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 非 root 用户提前建好，避免每次改代码都重跑这一层。
# local-path 创建的卷目录权限为 0777，uid 10001 可直接写；
# /data 用于挂 checkpoint（CHECKPOINT_PATH=/data/bank.db）。
RUN useradd -u 10001 -m -s /bin/bash bank \
    && mkdir -p /data \
    && chown -R 10001:10001 /data

# 依赖层单独缓存：改代码不会重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import pymysql, cryptography, fastapi, langgraph; print('deps ok')"

# 业务代码（.dockerignore 已排除 .env / .venv / bank.db* / _reports 等）
COPY --chown=10001:10001 . .

# ★ 知识库的真实路径是 BASE_DIR.parent/"docs"/"knowledge"，即容器内 **/docs/knowledge**
#   （见 utils/knowledge.py、routes/admin.py），不是 /app/docs/knowledge。
#   放错位置不会报错，只会表现为"知识库一片空白、RAG 检索不到任何东西"。
#   源码在 ../docs/knowledge（agaent/docs/knowledge），由 k8s/build-and-import.sh 同步进来。
COPY --chown=10001:10001 docs /docs

USER 10001

EXPOSE 8178 8003

# 默认入口 = backend；mock-bank 由 Deployment 的 command 覆盖
CMD ["python", "run_server.py"]
