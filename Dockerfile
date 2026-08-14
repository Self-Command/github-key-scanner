# github-key-scanner 镜像
# 用法：docker compose up -d --build（推荐）或 docker build -t github-key-scanner .
FROM python:3.11-slim

# 时区：日志与任务时间显示为北京时间
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖清单以利用构建缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码（.dockerignore 已排除 data/ 与 .env 等）
COPY . .

# 数据目录：数据库 / 日志 / 触发文件都放这里（compose 挂载持久卷）
RUN mkdir -p /app/data && chown -R 1000:1000 /app
USER 1000

VOLUME ["/app/data"]

EXPOSE 8765

# 默认启动 Web 服务；daemon 由 compose 用 command 覆盖
CMD ["python", "-u", "web/app.py"]
