# 素材抓取服务（小红书 / 抖音 → iPhone）
# 目标平台：群晖 DSM（Container Manager）/ 任何 x86_64 或 arm64 的 Linux
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PORT=8765

# curl：解析器做 TLS 兜底（Linux 上没有 curl.exe）
# ca-certificates：https 证书校验；tzdata：容器内时间正确
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 是否安装「手机扫码登录」能力。默认不装：镜像小、NAS 内存友好。
# 需要时加 --build-arg INSTALL_BROWSER=1（会多装 Chromium 及系统依赖，约 +500MB）。
ARG INSTALL_BROWSER=0

# 先装依赖，利用层缓存。
# 清单以 requirements.txt 为**唯一真源**：这里以前硬编码了一份 pip 列表，和
# requirements.txt 各写各的——加依赖时极易只改一处，镜像里却没有那个包。
# curl_cffi 在少数架构上没有预编译 wheel：整体安装失败时剔除它重装（降级后解析仍可用，
# 但「自动接住平台滚动下发的 Cookie」会失效，那条能力依赖 curl_cffi 能读到响应 Cookie）。
COPY requirements.txt ./
RUN (pip install -r requirements.txt \
     || (echo "!! requirements.txt 整体安装失败（多为 curl_cffi 无 wheel），改为剔除它后重装" \
         && grep -vi 'curl_cffi' requirements.txt > /tmp/req.core \
         && pip install -r /tmp/req.core)) \
 && if [ "$INSTALL_BROWSER" = "1" ]; then \
      echo ">>> 安装浏览器登录能力（Chromium + 系统依赖）" \
      && pip install playwright \
      && playwright install --with-deps chromium; \
    else \
      echo ">>> 跳过浏览器登录（INSTALL_BROWSER=0）；需要手机扫码登录请用 --build-arg INSTALL_BROWSER=1 重建"; \
    fi

COPY . .

# 构建期自检：dunder 文件极容易被 .dockerignore 的下划线通配符误伤
# （曾用 `_*` 把 parsers/__init__.py 排掉 → parsers 退化成命名空间包，
#  容器启动才报 `ImportError: cannot import name 'parse' from 'parsers'`）。
# 放在这里，这类缺件会在构建阶段就暴露，而不是等部署起来才发现。
RUN test -f parsers/__init__.py \
 && python -c "import parsers; assert callable(getattr(parsers, 'parse', None)), 'parsers.parse 缺失'; print('>>> 构建自检通过：parsers 包完整')"

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

CMD ["python", "server.py"]
