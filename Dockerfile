# Pinned 2026-10-16 from docker.io/library/python:3.12-slim (3.12.15-slim-trixie)
# Multi-arch index: sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
# linux/amd64     : sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    YENO_HOST=0.0.0.0 \
    YENO_PORT=8080

RUN groupadd --gid 10001 yeno \
    && useradd --uid 10001 --gid yeno --shell /usr/sbin/nologin --no-create-home yeno

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

USER yeno
EXPOSE 8080

CMD ["python", "-m", "bot.server"]
