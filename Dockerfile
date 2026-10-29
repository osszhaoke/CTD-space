# StarMap C01 · 睡眠房容器化镜像
FROM python:3.12-alpine

WORKDIR /app

# 安装 curl 与 tzdata 用于健康检查和时区设置（纯 Python 标准库零 pip 依赖）
RUN apk add --no-cache curl tzdata

# 复制单房产品源码与展示页面静态资源（不打入冻结历史模块）
COPY development/ctds/room_display /app/development/ctds/room_display

# 环境变量设置：Python 路径、时区与监听宿主（0.0.0.0 允许容器端口映射）
ENV PYTHONPATH=/app/development/ctds
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai
ENV CTDS_LISTEN_HOST=0.0.0.0
ENV CTDS_IN_CONTAINER=1

# 暴露大屏与数据服务端口
EXPOSE 8765

# 挂载卷：持久化配置与 SQLite 数据库
VOLUME ["/app/var"]

# 健康检查探针
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://127.0.0.1:8765/healthz || exit 1

# 启动服务：采集器 + 大屏展示服务
CMD ["sh", "-c", "python3 -B -m room_display --config /app/var/ctds-c01/config.json init-config && python3 -B -m room_display --config /app/var/ctds-c01/config.json migrate-config && python3 -B -m room_display --config /app/var/ctds-c01/config.json --host 0.0.0.0 run"]
