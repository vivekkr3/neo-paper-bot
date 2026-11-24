FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Kolkata
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 neobot && useradd -u 10001 -g 10001 -m neobot
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py alerts.py northflank_runner.py config.json calendar.json ./
RUN mkdir -p /app/runtime && chown 10001:10001 /app/runtime
USER 10001:10001
CMD ["python", "-u", "northflank_runner.py"]
