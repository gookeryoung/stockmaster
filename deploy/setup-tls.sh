#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# StockMaster —— 独立子域 HTTPS 一键接入（在服务器上执行）
#
#   sudo DOMAIN=stock.gookeryoung.cn ACME_EMAIL=you@example.com \
#        bash deploy/setup-tls.sh
#
# 做四件事：
#   1. DNS 预检（子域必须已解析到本机，否则 ACME 必然失败）
#   2. certbot(HTTP-01) 签发证书 —— 复用 cndb nginx 已在 80 端口提供的
#      /.well-known/acme-challenge/ webroot，因此不需要停 cndb 的服务
#   3. 把证书落成真实文件到 /opt/cndb/certs/stock-*.pem
#      （certbot 的 live/ 目录全是相对软链，跨容器挂载会断，必须拷实体）
#   4. 把 nginx server 块装进 /opt/cndb/deploy/nginx/extra/ 并 reload，
#      nginx -t 不通过则自动回滚，保证 cndb 不受影响
#
# 幂等：证书还在有效期内时 certbot 不会重签；重复执行只会重装配置 + reload。
# 撤销：rm /opt/cndb/deploy/nginx/extra/stockmaster.conf 后
#       docker exec cndb-nginx nginx -s reload —— cndb 完全不受影响。
#
# 环境变量（都有默认值）：
#   DOMAIN            子域名，默认 stock.gookeryoung.cn
#   ACME_EMAIL        注册邮箱；留空则用 --register-unsafely-without-email
#   CNDB_DIR          cndb 部署目录，默认 /opt/cndb
#   NGINX_CONTAINER   cndb 的 nginx 容器名，默认 cndb-nginx
#   APP_PORT          StockMaster 端口，默认 8770
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

DOMAIN="${DOMAIN:-stock.gookeryoung.cn}"
ACME_EMAIL="${ACME_EMAIL:-}"
CNDB_DIR="${CNDB_DIR:-/opt/cndb}"
NGINX_CONTAINER="${NGINX_CONTAINER:-cndb-nginx}"
APP_PORT="${APP_PORT:-8770}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_CONF="$SCRIPT_DIR/nginx/stockmaster-subdomain.conf"

WEBROOT="$CNDB_DIR/certbot-www"       # → 容器内 /var/www/certbot      (rw)
CERTS="$CNDB_DIR/certs"               # → 容器内 /etc/nginx/certs      (ro)
EXTRA="$CNDB_DIR/deploy/nginx/extra"  # → 容器内 /etc/nginx/extra      (ro)
LE_STATE="$CNDB_DIR/.letsencrypt"     # certbot 的 config-dir，仅宿主机用
CERT_PREFIX="stock"

log()  { printf '\033[1;34m[tls]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 0. 前置检查 ─────────────────────────────────────────────────────
[ "$(id -u)" -eq 0 ] || die "需要 root（要写 $CNDB_DIR 并操作 docker）"
command -v docker >/dev/null || die "未找到 docker"
[ -f "$SRC_CONF" ] || die "找不到 $SRC_CONF"
docker inspect "$NGINX_CONTAINER" >/dev/null 2>&1 || die "容器 $NGINX_CONTAINER 不存在"
for d in "$WEBROOT" "$CERTS" "$EXTRA"; do
  [ -d "$d" ] || die "$d 不存在 —— $CNDB_DIR 不是预期的 cndb 部署目录？"
done

# ── 1. DNS 预检 ─────────────────────────────────────────────────────
log "DNS 预检 $DOMAIN"
RESOLVED="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | paste -sd, - || true)"
if [ -z "$RESOLVED" ]; then
  die "$DOMAIN 未解析。请先在域名服务商（本域在 DNSPod）添加：
     记录类型 A | 主机记录 ${DOMAIN%%.*} | 记录值 <本机公网IP> | TTL 600
   加完等 1-2 分钟再重跑本脚本。"
fi
log "解析到：$RESOLVED"
# 认证用的权威 IP 由 nginx 从 80 端口回给 Let's Encrypt，这里只提示不阻断
HOST_PUBLIC_IP="$(curl -fsS -m 5 -H 'Metadata-Flavor: Google' \
  http://metadata.tencentyun.com/latest/meta-data/public-ipv4 2>/dev/null || true)"
if [ -n "$HOST_PUBLIC_IP" ] && ! printf '%s' ",$RESOLVED," | grep -q ",$HOST_PUBLIC_IP,"; then
  warn "解析结果不含本机公网 IP $HOST_PUBLIC_IP —— 若这是 A 记录刚生效/多线路解析，可忽略；否则 ACME 会失败。"
fi

# ── 2. 签发证书 ─────────────────────────────────────────────────────
mkdir -p "$WEBROOT" "$CERTS" "$LE_STATE"
chmod 755 "$WEBROOT"
# acme-challenge 目录交给 nginx 读
mkdir -p "$WEBROOT/.well-known/acme-challenge"

if [ -n "$ACME_EMAIL" ]; then
  REG_ARGS=(--email "$ACME_EMAIL")
else
  REG_ARGS=(--register-unsafely-without-email)
  warn "未提供 ACME_EMAIL，证书到期前收不到提醒（可设 ACME_EMAIL=you@example.com）"
fi

log "申请证书（HTTP-01，webroot=$WEBROOT）"
docker run --rm \
  -v "$WEBROOT":/var/www/certbot \
  -v "$LE_STATE":/etc/letsencrypt \
  certbot/certbot certonly \
    --webroot -w /var/www/certbot \
    -d "$DOMAIN" \
    --non-interactive --agree-tos --no-eff-email \
    --keep-until-expiring \
    "${REG_ARGS[@]}"

LE_LIVE="$LE_STATE/live/$DOMAIN"
[ -s "$LE_LIVE/fullchain.pem" ] || die "签发失败：$LE_LIVE/fullchain.pem 不存在"
[ -s "$LE_LIVE/privkey.pem" ]   || die "签发失败：$LE_LIVE/privkey.pem 不存在"

# 软链在容器里会指向 /etc/nginx/archive/... （未挂载）→ 必须拷贝实体文件
install -m 644 "$LE_LIVE/fullchain.pem" "$CERTS/${CERT_PREFIX}-fullchain.pem"
install -m 600 "$LE_LIVE/privkey.pem"   "$CERTS/${CERT_PREFIX}-privkey.pem"
log "证书已安装：$CERTS/${CERT_PREFIX}-{fullchain,privkey}.pem"
openssl x509 -in "$CERTS/${CERT_PREFIX}-fullchain.pem" -noout -subject -dates 2>/dev/null \
  | sed 's/^/      /' || true

# ── 3. 安装 nginx 配置（带网关自动纠正 + 失败回滚）───────────────────
DEST="$EXTRA/stockmaster.conf"
TMP="$(mktemp)"

# 上游网关从 cndb-nginx 实际网络里取，避免硬编码网段失效
GW="$(docker inspect "$NGINX_CONTAINER" \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{$v.Gateway}}{{"\n"}}{{end}}' \
  | head -1)"
[ -n "$GW" ] || die "取不到 $NGINX_CONTAINER 的网关地址"
GATEWAY_FILE="$CNDB_DIR/stockmaster-gateway"
log "反代上游 = $GW:$APP_PORT（网关取自 $NGINX_CONTAINER 的网络）"
sed -E "s|^([[:space:]]*server[[:space:]]+)[0-9.]+:${APP_PORT};|\1${GW}:${APP_PORT};|" \
  "$SRC_CONF" > "$TMP"

BACKUP=""
if [ -f "$DEST" ]; then
  BACKUP="$(mktemp)"
  cp -p "$DEST" "$BACKUP"
fi
install -m 644 "$TMP" "$DEST"
rm -f "$TMP"
printf '%s\n' "$GW" > "$GATEWAY_FILE"

log "校验 nginx 配置"
if ! docker exec "$NGINX_CONTAINER" nginx -t; then
  if [ -n "$BACKUP" ]; then
    cp -p "$BACKUP" "$DEST"
    warn "已回滚到原配置"
  else
    rm -f "$DEST"
    warn "已移除新配置"
  fi
  die "nginx -t 失败，未 reload —— cndb 保持原状"
fi
[ -n "$BACKUP" ] && rm -f "$BACKUP"

log "reload nginx"
docker exec "$NGINX_CONTAINER" nginx -s reload

# ── 4. 自测 ─────────────────────────────────────────────────────────
sleep 2
CODE="$(curl -fsS -m 15 -o /dev/null -w '%{http_code}' "https://$DOMAIN/" 2>/dev/null || echo 000)"
HEALTH="$(curl -fsS -m 15 "https://$DOMAIN/api/v1/health" 2>/dev/null || echo '(取不到)')"
log "https://$DOMAIN/           → HTTP $CODE"
log "https://$DOMAIN/api/v1/health → $HEALTH"
log "证书路径 $LE_LIVE（续期：certbot renew，webroot 已是同一份）"
log "完成。cndb 未受影响（本配置仅新增 server_name=$DOMAIN 的 server 块）。"
