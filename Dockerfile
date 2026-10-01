# TradeLab image: one image for every service (migrate, bot, jobs).
# Built locally (or in CI), pushed to ghcr.io and pulled on the server: see
# deploy/README.md. The server never builds.
FROM python:3.12-slim

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.source="https://github.com/alejandrovarelav/tradelab" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.title="tradelab"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    TRADELAB_GIT_SHA=${GIT_SHA}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY database/ ./database/
COPY tradelab/ ./tradelab/
COPY config/   ./config/

# Non-root runtime user.
RUN useradd --system --uid 10001 tradelab
USER tradelab

CMD ["python", "-m", "tradelab.bot"]
