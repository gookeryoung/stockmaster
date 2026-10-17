# CI/容器化用 Dockerfile
# 使用国内镜像源拉取基础镜像（如不需要可替换为官方镜像）
# 备选镜像源前缀：docker.1ms.run / dockerpull.com / docker.xuanyuan.me
FROM docker.m.daocloud.io/python:3.13-slim

# ---- 国内镜像源 ----
ENV PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ENV PIP_TRUSTED_HOST=mirrors.cloud.tencent.com
ENV UV_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ENV UV_TRUSTED_HOST=mirrors.cloud.tencent.com

# 环境变量：非交互 + 路径配置
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/uv-cache \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

# 配置 apt 国内镜像（华为云）并安装系统依赖
RUN sed -i 's|deb.debian.org|mirrors.cloud.tencent.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*


# 安装 uv（国内镜像）
RUN pip install --no-cache-dir uv -i https://mirrors.cloud.tencent.com/pypi/simple/

# 预装项目所需 Python 版本（min==max 时只装一次）
RUN uv python install 3.13

# 预装项目 dev 依赖（仅复制依赖描述文件，利用 Docker 层缓存）
WORKDIR /workspace
COPY pyproject.toml uv.lock alembic.ini README.md ./
COPY backend/ ./backend/

# 同步依赖到 /opt/venv（CI 时直接复用）
# uv.lock 随仓库提交，--frozen 锁定版本保证可复现；若 lock 与 pyproject 失配则直接失败，
# 而不是静默重新解析（避免构建出的镜像与本地/CI 依赖不一致）。
RUN uv sync --frozen --no-install-project --extra dev

# 持久化 uv 缓存目录（CI 可挂载到宿主机加速）
VOLUME ["/uv-cache"]

# 以非 root 用户运行（最小权限）
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser /workspace /uv-cache /opt/venv
USER appuser

# 默认入口
CMD ["/bin/bash"]
