FROM python:3.12-slim

# Node for the WASM decryption step + ffmpeg for HLS download.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY package.json ./
RUN npm install --omit=dev

COPY decrypt.js module.wasm download.py ./
COPY src ./src

ENV WSG_DOWNLOAD_DIR=/downloads \
    PYTHONUNBUFFERED=1 \
    PORT=8765

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/health || exit 1

CMD ["python", "-m", "uvicorn", "src.watchseries.main:app", \
     "--host", "0.0.0.0", "--port", "8765"]
