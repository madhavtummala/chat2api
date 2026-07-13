FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install Chromium plus its OS-level dependencies.
RUN playwright install --with-deps chromium

COPY src ./src

# Persist the authenticated browser profile across container restarts.
VOLUME ["/app/.browser_profile"]

EXPOSE 8000

CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
