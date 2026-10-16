#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# StockMaster 服务器端部署脚本（在服务器的仓库副本根目录执行）
#
#   ./deploy/deploy.sh [版本号]      # 版本号缺省用 git describe --tags --always
#
# 流程：构建镜像（服务器本地）→ 预备份数据卷 → 重建容器 → 轮询健康检查
#       失败时打印容器日志并自动回滚到上一个镜像。
#
# 环境变量（一般不用传）：
#   SKIP_BUILD=1            跳过构建，只重建容器（仅改了 .env 时用）
#   BUILD_BASE_REGISTRY     基础镜像源，默认 mirror.ccs.tencentyun.com/library
#   HEALTH_TIMEOUT          健康探测上限秒数，默认 180
#   HEALTH_PORT             健康探测端口，默认 8770
#   IMAGE_REPO_OVERRIDE     镜像仓库名前缀，默认 stockmaster
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE="docker-compose.prod.yml"
DOCKERFILE="deploy/Dockerfile.prod"
ENV_FILE=".env"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
HEALTH_PORT="${HEALTH_PORT:-8770}"
IMAGE_REPO="${IMAGE_REPO_OVERRIDE:-stockmaster}"
KEEP_BACKUPS="${KEEP_BACKUPS:-5}"

log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "未找到 docker"
docker compose version >/dev/null 2>&1 || die "未找到 docker compose 插件"
[ -f "$COMPOSE_FILE" ] || die "缺少 $COMPOSE_FILE（请在仓库根目录运行本脚本）"
[ -f "$DOCKERFILE" ]   || die "缺少 $DOCKERFILE"

# ── .env ────────────────────────────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  [ -f deploy/.env.prod.example ] || die "缺少 $ENV_FILE 且无模板可生成"
  cp deploy/.env.prod.example "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  die "已从模板生成 $ENV_FILE —— 请先核对端口/配置，再重新执行本脚本"
fi
chmod 600 "$ENV_FILE" 2>/dev/null || true

# 端口从 .env 的 SM_BIND（形如 0.0.0.0:8770）推导，允许命令行用 HEALTH_PORT 覆盖
ENV_PORT="$(grep -oE '^SM_BIND=.*' "$ENV_FILE" | head -1 | sed 's/.*://' || true)"
if [ -n "$ENV_PORT" ]; then
  HEALTH_PORT="$ENV_PORT"
fi
HEALTH_URL="http://127.0.0.1:${HEALTH_PORT}/api/v1/health"

# ── 版本与镜像 ──────────────────────────────────────────────────────
if [ "${1:-}" != "" ]; then
  VERSION="$1"
else
  VERSION="$(git describe --tags --always 2>/dev/null || echo dev)"
fi
IMAGE_TAG="${IMAGE_REPO}:${VERSION}"

# 记录当前正在运行的镜像，失败时用于回滚
PREV_IMAGE="$(docker inspect -f '{{.Config.Image}}' stockmaster 2>/dev/null || true)"

log "目标版本 ${VERSION} → ${IMAGE_TAG}"
if [ -n "$PREV_IMAGE" ]; then
  log "当前镜像 ${PREV_IMAGE}"
fi

DC=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

# ── 1. 构建 ─────────────────────────────────────────────────────────
if [ "${SKIP_BUILD:-0}" = "1" ]; then
  warn "SKIP_BUILD=1，跳过构建"
  docker image inspect "$IMAGE_TAG" >/dev/null 2>&1 || die "镜像 $IMAGE_TAG 不存在，无法跳过构建"
else
  BASE_REGISTRY="${BUILD_BASE_REGISTRY:-mirror.ccs.tencentyun.com/library}"
  log "构建镜像（基础镜像源 ${BASE_REGISTRY}；首次约 10-20 分钟，之后走层缓存）"
  docker build \
    -f "$DOCKERFILE" \
    --build-arg "BASE_REGISTRY=${BASE_REGISTRY}" \
    -t "$IMAGE_TAG" \
    .
fi

# 镜像名写回 .env（compose 从这里取）
if grep -q '^SM_IMAGE=' "$ENV_FILE"; then
  sed -i "s|^SM_IMAGE=.*|SM_IMAGE=${IMAGE_TAG}|" "$ENV_FILE"
else
  printf 'SM_IMAGE=%s\n' "$IMAGE_TAG" >> "$ENV_FILE"
fi

# ── 2. 预备份数据卷 ─────────────────────────────────────────────────
VOLUME_NAME="$(grep -oE '^SM_VOLUME=.*' "$ENV_FILE" | head -1 | cut -d= -f2 || true)"
VOLUME_NAME="${VOLUME_NAME:-stockmaster-data}"
mkdir -p backups
if docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
  TS="$(date +%Y%m%d-%H%M%S)"
  ARCHIVE="pre-deploy-${VERSION}-${TS}.tar.gz"
  log "预备份数据卷 ${VOLUME_NAME} → backups/${ARCHIVE}"
  # 用刚构建的镜像自带的 tar 备份，不额外拉基础镜像
  if docker run --rm \
      -v "${VOLUME_NAME}:/data" \
      -v "$ROOT/backups:/backup" \
      "$IMAGE_TAG" tar czf "/backup/${ARCHIVE}" -C /data . 2>/dev/null; then
    log "预备份完成（$(du -sh "backups/${ARCHIVE}" | cut -f1)）"
  else
    warn "预备份失败，继续部署（不影响回滚镜像）"
  fi
  # 只保留最近 N 份，避免备份把磁盘写满
  ls -1t backups/pre-deploy-*.tar.gz 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f
else
  log "数据卷 ${VOLUME_NAME} 尚不存在，跳过预备份（首次部署）"
fi

# ── 3. 重建容器 ─────────────────────────────────────────────────────
log "重建容器"
"${DC[@]}" up -d --force-recreate --remove-orphans

# ── 4. 健康探测 ─────────────────────────────────────────────────────
log "等待健康检查 ${HEALTH_URL}（最长 ${HEALTH_TIMEOUT}s）"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
ok=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  if curl -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done

if [ "$ok" != "1" ]; then
  warn "健康检查未通过，最近 60 行日志："
  "${DC[@]}" logs --tail 60 app || true
  if [ -n "$PREV_IMAGE" ] && [ "$PREV_IMAGE" != "$IMAGE_TAG" ]; then
    warn "回滚到上一个镜像 ${PREV_IMAGE}"
    sed -i "s|^SM_IMAGE=.*|SM_IMAGE=${PREV_IMAGE}|" "$ENV_FILE"
    "${DC[@]}" up -d --force-recreate || true
  fi
  die "部署失败"
fi

log "部署成功：$(curl -fsS "$HEALTH_URL")"
log "对外地址：http://<域名或公网IP>:${HEALTH_PORT}"
log "容器状态："
"${DC[@]}" ps
