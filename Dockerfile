# NOTE: The MetaTrader5 Python package is Windows-only. This image runs the
# Telegram listener + parser pipeline and the MT5 *simulator* (DRY_RUN) on
# Linux. For live trading, run MT5 on a Windows host/VPS (or under Wine with a
# MetaTrader bridge) and point this container/host at it. See README.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for better layer caching. MetaTrader5 is skipped on Linux
# via the platform marker in requirements.txt.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Persist Telethon session, logs and state across restarts.
VOLUME ["/app/logs", "/app/state"]

CMD ["python", "main.py"]
