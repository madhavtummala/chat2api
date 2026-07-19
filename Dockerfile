FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CHAT2API_USER_DATA_DIR=/app/.browser_profile

WORKDIR /app

# VNC stack (only exercised by the one-time `login` mode) + tini for clean
# signal handling so the browser profile is flushed on shutdown.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install Chromium plus its OS-level dependencies.
RUN playwright install --with-deps chromium

COPY src ./src
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Persist the authenticated browser profile across container restarts.
VOLUME ["/app/.browser_profile"]

# 9000 = API server, 6080 = noVNC (login mode only).
EXPOSE 9000 6080

ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["server"]
