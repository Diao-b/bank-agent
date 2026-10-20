#!/usr/bin/env bash
# ============================================================================
# bank-agent 镜像：Windows 本地构建 → 离线导入 K3s
#
# 流程（严格按这个顺序，不要省步）：
#   1) 本地（Windows + Docker Desktop）: docker build
#   2) 本地: docker save 成一个 tar（含 bank-agent 与 mysql:8.0 两个镜像）
#   3) 手动把 tar 上传到 master
#   4) master 上: sudo k3s ctr -n k8s.io images import
#
# ★★★ 绝对不要在 master / worker 上安装 Docker 或执行 docker build ★★★
#   K3s 的运行时是 containerd。宿主再起一个 dockerd 会写入 iptables NAT 与
#   docker0 网桥规则，与 flannel(VXLAN) + Traefik 争抢转发链路 —— 轻则
#   Service 不通、Pod 拿不到 IP，重则整个集群失联。脚本会主动拦截这种误操作。
#
# 用法（在 bank-agent/ 目录下）：
#   bash k8s/build-and-import.sh
#   TAG=1.1 bash k8s/build-and-import.sh
#   SKIP_MYSQL=1 bash k8s/build-and-import.sh     # master 已有 mysql:8.0 时
# ============================================================================
set -euo pipefail

IMAGE="${IMAGE:-bank-agent}"
TAG="${TAG:-1.0}"
PLATFORM="${PLATFORM:-linux/amd64}"
DIST_DIR="${DIST_DIR:-./dist}"
MASTER_IP="${MASTER_IP:-118.31.61.128}"
SSH_USER="${SSH_USER:-root}"
SKIP_MYSQL="${SKIP_MYSQL:-0}"
# mock-bank 现在是**自动调度**（无 nodeSelector），K8s 可能把它放到任意一台 worker。
# 因此 bank-agent 镜像必须预置在**所有可能被调度的节点**上，否则那台机器会 ImagePullBackOff。
# 两台 worker 的公网 IP 填这里；留空则脚本只打印占位符。
WORKER_IP="${WORKER_IP:-}"
WORKER2_IP="${WORKER2_IP:-}"

die() { echo "!! $*" >&2; exit 1; }

cd "$(dirname "$0")/.."
echo "==> 工作目录 : $(pwd)"
echo "==> 镜像     : ${IMAGE}:${TAG}  (${PLATFORM})"

# ---------------------------------------------------------------------------
# 0. 安全闸门：不允许在 K3s 节点上构建
# ---------------------------------------------------------------------------
if [ "$(uname -s)" = "Linux" ] && { [ -S /run/k3s/containerd/containerd.sock ] || [ -d /var/lib/rancher/k3s ]; }; then
  die "检测到本机是 K3s 节点。禁止在此安装/使用 docker 构建（会与 containerd 网络冲突）。
   请回到 Windows 本地执行本脚本，只把 tar 传上来用 sudo k3s ctr 导入。"
fi

command -v docker >/dev/null 2>&1 \
  || die "本机没有 docker。请安装 Docker Desktop（Windows）后重开终端再执行。"
docker version >/dev/null 2>&1 || die "docker 已安装但未启动，请先打开 Docker Desktop。"

# ---------------------------------------------------------------------------
# 1. 同步知识库到构建上下文
#    代码读的是 BASE_DIR.parent/docs/knowledge → 容器内 /docs/knowledge，
#    真身在 ../docs/knowledge（agaent/docs/knowledge）。
# ---------------------------------------------------------------------------
KB_SRC="../docs/knowledge"
KB_DST="./docs/knowledge"
if [ -d "${KB_SRC}" ]; then
  mkdir -p ./docs
  rm -rf "${KB_DST}"
  cp -a "${KB_SRC}" "${KB_DST}"
  echo "==> 知识库同步: $(find "${KB_DST}" -type f | wc -l | tr -d ' \r') 个文件 → docs/knowledge"
else
  echo "!! 警告：找不到 ${KB_SRC}，镜像内知识库将为空（RAG 完全失效）"
fi

# ---------------------------------------------------------------------------
# 2. 构建
# ---------------------------------------------------------------------------
echo
echo "==> [1/4] docker build"
docker build --platform "${PLATFORM}" -t "${IMAGE}:${TAG}" .

echo
echo "==> [2/4] 镜像自检"

ARCH=$(docker image inspect "${IMAGE}:${TAG}" --format '{{.Architecture}}')
echo "    架构: ${ARCH}（节点是 x86_64，必须是 amd64）"
[ "${ARCH}" = "amd64" ] || die "镜像架构是 ${ARCH}，导入 x86 节点会报 exec format error"

KNOW=$(docker run --rm --entrypoint sh "${IMAGE}:${TAG}" -c 'ls /docs/knowledge 2>/dev/null | wc -l' | tr -d ' \r')
echo "    /docs/knowledge 文档数: ${KNOW:-0}"
[ "${KNOW:-0}" -ge 1 ] || die "/docs/knowledge 是空的 —— 知识库路径错了，RAG 会全失效"

LEAK=0
docker run --rm --entrypoint sh "${IMAGE}:${TAG}" -c 'test -f .env -o -f bank.db' || LEAK=$?
if [ "${LEAK}" -eq 0 ]; then
  die ".env 或 bank.db 被打进镜像（数据泄露），已中止"
fi
echo "    机密与本地库未进镜像 ✓"

# ---------------------------------------------------------------------------
# 3. 导出（分两个 tar）
#   master 需要：bank-agent + mysql:8.0
#   Worker 需要：bank-agent（mock-bank 跑在 Worker 上，只它需要这个）
#   分开打是为了少传 600MB —— 别把 mysql 也传到 2G 的那台机器上。
# ---------------------------------------------------------------------------
echo
echo "==> [3/4] docker save"
mkdir -p "${DIST_DIR}"

APP_TAR="${DIST_DIR}/${IMAGE}-${TAG}.tar"
rm -f "${APP_TAR}"
docker save -o "${APP_TAR}" "${IMAGE}:${TAG}"
echo "    $(du -h "${APP_TAR}" | cut -f1)  ${APP_TAR}"

MYSQL_TAR=""
if [ "${SKIP_MYSQL}" != "1" ]; then
  echo "    master 拉不到 DockerHub，mysql:8.0 必须离线带入"
  docker image inspect mysql:8.0 >/dev/null 2>&1 || docker pull --platform "${PLATFORM}" mysql:8.0
  MYSQL_TAR="${DIST_DIR}/mysql-8.0.tar"
  rm -f "${MYSQL_TAR}"
  docker save -o "${MYSQL_TAR}" mysql:8.0
  echo "    $(du -h "${MYSQL_TAR}" | cut -f1)  ${MYSQL_TAR}"
fi

echo "    （带宽小可先压缩：gzip -1 <file>，ctr 同样认 .tar.gz）"

# ---------------------------------------------------------------------------
# 4. 手动上传 + 导入（脚本不自动执行，避免在错误机器上跑 docker）
# ---------------------------------------------------------------------------
MYSQL_STEP=""
if [ -n "${MYSQL_TAR}" ]; then
  MYSQL_STEP="     sudo k3s ctr -n k8s.io images import /root/$(basename "${MYSQL_TAR}")
"
fi

cat <<EOF

==> [4/4] 接下来手动执行（★ 全程只用 k3s ctr，绝不在节点装 docker）

  ① 主节点上传（两个包）
     scp ${APP_TAR} ${MYSQL_TAR:-} ${SSH_USER}@${MASTER_IP}:/root/

  ② 主节点导入
     ssh ${SSH_USER}@${MASTER_IP}
     sudo k3s ctr -n k8s.io images import /root/$(basename "${APP_TAR}")
${MYSQL_STEP}     sudo k3s ctr -n k8s.io images ls -q | grep -E 'bank-agent|mysql'

  ③ ★ 两台 Worker 也要导入主镜像
     mock-bank 现在是自动调度（没有 nodeSelector），可能落到任意一台 worker。
     哪台没有镜像，它就可能在那台上 ImagePullBackOff。三台机器都要导。

     for H in ${WORKER_IP:-<worker1公网IP>} ${WORKER2_IP:-<worker2公网IP>}; do
       scp ${APP_TAR} ${SSH_USER}@\$H:/root/
       ssh ${SSH_USER}@\$H "sudo k3s ctr -n k8s.io images import /root/$(basename "${APP_TAR}")"
       ssh ${SSH_USER}@\$H "sudo k3s ctr -n k8s.io images ls -q | grep bank-agent"
     done

  ④ 校验完删掉临时文件
     rm -f /root/$(basename "${APP_TAR}")${MYSQL_TAR:+ /root/$(basename "${MYSQL_TAR}")}

  然后按 k8s/README.md §3 的顺序 apply YAML（清单里的 image 就是 ${IMAGE}:${TAG}）。
EOF
