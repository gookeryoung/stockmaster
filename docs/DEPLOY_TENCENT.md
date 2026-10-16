# 腾讯云生产部署指南

面向**轻量应用服务器单机**形态：仓库自带部署链，服务器上 `git clone` 源码后**本地构建镜像**
（镜像不过网、不依赖镜像仓库），`deploy.sh` 负责备份 → 重建 → 健康探测 → 失败回滚。

---

## 一、当前线上环境

| 项 | 值 |
|---|---|
| 实例 | 腾讯云轻量应用服务器 `ap-shanghai`（Ubuntu 24.04 / 2C2G / 50G SSD） |
| 公网 IP | `49.235.154.84` |
| 访问地址 | **http://gookeryoung.cn:8770** |
| 部署目录 | `/opt/stockmaster` |
| 容器名 | `stockmaster` |
| 数据卷 | `stockmaster-data` → 容器 `/data` |

> **端口为什么是 8770**：同一台机器上已部署 cndb，它的 nginx 容器独占 `80`/`443`
> （域名 gookeryoung.cn 的 HTTPS 由它提供）。StockMaster 因此用独立端口对外，
> 两个服务互不影响。默认端口 8770 与本地 `make dev` 口径一致。

服务形态：

```
浏览器 ──> http://gookeryoung.cn:8770
              │  （Lighthouse 防火墙放行 TCP 8770）
              ▼
        宿主机 0.0.0.0:8770
              │  docker 端口映射
              ▼
        容器 stockmaster:8770
          uvicorn app.main:app  ──> SQLite（/data/data/stockmaster.db，宿主机数据卷内）
```

**数据持久化**：唯一数据卷 `stockmaster-data` 挂在 `/data`，库与训练模型全在里面。换版本只换镜像，卷不动。

---

## 二、一次性准备

### 1. 服务器

Docker 与 git 必需（腾讯云镜像通常已预装）：

```bash
docker --version && docker compose version && git --version
```

国内服务器直连 Docker Hub 会超时，务必配内网镜像加速（**本项目实测有效**）：

```bash
sudo tee /etc/docker/daemon.json <<'EOF'
{ "registry-mirrors": ["https://mirror.ccs.tencentyun.com"] }
EOF
sudo systemctl restart docker
```

建部署目录并取源码：

```bash
mkdir -p /opt && cd /opt
git clone https://github.com/gookeryoung/stockmaster.git stockmaster
cd /opt/stockmaster
```

### 2. 防火墙放行 8770

控制台 → 轻量应用服务器 → 防火墙，添加规则：

| 协议 | 端口 | 来源 | 说明 |
|---|---|---|---|
| TCP | 8770 | 0.0.0.0/0 | StockMaster |

> 不要放行 `2375`（Docker 守护进程端口，等于交出服务器控制权）。
> 换端口时三处要同步：防火墙规则、`.env` 的 `SM_BIND`、`deploy.sh` 的健康探测端口。

### 3. 配置 .env

```bash
cd /opt/stockmaster
cp deploy/.env.prod.example .env
chmod 600 .env
vim .env
```

必看项：

| 变量 | 说明 |
|---|---|
| `SM_BIND` | 对外绑定，形如 `0.0.0.0:8770`。同机已被 80/443 占用时不要改到这两个端口 |
| `SM_IMAGE` | 镜像地址，`deploy.sh` 每次按版本号自动覆写，手工改无意义 |
| `SM_SCHEDULER_ENABLED` | 内置 APScheduler（收盘后同步 + 预警巡检）。**同一份数据只允许一个实例开着** |
| `SM_SYNC_WORKERS` | baostock 并发敏感，建议 ≤ 4 |

> `.env` 不在 git 追踪中，`.dockerignore` 也已排除 —— 不会被提交，也不会被打进镜像。
> 容器内的运行期配置全部由 `docker-compose.prod.yml` 的 `environment` 段注入。

### 4. 首次部署

```bash
cd /opt/stockmaster
./deploy/deploy.sh
```

首次约 10-20 分钟（拉基础镜像 + 装 pandas/scikit-learn/akshare + 构建前端），
之后走 Docker 层缓存通常 2-5 分钟。

---

## 三、日常发布

```bash
cd /opt/stockmaster
git pull
./deploy/deploy.sh v0.1.5      # 版本号可选；缺省取 git describe --tags --always
```

只改了 `.env`（不动代码）时：

```bash
SKIP_BUILD=1 ./deploy/deploy.sh
```

**回滚**：`deploy.sh` 失败会自动回滚到上一个镜像；手工指定历史版本：

```bash
./deploy/deploy.sh v0.1.3
```

---

## 四、数据初始化

新环境是空库，页面所有页面都会是空的。按顺序跑一次（容器内执行，等价于本地 CLI）：

```bash
DC="docker compose --env-file .env -f docker-compose.prod.yml"

$DC exec app python -m app.cli sync-calendar      # 交易日历
$DC exec app python -m app.cli sync-instruments   # 证券主数据
$DC exec app python -m app.cli sync-daily         # 核心池日线（几分钟内可用）
$DC exec app python -m app.cli analytics          # 指标与因子
$DC exec app python -m app.cli train              # 训练模型
$DC exec app python -m app.cli predict            # 批量预测
$DC exec app python -m app.cli stats              # 数据概况
```

> 全市场回补耗时较长、支持断点续传：`sync-daily --full`。
> 服务器 2C2G 上建议在低峰期分批跑，避免与调度器任务叠加抢内存。

页面 **设置** 页也可点击同样的任务，二者等价。

---

## 五、备份与恢复

`deploy.sh` 每次部署前自动把数据卷打包到 `backups/pre-deploy-<版本>-<时间戳>.tar.gz`
（仅保留最近 5 份，`KEEP_BACKUPS` 可调）。

手工备份 / 恢复：

```bash
cd /opt/stockmaster
DC="docker compose --env-file .env -f docker-compose.prod.yml"

# 备份
$DC exec app tar czf /data/backups/manual-$(date +%Y%m%d).tar.gz -C /data .

# 恢复（先停应用，避免写坏库）
$DC exec app tar xzf /data/backups/manual-20261016.tar.gz -C /data
$DC restart app
```

> 宿主机上库文件位于 `/var/lib/docker/volumes/stockmaster-data/_data/data/stockmaster.db`，
> 应急可直接 `cp` 出来，但**不要在容器运行时直接改它**。
> 想异地上传 COS 的话，把上面第一条命令的输出接 `coscmd upload` 即可。

---

## 六、排查

```bash
DC="docker compose --env-file .env -f docker-compose.prod.yml"

$DC ps                                  # 看状态：Up / Restarting / Exited
$DC logs --tail 100 app                 # 关键日志
docker inspect stockmaster --format '{{json .State.Health}}'
curl -v http://127.0.0.1:8770/api/v1/health    # 服务器本机探测，绕过外网定位
```

| 现象 | 原因与处理 |
|---|---|
| 一直 `Restarting` | 启动即崩，看日志首行。常见：`/data` 权限、`.env` 注入的变量格式错 |
| `unhealthy` | 首次启动要建库 + 起调度器，`start_period` 60s 内属正常 |
| 本机 curl 通、外网不通 | Lighthouse 防火墙未放行 8770（见二.2） |
| 外网 404 / 空白页 | 前端产物没进镜像：确认 `frontend/dist` 构建成功（构建日志里的 node 阶段） |
| 页面能开、数据全空 | 未做数据初始化（见第四节） |
| 数据源全部不可用 | 服务器出网异常。`$DC exec app python -m app.cli doctor` 自检 |
| 构建卡在 `FROM`/`npm install` | 基础镜像源或 npm 源不可达。检查 `mirror.ccs.tencentyun.com` 与 `registry.npmmirror.com` |
| 构建 OOM（被 kill 137） | 2G 内存跑前端构建吃紧。先确认 swap 开着（`swapon --show`），或换更大规格 |
| 磁盘不足 | `docker system df` 看占用；`docker image prune -a` 清理旧镜像后重跑 |

---

## 七、安全清单

- [ ] `.env` 权限 `600`，且不在 git 追踪中
- [ ] 防火墙只放行必要端口（22 / 80 / 443 / 8770），SSH 来源可进一步限制
- [ ] 未放行 `2375`
- [ ] 预警 webhook（若填写）指向可信地址
- [ ] 已确认 `deploy.sh` 生成的预备份能实际恢复

> StockMaster 是**无用户体系的研究工具**：只要端口可达，任何能访问该地址的人都可读写数据。
> 若需要对外公开，建议在域名前加一层鉴权（或只在内网/白名单 IP 内开放该端口）。
