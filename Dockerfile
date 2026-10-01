# TradeLab image: one image for every service (migrate, bot, and later the
# daily jobs). Built on the server by `docker compose build`, like STAIR.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

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
